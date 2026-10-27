"""The platform baseline: who owns it, who sees it, when a conflict surfaces.

Three properties, each of which was a real defect before:

1. **the baseline is THIS process's interpreter, and it travels in the plan.**
   The installer runs in another image; letting it introspect its own
   interpreter installed a second dagster over the platform's and skipped
   packages the code location lacks — neither visible in development, where the
   local backend runs pip in this very process;
2. **a conflict is reported on SAVE and on READ, not only by an install.**
   Learning that `pyarrow<15` cannot work after a multi-minute reload is the
   experience this closes;
3. **the baseline is visible.** A user cannot write a compatible declaration
   against a list nobody can read, and the agent writes most declarations.

The FastAPI app is real; only the task submitter is stubbed, because starting a
sandbox is not what any of this is about.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from datacode_common.task_protocol import TaskResult, TaskStatus
from fastapi.testclient import TestClient


def _copy(tmp_path: Path, requirements: str) -> str:
    copy = tmp_path / "base" / "t1" / "ws-t1-shop" / "u1" / "envs" / "dev"
    (copy / "asset-dag").mkdir(parents=True)
    (copy / "asset-dag" / "pyproject.toml").write_text(
        f'[project]\nname = "assets"\ndependencies = [{requirements}]\n',
        encoding="utf-8",
    )
    return str(copy)


@pytest.fixture(autouse=True)
def fresh_baseline():
    """Drop the process-wide baseline cache around every test.

    The cache is correct in production — the image is immutable — but it makes
    tests order-dependent: one that substitutes a fake baseline would otherwise
    leave it installed for every test that follows.
    """
    from src.deps.baseline import reset_platform_baseline

    reset_platform_baseline()
    yield
    reset_platform_baseline()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """The real app, with the workspace base pointed at a temporary tree."""
    from src.config import settings

    monkeypatch.setattr(settings, "workspace_base_path", str(tmp_path / "base"))
    monkeypatch.setattr(settings, "deps_index_url", "https://index.invalid/simple")

    from src.main import app

    return TestClient(app)


# ── 1. the baseline belongs to this process ───────────────────────────────


def test_the_baseline_describes_this_interpreter():
    """It is the interpreter code locations load user code on."""
    from src.deps.baseline import platform_baseline

    baseline = platform_baseline()

    # Things this service declares as core dependencies.
    assert baseline.get("dagster"), "the code location server itself"
    assert baseline.get("sqlmesh")
    # The shared library whose contracts define this very module.
    assert "datacode-common" in baseline


def test_the_baseline_is_computed_once(monkeypatch):
    """Asked for on every asset-page open; a reverse lookup per call is not free."""
    from src.deps import baseline as module

    calls = []

    def _counted():
        calls.append(1)
        return {"dagster": "1.13.21"}

    monkeypatch.setattr(module, "installed_distributions", _counted)
    module.platform_baseline()
    module.platform_baseline()
    assert len(calls) == 1


def test_the_install_plan_carries_the_whole_baseline(monkeypatch, tmp_path):
    """Not only the declared names.

    The plan's baseline becomes pip's constraint set, which is what bounds
    TRANSITIVE resolution. A subset would leave exactly the packages nobody
    declared — numpy, pyarrow — free to resolve to a version that shadows the
    platform's and breaks pandas at ABI level inside a code location.
    """
    from datacode_common.python_deps import parse_declaration

    from src.config import settings
    from src.deps.baseline import platform_baseline
    from src.deps.service import DepsService

    monkeypatch.setattr(settings, "deps_index_url", "https://index.invalid/simple")
    declaration = parse_declaration(
        '[project]\ndependencies = ["polars>=1.0"]\n'
    )

    plan = DepsService._build_plan(declaration)

    assert plan.baseline == platform_baseline()
    assert len(plan.baseline) > len(plan.requirements)


@pytest.mark.asyncio
async def test_a_conflicting_declaration_never_reaches_a_sandbox(
    monkeypatch, tmp_path
):
    """Refused by the runtime: the verdict is a pure comparison it already has.

    Spending a vehicle and a pip resolve to reach the same answer would only
    make the user wait for it.
    """
    from src.config import settings
    from src.deps.baseline import platform_baseline
    from src.deps.service import DepsService

    monkeypatch.setattr(settings, "deps_index_url", "https://index.invalid/simple")
    shipped = platform_baseline()["dagster"]
    path = _copy(tmp_path, '"dagster<1.0"')

    submitted: list[str] = []

    class _Submitter:
        async def submit(self, kind, workspace_path, **kwargs):
            submitted.append(kind)
            return TaskResult(task_id="t", status=TaskStatus.SUCCEEDED, result={})

    outcome = await DepsService(submitter=_Submitter()).sync(path, source="cli")

    assert submitted == [], "no sandbox may be started for a plan that cannot work"
    assert outcome.failure_code == "deps_platform_conflict"
    # NOT the declaration code: the file is valid TOML, and reporting it as
    # unreadable sent users hunting for a syntax error that was never there.
    assert outcome.failure_code != "deps_declaration_invalid"
    assert shipped in outcome.message
    assert [item.suggested for item in outcome.conflicts] == [f"dagster>={shipped}"]


# ── 2. a conflict surfaces on save and on read ────────────────────────────


def test_saving_the_declaration_reports_a_conflict_immediately(client, tmp_path):
    """The whole check is a parse plus a comparison — microseconds.

    Deferring it to a reload is what made the user learn about `pyarrow<15`
    after a full install cycle.
    """
    path = _copy(tmp_path, "")

    response = client.post(
        "/api/v1/files/write",
        json={
            "workspace_path": path,
            "file_path": "asset-dag/pyproject.toml",
            "content": '[project]\ndependencies = ["dagster<1.0"]\n',
        },
    )

    assert response.status_code == 200
    body = response.json()
    # The save ALWAYS succeeds: the file is the user's, and an intermediate edit
    # is legitimately broken.
    assert body["success"] is True
    assert body["deps"]["valid"] is False
    assert body["deps"]["conflicts"][0]["name"] == "dagster"
    assert body["deps"]["conflicts"][0]["suggested"].startswith("dagster>=")
    # And the file really was written.
    assert "dagster<1.0" in Path(path, "asset-dag", "pyproject.toml").read_text()


def test_saving_a_compatible_declaration_reports_valid(client, tmp_path):
    path = _copy(tmp_path, "")

    body = client.post(
        "/api/v1/files/write",
        json={
            "workspace_path": path,
            "file_path": "asset-dag/pyproject.toml",
            "content": '[project]\ndependencies = ["polars>=1.0"]\n',
        },
    ).json()

    assert body["deps"] == {
        "valid": True,
        "message": "",
        "conflicts": [],
        "declared": 1,
    }


def test_saving_broken_toml_reports_it_without_refusing_the_save(client, tmp_path):
    path = _copy(tmp_path, "")

    body = client.post(
        "/api/v1/files/write",
        json={
            "workspace_path": path,
            "file_path": "asset-dag/pyproject.toml",
            "content": "[project\nbroken",
        },
    ).json()

    assert body["success"] is True
    assert body["deps"]["valid"] is False
    assert "TOML" in body["deps"]["message"]


def test_saving_any_other_file_carries_no_diagnostic(client, tmp_path):
    """Every save would otherwise pay for a check that cannot apply to it."""
    path = _copy(tmp_path, "")

    body = client.post(
        "/api/v1/files/write",
        json={
            "workspace_path": path,
            "file_path": "asset-dag/defs/assets/x.py",
            "content": "x = 1\n",
        },
    ).json()

    assert "deps" not in body


def test_the_read_only_status_reports_conflicts_too(client, tmp_path):
    """A reload that cannot succeed must be visible before it is started."""
    path = _copy(tmp_path, '"dagster<1.0"')

    body = client.get("/api/v1/deps/status", params={"workspace_path": path}).json()

    assert body["conflicts"][0]["name"] == "dagster"
    # The conflict wins over "N packages are not installed yet": nothing else
    # about this copy matters until the declaration and the platform agree.
    assert "cannot be installed" in body["message"]


# ── 3. the baseline is visible ────────────────────────────────────────────


def test_the_baseline_endpoint_needs_no_workspace(client):
    """It describes the runtime image, identical for every working copy.

    Requiring a workspace would make the answer look workspace-specific and
    send the request through lease routing for nothing.
    """
    response = client.get("/api/v1/deps/baseline")

    assert response.status_code == 200
    body = response.json()
    names = {item["name"] for item in body["packages"]}
    assert "dagster" in names
    # The interpreter the overlay must be built for is the other half of a
    # compatible declaration.
    assert body["python_version"]
    assert body["platform_tag"]


def test_every_baseline_entry_carries_the_string_to_declare(client):
    """So no client re-derives the rule that a bound is >= and not ==."""
    body = client.get("/api/v1/deps/baseline").json()

    for item in body["packages"][:20]:
        assert item["suggested"] == f"{item['name']}>={item['version']}"


def test_the_highlighted_subset_is_what_user_code_imports(client):
    """Several hundred distributions is a list nobody reads."""
    body = client.get("/api/v1/deps/baseline").json()

    highlighted = {item["name"] for item in body["highlighted"]}
    assert {"dagster", "sqlmesh", "pandas", "pyarrow"} <= highlighted
    assert len(body["highlighted"]) < len(body["packages"])


def test_the_cli_renders_the_baseline_as_a_declaration_reference():
    """The agent writes most declarations; this is what it reads first."""
    from src.tools import command_render as render
    from src.tools.deps_commands import _baseline_payload

    payload = _baseline_payload(full=False)
    text = render.deps_baseline(payload, as_json=False)

    assert "dagster" in text
    assert "--all" in text, "the curated view must say how to see everything"
    assert payload["truncated"] is True
    assert payload["total"] > len(payload["packages"])


def test_the_cli_status_names_the_edit_to_make(tmp_path):
    """A rewrite per line, not a paragraph: the agent acts on this."""
    from src.tools import command_render as render

    text = render.deps_status(
        {
            "state": "missing",
            "declared": 1,
            "installed": 0,
            "conflicts": [
                {
                    "requirement": "pyarrow<15.0.0,>=14.0.0",
                    "name": "pyarrow",
                    "platform_version": "25.0.1",
                    "suggested": "pyarrow>=25.0.1",
                }
            ],
        },
        as_json=False,
    )

    assert "pyarrow<15.0.0,>=14.0.0" in text
    assert "pyarrow>=25.0.1" in text


def test_the_status_payload_is_machine_readable(tmp_path):
    """`dc deps status --json` is what an agent parses."""
    from src.deps.manifest import read_status

    payload = read_status(_copy(tmp_path, '"dagster<1.0"')).to_dict()

    assert json.loads(json.dumps(payload))["conflicts"][0]["suggested"].startswith(
        "dagster>="
    )


# ── the scaffolded declaration is generated, not hand-written ─────────────


def test_the_scaffold_declares_versions_the_platform_really_ships(tmp_path):
    """A literal bound in the template drifts the moment the image is upgraded.

    The file would then document a version the platform no longer has, and a
    user copying that style writes bounds the sync refuses.
    """
    from datacode_common.python_deps import parse_declaration

    from src.codeloc.scaffold import ensure_asset_dag_scaffold
    from src.deps.baseline import conflicts_for, platform_baseline

    asset_dag = tmp_path / "asset-dag"
    ensure_asset_dag_scaffold(asset_dag, "shop")

    declaration = parse_declaration(
        (asset_dag / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert declaration.requirements, "the scaffold documents what it was written against"
    # The decisive assertion: a freshly created working copy can always install.
    assert conflicts_for(declaration) == ()
    shipped = platform_baseline()["dagster"]
    assert f"dagster>={shipped}" in declaration.requirements


def test_both_scaffold_paths_write_the_same_declaration(tmp_path):
    """Creation time and code-location ensure are two writers of one file.

    A workspace's declaration must not depend on which of them produced it.
    """
    from src.codeloc.scaffold import ensure_asset_dag_scaffold
    from src.files.asset_dag_skeleton import scaffold_asset_dag

    from_template = tmp_path / "a" / "asset-dag"
    ensure_asset_dag_scaffold(from_template, "shop")
    from_skeleton = tmp_path / "b" / "asset-dag"
    scaffold_asset_dag(str(from_skeleton), "shop")

    def _deps(path):
        from datacode_common.python_deps import parse_declaration

        return parse_declaration(
            (path / "pyproject.toml").read_text(encoding="utf-8")
        ).requirements

    assert _deps(from_template) == _deps(from_skeleton)


# ── which pins this deployment is willing to yield ────────────────────────
#
# Pinning the whole baseline gives every one of several hundred distributions a
# veto over every declaration. These tests are about the layer that decides
# which of those vetoes is worth keeping, derived from the real installed tree
# rather than from a list somebody maintains.


def test_the_yieldable_set_excludes_the_platforms_own_runtime():
    """Derived against the real image, because that is what it describes."""
    from src.deps.baseline import platform_baseline, relaxable_packages

    yieldable = relaxable_packages()
    baseline = platform_baseline()

    assert yieldable, "a deployment where nothing may yield refuses valid installs"
    assert yieldable < set(baseline), "yielding is a subset, never the whole baseline"
    # The packages whose replacement breaks the platform rather than the
    # workspace. dagster and sqlmesh are its machinery; numpy and pyarrow are
    # ABI-bound, and the compiled-extension rule has to catch them without
    # anyone having listed them.
    for name in ("dagster", "sqlmesh", "pydantic", "pydantic-core"):
        assert name not in yieldable, name
    for name in ("numpy", "pyarrow"):
        if name in baseline:
            assert name not in yieldable, f"{name} ships a compiled extension"


def test_an_operator_can_allow_a_specific_package_to_be_overridden(monkeypatch):
    """The escape hatch a failed install names, and its counterpart."""
    from src.config import settings
    from src.deps import baseline as module

    monkeypatch.setattr(
        module,
        "installed_distributions",
        lambda: {"tenacity": "9.1.4", "numpy": "2.5.2", "dagster": "1.13.21"},
    )
    monkeypatch.setattr(module, "compiled_distributions", lambda names: {"numpy"})

    monkeypatch.setattr(settings, "deps_protected_packages", "tenacity")
    monkeypatch.setattr(settings, "deps_relaxable_packages", "")
    module.reset_platform_baseline()
    assert module.relaxable_packages() == frozenset()

    # Re-admission is applied last, so it also reaches a compiled package: a
    # deployment that has decided this is safe must be able to say so without a
    # platform release.
    monkeypatch.setattr(settings, "deps_relaxable_packages", "numpy")
    module.reset_platform_baseline()
    assert module.relaxable_packages() == frozenset({"numpy"})


def test_the_yieldable_set_is_computed_once(monkeypatch):
    """Walking every distribution's file list is not a per-request cost."""
    from src.deps import baseline as module

    calls: list[int] = []

    def _counted(names):
        calls.append(1)
        return set()

    monkeypatch.setattr(module, "installed_distributions", lambda: {"tenacity": "9.1.4"})
    monkeypatch.setattr(module, "compiled_distributions", _counted)

    module.relaxable_packages()
    module.relaxable_packages()
    assert len(calls) == 1


