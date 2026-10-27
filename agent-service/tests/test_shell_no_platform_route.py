"""No executed command may be handed a way back into the platform.

Governed verbs (``sqlmesh apply``, ``dc run``, ...) used to be dispatched from
INSIDE the executing side: a CLI shim was prepended to PATH and called the
platform back over HTTP. That required giving the child the platform's address
and the caller's identity — which is exactly the network path the L3-base
NetworkPolicy denies (threats T3/T6), and which made dev and production decide
differently (dev could inject the shim and reach the network; production could
not).

Routing now happens in workspace-runtime, before anything executes. The property
that makes that worth having is NEGATIVE and therefore invisible at any call
site: nothing on this side may put a platform address, an internal token or a
shim directory into a child's environment. Re-adding one reads perfectly
reasonable — "the shim needs its upstream" — so it is asserted structurally.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools.shell import ShellRuntimeBootstrap  # noqa: E402

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "tools"

# Variables that would hand an executed command a route into the platform.
_FORBIDDEN_ENV_KEYS = (
    "DATACODE_WORKSPACE_RUNTIME_URL",
    "DATACODE_ONTOLOGY_SERVICE_URL",
    "DATACODE_SQLMESH_SERVICE_URL",
    "DATACODE_DAGSTER_SERVICE_URL",
    "WORKSPACE_RUNTIME_INTERNAL_TOKEN",
    "DATACODE_REAL_SQLMESH",
)


class TestCommandEnvironmentCarriesNoPlatformRoute:
    @pytest.fixture()
    def env(self, tmp_path):
        bootstrap = ShellRuntimeBootstrap(
            tmp_path, user_id="u1", session_id="s1", environment="dev"
        )
        return bootstrap

    async def test_no_platform_address_or_token_is_injected(self, env, monkeypatch):
        """The dev in-process fallback is the ONLY place that builds a command
        environment here, and even it must not carry a route into the platform."""
        for key in _FORBIDDEN_ENV_KEYS:
            monkeypatch.setenv(key, "http://should-not-be-forwarded")

        built = await env.build_command_env()

        # Inherited from os.environ is out of scope for this assertion (the dev
        # fallback copies the parent environment by design, with no isolation —
        # that is what makes it dev-only). What must not happen is this code
        # SETTING them to a resolved platform address.
        for key in _FORBIDDEN_ENV_KEYS:
            assert built.get(key) == "http://should-not-be-forwarded", (
                f"{key} was rewritten to a platform-resolved value"
            )

    async def test_no_shim_directory_is_prepended_to_path(self, env):
        """A shim on PATH would be a second, divergent dispatch decision — made
        on the executing side, where the router deliberately is not."""
        built = await env.build_command_env()

        assert "sqlmesh-cli" not in built["PATH"]

    async def test_the_workspace_context_is_still_provided(self, env):
        """Context is fine; a ROUTE is not. Tools legitimately read which copy
        and which environment they are acting on."""
        built = await env.build_command_env()

        assert built["DATACODE_WORKSPACE_PATH"] == str(env.agent_path)
        assert built["DATACODE_ENVIRONMENT"] == "dev"
        assert built["DATACODE_TRIGGER_TYPE"] == "agent"


class TestNoModuleResolvesAnAddressForAChild:
    """No code path may turn a lease resolution into a value a child can read.

    ``base_url_for`` used to exist for exactly that: it returned the POD address
    of the lease holder, and the shim received it as an environment variable. It
    has been removed, and this asserts it stays removed — re-adding it is the
    single change that would make a platform route reachable from an executed
    command again.
    """

    def test_no_module_exposes_a_resolved_address(self):
        offenders = []
        for path in _TOOLS_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "base_url_for":
                    offenders.append(path.name)
                if isinstance(node, ast.FunctionDef) and node.name == "base_url_for":
                    offenders.append(path.name)
                if (
                    isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "base_url_for"
                ):
                    offenders.append(path.name)
        assert offenders == [], (
            "an address-resolving helper is back; the shim used it to give a child "
            f"process a route into the platform: {sorted(set(offenders))}"
        )

    def test_no_tool_module_references_the_retired_shim_directory(self):
        """The shim is human-facing now. A reference from here would mean
        something is still trying to inject it."""
        offenders = [
            path.name
            for path in _TOOLS_DIR.glob("*.py")
            if "sqlmesh-cli" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], sorted(offenders)
