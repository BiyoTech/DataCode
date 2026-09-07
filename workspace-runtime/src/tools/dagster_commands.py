"""``dc`` subcommands — the Dagster data plane, served in-process (design 04 §3.6).

Same principle as :mod:`src.tools.ontology_commands`: the CLI spelling calls the
route handlers, not a private path of its own, so ``dc deployment status`` and
the UI's deployment panel can never disagree about what "ensure" does.

``environment`` is passed as a CONFIRMATION where the handler accepts one — the
working copy stays authoritative and a mismatch is rejected by the backend. This
is the same anti-hallucination contract the shim had, except the value now comes
from the caller's validated request instead of an environment variable a sandbox
could rewrite.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from src.tools import command_render as render
from src.tools.command_args import has_flag, parse_job_run_args
from src.utils.logging import get_logger

logger = get_logger(__name__)

_EXIT_OK = 0
_EXIT_USAGE = 1

_DEPLOYMENT_USAGE = "Usage: dc deployment {status|reload}"


async def run_assets_command(
    argv: Sequence[str], *, workspace_path: str, environment: str
) -> dict[str, Any]:
    """``dc assets [--json]`` — the asset dependency graph of this environment."""
    from src.api.dagster.assets import asset_graph

    graph = await asset_graph(
        workspace_path=workspace_path,
        repository_name="__repository__",
        environment=environment,
    )
    return render.result(
        _EXIT_OK,
        stdout=render.asset_graph(
            _as_dict(graph), as_json=has_flag(argv, "--json")
        ),
    )


async def run_deployment_command(
    argv: Sequence[str], *, workspace_path: str, environment: str
) -> dict[str, Any]:
    """``dc deployment status|reload``.

    ``status`` maps to ``ensure``, which is idempotent: an already-running code
    location is not respawned (that would kill in-flight queries), only its
    policy is refreshed. Safe for a read.
    """
    tokens = list(argv)
    subcommand = tokens[0] if tokens else ""

    if subcommand == "status":
        action = "ensure"
    elif subcommand == "reload":
        action = "reload"
    else:
        return render.result(
            _EXIT_USAGE,
            stderr=(
                f"[datacode] unknown deployment subcommand: "
                f"'{subcommand or '<empty>'}'\n{_DEPLOYMENT_USAGE}"
            ),
        )

    from src.api.dagster.deployments import ensure_deployment, reload_deployment
    from src.api.dagster.requests import DeploymentRequest

    request = DeploymentRequest(
        workspace_path=workspace_path,
        # Confirmation only. env_type / schedules_enabled stay unset: they carry
        # the environment registry's policy and default safely server-side.
        environment=environment,
    )
    handler = ensure_deployment if action == "ensure" else reload_deployment
    info = await handler(request)
    return render.result(
        _EXIT_OK,
        stdout=render.deployment(
            _as_dict(info), action=action, as_json=has_flag(tokens, "--json")
        ),
    )


async def run_job_command(
    argv: Sequence[str], *, workspace_path: str
) -> dict[str, Any]:
    """``dc run <job_name> [--config-json J]``."""
    args = parse_job_run_args(argv)
    if not args.job_name:
        return render.result(
            _EXIT_USAGE,
            stderr=(
                "[datacode] job_name is required.\n"
                "Usage: dc run <job_name> [--config-json '<json>']"
            ),
        )

    run_config = None
    if args.config_json:
        try:
            run_config = json.loads(args.config_json)
        except json.JSONDecodeError as exc:
            return render.result(
                _EXIT_USAGE,
                stderr=f"[datacode] --config-json is not valid JSON: {exc}",
            )

    from src.api.dagster.pipelines import trigger_run
    from src.api.dagster.requests import TriggerRunRequest

    info = await trigger_run(
        TriggerRunRequest(
            workspace_path=workspace_path,
            job_name=args.job_name,
            run_config=run_config,
        )
    )
    return render.result(
        _EXIT_OK,
        stdout=render.job_run(_as_dict(info), job_name=args.job_name),
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """Normalize a pydantic response model to a plain dict for rendering."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["run_assets_command", "run_deployment_command", "run_job_command"]
