"""``ensure`` reports dependency drift; it must never install.

``ensure`` sits on the asset page's load path (``loadGraph -> ensureDeployment``),
so it is called on every page open, every tab switch and every graph refresh. An
install there would be a multi-minute operation nobody asked for, behind a
request the UI expects to answer instantly — and it would run without the
progress bar and the failure reporting that make an install comprehensible.

Two independent assertions, because either one alone can be defeated:

* BEHAVIOURAL — drive the real endpoint against a copy with uninstalled
  dependencies and assert the sandbox was never asked for anything;
* STRUCTURAL — walk the AST of the ensure path and assert it does not reach the
  installer at all. This is what survives a future refactor that adds a
  convenient "and install it while we are here" call.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_RUNTIME_SRC = Path(__file__).resolve().parents[1] / "src"

# The installer's entry points. Reaching any of them from the ensure path is the
# defect this test exists to prevent.
_INSTALL_NAMES = frozenset({"sync", "op_deps_sync", "submit"})


class _FakeRuntime:
    host = "127.0.0.1"
    port = 4001
    definitions = "/copy/asset-dag/definitions.py"
    alive = True


class _FakeCodeLocations:
    async def ensure(self, spec):
        return _FakeRuntime()

    async def reload(self, spec):
        return _FakeRuntime()

    def load_failure(self, name):
        return {}

    def get(self, name):
        return None

    def port_of(self, name):
        return 0


class _RefusingSubmitter:
    """Any call is a test failure: ensure must not submit a task."""

    async def submit(self, kind, workspace_path, **kwargs):
        raise AssertionError(
            f"ensure reached the sandbox plane (kind={kind!r}); it must only "
            "report dependency state, never install"
        )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from src.api.dagster import deployments as api
    from src.codeloc import deployments as domain
    from src.config import settings
    from src.deps import service as deps_service

    monkeypatch.setattr(
        "src.api.deps.validate_workspace_path", lambda p: p, raising=False
    )
    monkeypatch.setattr(settings, "workspace_base_path", str(tmp_path / "base"))
    monkeypatch.setattr(settings, "deps_index_url", "https://index.invalid/simple")
    monkeypatch.setattr(
        domain, "_deployment_manager", domain.DeploymentManager(
            code_locations=_FakeCodeLocations()
        )
    )
    monkeypatch.setattr(
        deps_service,
        "_service",
        deps_service.DepsService(submitter=_RefusingSubmitter()),
    )

    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def _copy(tmp_path: Path, requirements: str) -> str:
    copy = tmp_path / "base" / "t1" / "ws-t1-shop" / "u1" / "envs" / "dev"
    (copy / "asset-dag").mkdir(parents=True)
    (copy / "asset-dag" / "pyproject.toml").write_text(
        f'[project]\nname = "assets"\ndependencies = [{requirements}]\n',
        encoding="utf-8",
    )
    return str(copy)


# ── behaviour ─────────────────────────────────────────────────────────────


def test_ensure_reports_missing_dependencies_without_installing(client, tmp_path):
    path = _copy(tmp_path, '"polars>=1.0"')

    body = client.post(
        "/api/v1/dagster/deployments/ensure", json={"workspace_path": path}
    ).json()

    assert body["deps"]["state"] == "missing"
    assert body["deps"]["declared"] == 1
    assert body["deps"]["installed"] == 0
    # The message is what the UI shows next to the reload button.
    assert "not installed" in body["deps"]["message"]
    # No overlay was created: ensure touched nothing.
    assert not (Path(path) / ".datacode" / "deps").exists()


def test_ensure_reports_an_unreadable_declaration_as_its_own_state(client, tmp_path):
    """The user's next action is to fix the file, not to press reload."""
    path = _copy(tmp_path, '"not a requirement=="')

    body = client.post(
        "/api/v1/dagster/deployments/ensure", json={"workspace_path": path}
    ).json()

    assert body["deps"]["state"] == "declaration_invalid"
    assert "pyproject.toml" in body["deps"]["message"]


def test_ensure_on_a_copy_with_no_declaration_is_empty_not_an_error(client, tmp_path):
    path = _copy(tmp_path, "")

    body = client.post(
        "/api/v1/dagster/deployments/ensure", json={"workspace_path": path}
    ).json()

    assert body["deps"]["state"] == "empty"
    assert body["alive"] is True


# ── structure ─────────────────────────────────────────────────────────────


def _called_names(node: ast.AST) -> set[str]:
    """Every attribute/function name called anywhere under ``node``."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"{name} not found in {path}")


def test_the_ensure_endpoint_does_not_reach_the_installer():
    endpoint = _function(
        _RUNTIME_SRC / "api" / "dagster" / "deployments.py", "ensure_deployment"
    )
    assert not (_called_names(endpoint) & _INSTALL_NAMES)


def test_the_ensure_domain_method_only_inspects():
    """``inspect`` is the read-only entry point; ``sync`` is the installing one."""
    method = _function(_RUNTIME_SRC / "codeloc" / "deployments.py", "ensure")
    called = _called_names(method)

    assert "inspect" in called, "ensure must report dependency state"
    assert not (called & _INSTALL_NAMES)


def test_the_read_only_status_path_has_no_installer_calls():
    """Everything ``inspect`` transitively uses stays read-only."""
    for name in ("read_status", "read_manifest", "read_declaration"):
        function = _function(_RUNTIME_SRC / "deps" / "manifest.py", name)
        assert not (_called_names(function) & _INSTALL_NAMES)