def test_the_install_plan_carries_the_yieldable_set(monkeypatch):
    """The installer cannot derive it: it runs in an image without the baseline.

    An empty one means "nothing may yield", which is the fail-closed direction
    and also the pre-existing behaviour — so a version skew degrades to
    refusing an install rather than to overwriting a platform package.
    """
    from datacode_common.python_deps import parse_declaration

    from src.config import settings
    from src.deps.baseline import relaxable_packages
    from src.deps.service import DepsService

    monkeypatch.setattr(settings, "deps_index_url", "https://index.invalid/simple")
    plan = DepsService._build_plan(
        parse_declaration('[project]\ndependencies = ["polars>=1.0"]\n')
    )

    assert plan.relaxable == relaxable_packages()
    assert "dagster" not in plan.relaxable
    # Survives the trip through task parameters, which is the only path it has.
    from datacode_common.python_deps import InstallPlan

    assert InstallPlan.from_params(plan.to_params()).relaxable == plan.relaxable


def test_source_builds_are_off_until_an_operator_names_a_package(monkeypatch):
    """The wheel-only rule is the default, and the exception is per package.

    A working copy can never ask for a source build — the permission is read from
    PLATFORM configuration and carried in the plan, the same provenance rule the
    index URL follows, because a declaration that could widen it would be a way
    to run a build script from inside the code under review.
    """
    from datacode_common.python_deps import InstallPlan, parse_declaration

    from src.config import settings
    from src.deps.service import DepsService

    monkeypatch.setattr(settings, "deps_index_url", "https://index.invalid/simple")
    declaration = parse_declaration('[project]\ndependencies = ["pyspark>=3.5.0"]\n')

    monkeypatch.setattr(settings, "deps_allow_source_packages", "")
    assert DepsService._build_plan(declaration).source_allowed == frozenset()

    # Canonicalised, so the config file's spelling cannot miss the requirement's.
    monkeypatch.setattr(settings, "deps_allow_source_packages", "PySpark, other")
    plan = DepsService._build_plan(declaration)
    assert plan.source_allowed == {"pyspark", "other"}
    # Survives the only path it has: task parameters.
    assert InstallPlan.from_params(plan.to_params()).source_allowed == plan.source_allowed


