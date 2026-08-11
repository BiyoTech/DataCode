"""Canonical asset-dag project scaffold rendering.

Renders the templates under ``src/templates/`` into a workspace's
``asset-dag/`` directory. Rendering is fill-missing for user-editable files
(existing files are never overwritten, so user edits survive), with one
exception: a small set of *managed* bridge files are force-overwritten whenever
their content drifts from the template. This keeps the SQLMesh bridge
(``defs/assets/sql_assets.py``) authoritative and lets us evolve its behavior
(naming, grouping, removed assets) without stale on-disk copies lingering.

Calling this on every code-location ensure is safe and idempotent.

Canonical layout (modern Dagster ``dg`` auto-discovery):

    asset-dag/                  # code location working dir (NOT a package)
    ├── definitions.py          # entrypoint: load_defs(defs_root=defs)
    ├── pyproject.toml          # metadata; [tool.dagster] module_name
    ├── README.md               # layout + conventions
    ├── defs/                   # auto-discovery root (importable package)
    │   ├── __init__.py
    │   ├── assets/             # sql_assets (SQLMesh bridge) + daft_assets
    │   ├── resources/          # resources wired via @dg.definitions
    │   ├── jobs/               # asset jobs
    │   ├── schedules/          # schedules
    │   └── sensors/            # sensors
    └── tests/                  # sibling of defs/, NOT auto-discovered

The ``asset-dag`` directory is the code location's cwd (Dagster puts it on
sys.path), so ``import defs`` resolves even though ``asset-dag`` itself has a
hyphenated, non-importable name.

NOTE: keep these templates in sync with the skeleton embedded in
sqlmesh-service (src/workspace/asset_dag_skeleton.py), which writes the same
files at workspace creation time.
"""

from __future__ import annotations

from pathlib import Path

from src.utils.errors import CodeLocationLoadError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Relative location of the Dagster project inside a workspace.
ASSET_DAG_SUBDIR = "asset-dag"

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_WORKSPACE_PLACEHOLDER = "{{ workspace_name }}"

# Template path (relative to _TEMPLATE_DIR) -> rendered path (relative to
# asset-dag/). Every file, including each package ``__init__.py``, has a
# template, so rendering is a single uniform pass.
_SCAFFOLD_FILES = {
    "definitions.py.tpl": "definitions.py",
    "pyproject.toml.tpl": "pyproject.toml",
    "README.md.tpl": "README.md",
    # defs/ auto-discovery root
    "defs/__init__.py.tpl": "defs/__init__.py",
    "defs/assets/__init__.py.tpl": "defs/assets/__init__.py",
    "defs/assets/sql_assets.py.tpl": "defs/assets/sql_assets.py",
    "defs/assets/daft_assets.py.tpl": "defs/assets/daft_assets.py",
    "defs/resources/__init__.py.tpl": "defs/resources/__init__.py",
    "defs/resources/example_resource.py.tpl": "defs/resources/example_resource.py",
    "defs/jobs/__init__.py.tpl": "defs/jobs/__init__.py",
    "defs/jobs/example_job.py.tpl": "defs/jobs/example_job.py",
    "defs/schedules/__init__.py.tpl": "defs/schedules/__init__.py",
    "defs/schedules/example_schedule.py.tpl": "defs/schedules/example_schedule.py",
    "defs/sensors/__init__.py.tpl": "defs/sensors/__init__.py",
    "defs/sensors/example_sensor.py.tpl": "defs/sensors/example_sensor.py",
    # tests/ (sibling of defs/, not auto-discovered)
    "tests/__init__.py.tpl": "tests/__init__.py",
    "tests/test_assets.py.tpl": "tests/test_assets.py",
}

# Managed bridge files (subset of _SCAFFOLD_FILES) that dagster-service owns and
# keeps authoritative. Unlike user-editable files, these are force-overwritten
# whenever the on-disk content drifts from the rendered template. This eliminates
# stale bridge code (e.g. legacy graph/nodes assets or the wrong asset group)
# left behind by earlier scaffold versions. Keep this set as narrow as possible
# so user-authored assets/jobs/schedules are never clobbered.
_MANAGED_FILES = {
    "defs/assets/sql_assets.py.tpl": "defs/assets/sql_assets.py",
}


def _render_template(template_rel: str, asset_dag: Path, workspace_name: str) -> str:
    """Read a template and substitute the workspace placeholder."""
    template = _TEMPLATE_DIR / template_rel
    try:
        content = template.read_text(encoding="utf-8")
    except OSError as e:
        raise CodeLocationLoadError(
            str(asset_dag), detail=f"Scaffold template unreadable: {template} ({e})"
        ) from e
    return content.replace(_WORKSPACE_PLACEHOLDER, workspace_name)


def ensure_asset_dag_scaffold(asset_dag: Path, workspace_name: str) -> None:
    """Ensure ``asset_dag`` contains the canonical skeleton.

    User-editable files are rendered only when missing (existing edits survive).
    Managed bridge files (see ``_MANAGED_FILES``) are force-overwritten whenever
    their content drifts from the template. Raises CodeLocationLoadError if a
    template is unreadable.
    """
    asset_dag.mkdir(parents=True, exist_ok=True)
    rendered: list[str] = []
    overwritten: list[str] = []

    for template_rel, target_rel in _SCAFFOLD_FILES.items():
        target = asset_dag / target_rel
        is_managed = template_rel in _MANAGED_FILES

        if target.exists():
            if not is_managed:
                # User-editable file already present: never touch it.
                continue
            # Managed file: overwrite only when content drifts (idempotent).
            desired = _render_template(template_rel, asset_dag, workspace_name)
            try:
                current = target.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current == desired:
                continue
            target.write_text(desired, encoding="utf-8")
            overwritten.append(target_rel)
            continue

        # Missing file: render it from the template.
        content = _render_template(template_rel, asset_dag, workspace_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        rendered.append(target_rel)

    if rendered:
        logger.info("asset_dag_scaffold_rendered", path=str(asset_dag), files=rendered)
    if overwritten:
        logger.info(
            "asset_dag_scaffold_managed_overwritten",
            path=str(asset_dag),
            files=overwritten,
        )

