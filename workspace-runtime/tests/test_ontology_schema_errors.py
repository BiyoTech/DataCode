"""Schema-invalid ontology YAML must degrade gracefully, not crash the flow.

Regression guard: an ontology file with bad field values (e.g. attribute
``type: identifier``) used to raise ``OntologySchemaError`` straight out of
the route handlers, surfacing as an opaque HTTP 400 — the CLI died with
"sandbox unavailable" and the agent had nothing to act on. Now:

* ``POST /ontology/{name}/validate`` answers ``valid=False`` with
  ``error_kind="schema"`` and per-field errors (path + message + got-value).
* ``GET /ontology/{name}`` returns ``model=null`` + the same error list.
* ``sqlmesh ontology validate --all`` prints every offending YAML path and
  exits 1 instead of throwing.
"""

from pathlib import Path

import pytest

from src.api.rest import ontology as ontology_api
from src.api.rest.ontology import get_ontology, validate_ontology
from src.ontology.config import settings as ontology_settings
from src.tools.ontology_commands import run_ontology_command

_BAD_YAML = """
version: "1.0"
description: Campus facilities
entities:
  - name: Building
    attributes:
      - name: building_id
        column: building_id
        type: identifier
      - name: floor_count
        column: floor_count
        type: numerical
"""

_GOOD_YAML = """
version: "1.0"
description: Campus facilities
entities:
  - name: Building
    attributes:
      - name: building_id
        column: building_id
        type: categorical
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    (ws / "ontology").mkdir(parents=True)
    (ws / "ontology" / "campus.yaml").write_text(_BAD_YAML, encoding="utf-8")
    # Containment check: the workspace must live under the configured base.
    monkeypatch.setattr(ontology_settings, "workspace_base_path", str(tmp_path))
    # Skip in-process SQLMesh metadata fetches (not under test here).
    async def _no_metadata(_wp: str):
        return None, None, None

    monkeypatch.setattr(ontology_api, "_fetch_sqlmesh_metadata", _no_metadata)
    return ws


async def test_validate_reports_structured_schema_errors(workspace: Path) -> None:
    outcome = await validate_ontology(
        name="campus", workspace_path=str(workspace), check_sqlmesh=False
    )
    assert outcome["valid"] is False
    assert outcome["error_kind"] == "schema"
    assert outcome["sqlmesh_checked"] is False

    errors = outcome["errors"]
    paths = {e.path for e in errors}
    assert "entities.Building.attributes[0].type" in paths
    assert "entities.Building.attributes[1].type" in paths
    # The offending input value is folded into the message.
    assert any("identifier" in e.message for e in errors)
    assert any("numerical" in e.message for e in errors)


async def test_get_ontology_degrades_instead_of_400(workspace: Path) -> None:
    result = await get_ontology(
        name="campus", workspace_path=str(workspace), check_sqlmesh=False
    )
    assert result["model"] is None
    assert result["schema_invalid"] is True
    assert result["validation_errors"]
    assert result["sqlmesh_checked"] is False


async def test_valid_ontology_still_returns_model(workspace: Path) -> None:
    (workspace / "ontology" / "campus.yaml").write_text(_GOOD_YAML, encoding="utf-8")
    result = await get_ontology(
        name="campus", workspace_path=str(workspace), check_sqlmesh=False
    )
    assert result["model"] is not None
    assert result["schema_invalid"] is False


async def test_cli_validate_all_survives_bad_yaml(workspace: Path) -> None:
    result = await run_ontology_command(
        ["validate", "--all"], workspace_path=str(workspace)
    )
    assert result["exit_code"] == 1
    stdout = result["stdout"]
    assert "campus" in stdout
    assert "INVALID" in stdout
    # Agent-fixable: the exact YAML path appears in the output.
    assert "entities.Building.attributes[0].type" in stdout
    assert "identifier" in stdout


async def test_cli_validate_single_survives_bad_yaml(workspace: Path) -> None:
    result = await run_ontology_command(
        ["validate", "campus"], workspace_path=str(workspace)
    )
    assert result["exit_code"] == 1
    assert result["stdout"].startswith('Ontology "campus" is INVALID')
    assert "entities.Building.attributes[0].type" in result["stdout"]
