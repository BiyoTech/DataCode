"""Every third-party package the SCAFFOLD imports must be installed here.

The defect this pins is structural: the asset-dag project the platform renders
into every working copy is user code that runs on THIS service's interpreter —
code locations deliberately share one interpreter rather than getting a
per-workspace venv (see ``CodeLocationManager._spawn_grpc``). A package the
scaffold imports is therefore a service dependency, whether or not anybody
remembered to declare it.

It is also an unusually quiet one to get wrong. ``daft`` is imported INSIDE the
asset body, so unlike a module-level import it does not stop the code location
from loading: the location reports healthy, the asset appears in the DAG, and
only materializing it raises ``ModuleNotFoundError``. Nothing in the startup
path, the health endpoint or the scaffold log notices — the template's own
docstring promises the asset "runs out of the box" while it cannot.

So the check has to walk the templates and ask, rather than trust a list:
an import added to a scaffold file that nobody added to pyproject.toml turns
this test red at the next run.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "src" / "templates"

# Imports that resolve to the rendered project itself, not to a distribution.
# ``defs`` is the asset-dag's auto-discovery package: it exists only after the
# scaffold is rendered into a working copy, which is exactly where this test
# does not run from.
LOCAL_MODULES = frozenset({"defs", "tests"})


def _third_party_imports(path: Path) -> set[str]:
    """Top-level distribution names imported anywhere in ``path``.

    Walks the whole tree rather than reading module-level statements only: an
    import buried in a function body is no less of a dependency, and it is the
    one shape that fails silently (see the module docstring).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return {
        name
        for name in names
        if name not in LOCAL_MODULES and name not in sys.stdlib_module_names
    }


def _scaffold_templates() -> list[Path]:
    return sorted(TEMPLATE_ROOT.rglob("*.py.tpl"))


def test_the_scaffold_renders_python() -> None:
    """Guard for the scan itself: no templates means nothing was checked."""
    assert _scaffold_templates(), f"no scaffold templates found under {TEMPLATE_ROOT}"


@pytest.mark.parametrize("template", _scaffold_templates(), ids=lambda p: p.name)
def test_scaffold_imports_are_installed(template: Path) -> None:
    missing = [
        name
        for name in sorted(_third_party_imports(template))
        if importlib.util.find_spec(name) is None
    ]
    assert not missing, (
        f"{template.name} imports {missing}, which is not installed in this "
        f"service's interpreter. Code locations run on this interpreter, so a "
        f"scaffold dependency must be declared in pyproject.toml — otherwise "
        f"the rendered asset fails only when a user materializes it."
    )


def test_the_daft_example_asset_runs() -> None:
    """Behavioural: the scaffold's promise, executed.

    The template claims the asset "runs out of the box", so prove it by running
    the body rather than only checking that ``import daft`` resolves — a
    distribution can import fine and still fail on the API the template uses.
    """
    source = (TEMPLATE_ROOT / "defs" / "assets" / "daft_assets.py.tpl").read_text(
        encoding="utf-8"
    )
    # Render the only placeholder the scaffold has, then take the asset's body.
    module = ast.parse(source.replace("{{ workspace_name }}", "ws-test"))
    def _is_asset(node: ast.AST) -> bool:
        for decorator in getattr(node, "decorator_list", []):
            # The decorator is applied with arguments (@asset(group_name=...)),
            # so it parses as a Call around the name.
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Name) and target.id == "asset":
                return True
        return False

    asset_fn = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and _is_asset(node)
    )
    # Strip the decorators only: the module keeps its own imports and the
    # function keeps its parameter, so what runs is the template as written
    # rather than a reconstruction of it.
    asset_fn.decorator_list = []
    namespace: dict = {}
    exec(  # noqa: S102 - executing the platform's own scaffolded template
        compile(
            ast.fix_missing_locations(module),
            filename="<daft_assets scaffold>",
            mode="exec",
        ),
        namespace,
    )

    result = namespace["example_features"](
        type("Ctx", (), {"log": type("Log", (), {"info": staticmethod(lambda *a: None)})()})()
    )

    assert result.metadata["row_count"] == 3
