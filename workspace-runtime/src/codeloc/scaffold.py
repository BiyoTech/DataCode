"""Canonical asset-dag project scaffold rendering.

Renders the templates under ``src/templates/`` into a working copy's
``asset-dag/`` directory. Rendering is fill-missing for user-editable files
(existing files are never overwritten, so user edits survive), with one
exception: a small set of *managed* platform files are force-overwritten whenever
their content drifts from the template. That keeps the SQLMesh bridge
(``defs/assets/sql_assets.py``) and the deployment gate
(``defs/deployment.py``) authoritative, so their behavior can evolve without
stale on-disk copies lingering — and so the gate deciding whether this
environment's schedules may run cannot be edited into always-on.

Calling this on every deployment ensure is safe and idempotent.

Canonical layout (modern Dagster ``dg`` auto-discovery):

    asset-dag/                  # code location working dir (NOT a package)
    ├── definitions.py          # entrypoint: load_defs(defs_root=defs)
    ├── pyproject.toml          # metadata; [tool.dagster] module_name
    ├── README.md               # layout + conventions
    ├── defs/                   # auto-discovery root (importable package)
    │   ├── __init__.py
    │   ├── deployment.py       # MANAGED: deployment identity + schedule gate
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

from src.codeloc.errors import CodeLocationLoadError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Relative location of the Dagster project inside a workspace.
ASSET_DAG_SUBDIR = "asset-dag"

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_WORKSPACE_PLACEHOLDER = "{{ workspace_name }}"

# Placeholders substituted from the PLATFORM BASELINE rather than from a
# hand-written list. The scaffolded declaration used to carry literal bounds
# (`dagster>=1.8.0`), which drift the moment the image is upgraded: the file
# then documents a version the platform no longer has, and a user copying that
# style writes bounds that the sync refuses. Generating them means the scaffold
# cannot describe a platform that does not exist.
_PYTHON_VERSION_PLACEHOLDER = "{{ python_version }}"
_PLATFORM_DEPENDENCIES_PLACEHOLDER = "{{ platform_dependencies }}"

# What the scaffold names explicitly, in the order a reader wants them. The full
# baseline is several hundred distributions; listing all of them in every
# working copy's pyproject.toml would bury the user's own declarations. These
# are the ones the generated assets actually import.
_SCAFFOLD_BASELINE_NAMES = ("dagster", "getdaft", "httpx")

# Template path (relative to _TEMPLATE_DIR) -> rendered path (relative to
# asset-dag/). Every file, including each package ``__init__.py``, has a
# template, so rendering is a single uniform pass.
_SCAFFOLD_FILES = {
    "definitions.py.tpl": "definitions.py",
    "pyproject.toml.tpl": "pyproject.toml",
    "README.md.tpl": "README.md",
    # defs/ auto-discovery root
    "defs/__init__.py.tpl": "defs/__init__.py",
    "defs/deployment.py.tpl": "defs/deployment.py",
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

# Managed platform files (subset of _SCAFFOLD_FILES) that dagster-service owns and
# keeps authoritative. Unlike user-editable files, these are force-overwritten
# whenever the on-disk content drifts from the rendered template. This eliminates
# stale bridge code (e.g. legacy graph/nodes assets or the wrong asset group)
# left behind by earlier scaffold versions, and keeps the deployment gate — the
# module that decides whether this environment's schedules may run at all — from
# being edited into always-on. Keep this set as narrow as possible so
# user-authored assets/jobs/schedules are never clobbered.
_MANAGED_FILES = {
    "defs/assets/sql_assets.py.tpl": "defs/assets/sql_assets.py",
    "defs/deployment.py.tpl": "defs/deployment.py",
}


def _platform_dependency_lines() -> str:
    """The platform-provided block of a scaffolded declaration.

    Generated from the running image's baseline, so every bound names a version
    that is genuinely installed. A name missing from the baseline is simply
    omitted: a scaffold that declared a package the platform does not have would
    produce a working copy whose first reload fails.
    """
    from src.deps.baseline import platform_baseline

    baseline = platform_baseline()
    lines = [
        f'    "{name}>={baseline[name]}",'
        for name in _SCAFFOLD_BASELINE_NAMES
        if baseline.get(name)
    ]
    return "\n".join(lines)


def render_placeholders(content: str, workspace_name: str) -> str:
    """Substitute every scaffold placeholder in one template's text.

    Public because the creation-time skeleton (``src/files/asset_dag_skeleton``)
    writes the same files from inline constants and must substitute them
    identically — two substitution rules would mean a workspace's declaration
    differed depending on which path created it.
    """
    from datacode_common.python_deps import interpreter_python_version

    content = content.replace(_WORKSPACE_PLACEHOLDER, workspace_name)
    if _PLATFORM_DEPENDENCIES_PLACEHOLDER in content:
        content = content.replace(
            _PLATFORM_DEPENDENCIES_PLACEHOLDER, _platform_dependency_lines()
        )
    if _PYTHON_VERSION_PLACEHOLDER in content:
        content = content.replace(
            _PYTHON_VERSION_PLACEHOLDER, interpreter_python_version()
        )
    return content


def _render_template(template_rel: str, asset_dag: Path, workspace_name: str) -> str:
    """Read a template and substitute its placeholders."""
    template = _TEMPLATE_DIR / template_rel
    try:
        content = template.read_text(encoding="utf-8")
    except OSError as e:
        raise CodeLocationLoadError(
            str(asset_dag), detail=f"Scaffold template unreadable: {template} ({e})"
        ) from e
    return render_placeholders(content, workspace_name)


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