# ── a yielded pin is visible afterwards ───────────────────────────────────


def _ready_manifest(path: str, relaxed: list[dict]) -> None:
    from datacode_common.python_deps import (
        DepsManifest,
        interpreter_platform_tag,
        interpreter_python_version,
    )

    from src.deps.manifest import read_declaration, write_manifest

    write_manifest(
        path,
        DepsManifest(
            state="ready",
            fingerprint=read_declaration(path).fingerprint,
            packages=({"name": "feast", "version": "0.65.0"},),
            relaxed=tuple(relaxed),
            python_version=interpreter_python_version(),
            platform_tag=interpreter_platform_tag(),
        ),
    )
    Path(path, ".datacode", "deps", "site-packages").mkdir(parents=True, exist_ok=True)


def test_the_read_only_status_reports_an_overridden_platform_package(
    client, tmp_path
):
    """The override outlives the install, so the READ path has to show it.

    Whoever debugs "why does only this workspace behave like that" months later
    did not watch the install that caused it.
    """
    path = _copy(tmp_path, '"feast>=0.60.0"')
    _ready_manifest(
        path,
        [
            {
                "name": "tenacity",
                "platform_version": "9.1.4",
                "overlay_version": "8.5.0",
                "required_by": "feast",
            }
        ],
    )

    body = client.get("/api/v1/deps/status", params={"workspace_path": path}).json()

    assert body["state"] == "ready"
    assert body["relaxed"] == [
        {
            "name": "tenacity",
            "platform_version": "9.1.4",
            "overlay_version": "8.5.0",
            "required_by": "feast",
        }
    ]


def test_an_override_is_not_reported_once_the_tree_it_belongs_to_is_stale(
    client, tmp_path
):
    """A drifted manifest describes a tree that is no longer installed.

    Naming its overrides would report versions that are not in effect.
    """
    path = _copy(tmp_path, '"feast>=0.60.0"')
    _ready_manifest(
        path,
        [
            {
                "name": "tenacity",
                "platform_version": "9.1.4",
                "overlay_version": "8.5.0",
                "required_by": "feast",
            }
        ],
    )
    # The user edits the declaration: the manifest's fingerprint no longer
    # matches, so the tree it describes is not the tree that is wanted.
    Path(path, "asset-dag", "pyproject.toml").write_text(
        '[project]\ndependencies = ["feast>=0.61.0"]\n', encoding="utf-8"
    )

    body = client.get("/api/v1/deps/status", params={"workspace_path": path}).json()

    assert body["state"] == "drifted"
    assert body["relaxed"] == []


def test_the_cli_status_names_the_overridden_package(tmp_path):
    """stdout is all an agent sees; an override nowhere in it is invisible."""
    from src.tools import command_render as render

    text = render.deps_status(
        {
            "state": "ready",
            "declared": 1,
            "installed": 12,
            "relaxed": [
                {
                    "name": "tenacity",
                    "platform_version": "9.1.4",
                    "overlay_version": "8.5.0",
                    "required_by": "feast",
                }
            ],
        },
        as_json=False,
    )

    assert "tenacity" in text
    assert "9.1.4" in text and "8.5.0" in text
    assert "feast" in text
