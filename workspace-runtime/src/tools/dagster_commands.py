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

import asyncio
import json
from typing import Any, Sequence

from src.tools import command_render as render
from src.tools.command_args import has_flag, parse_job_run_args
from src.utils.logging import get_logger

logger = get_logger(__name__)

_EXIT_OK = 0
_EXIT_USAGE = 1
_EXIT_BACKEND_ERROR = 2

_DEPLOYMENT_USAGE = "Usage: dc deployment {status|reload|deps}"

# How long the CLI waits for a reload. Above the dependency install budget
# (deps.install_timeout_seconds) plus the code-location start, so a legitimate
# first install finishes inside it; the job itself is bounded by the sandbox, so
# this only decides how long the CALLER waits.
_RELOAD_WAIT_SECONDS = 1200.0
_RELOAD_POLL_SECONDS = 0.5


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
    argv: Sequence[str], *, workspace_path: str, environment: str, user_id: str = ""
) -> dict[str, Any]:
    """``dc deployment status|reload``.

    ``status`` maps to ``ensure``, which is idempotent: an already-running code
    location is not respawned (that would kill in-flight queries), only its
    policy is refreshed. Safe for a read.

    ``reload`` is asynchronous on the HTTP surface, but a CLI must return a
    DETERMINATE answer: an agent that reads "job accepted" has learnt nothing and
    would go on to run assets that do not exist yet. So this waits for the job to
    finish and renders the stages it passed through, exactly as if it had been
    synchronous — the difference is that no HTTP request is held open, and the same
    job can be watched from the UI while the CLI waits on it.
    """
    tokens = list(argv)
    subcommand = tokens[0] if tokens else ""

    if subcommand == "reload":
        return await _reload_to_completion(
            workspace_path,
            environment=environment,
            user_id=user_id,
            as_json=has_flag(tokens, "--json"),
        )
    if subcommand == "deps":
        # `dc deployment deps` reads the dependency state without touching the
        # code location. Same answer the asset page shows.
        from src.deps.service import get_deps_service

        status = get_deps_service().inspect(workspace_path)
        return render.result(
            _EXIT_OK,
            stdout=render.deps_status(
                status.to_dict(), as_json=has_flag(tokens, "--json")
            ),
        )
    if subcommand != "status":
        return render.result(
            _EXIT_USAGE,
            stderr=(
                f"[datacode] unknown deployment subcommand: "
                f"'{subcommand or '<empty>'}'\n{_DEPLOYMENT_USAGE}"
            ),
        )

    from src.api.dagster.deployments import ensure_deployment
    from src.api.dagster.requests import DeploymentRequest

    info = await ensure_deployment(
        DeploymentRequest(
            workspace_path=workspace_path,
            # Confirmation only. env_type / schedules_enabled stay unset: they
            # carry the environment registry's policy and default safely
            # server-side.
            environment=environment,
        )
    )
    return render.result(
        _EXIT_OK,
        stdout=render.deployment(
            _as_dict(info), action="ensure", as_json=has_flag(tokens, "--json")
        ),
    )


async def _reload_to_completion(
    workspace_path: str, *, environment: str, user_id: str, as_json: bool
) -> dict[str, Any]:
    """Start (or join) a reload job and wait for its terminal state.

    Goes through the DOMAIN manager rather than the HTTP handler, unlike the rest
    of this module: that handler takes a request-scoped caller identity, which a
    CLI invocation does not have. Everything below the handler — the job store,
    the dependency gate, the code-location vehicle — is the same code path.
    """
    from src.codeloc.deployments import get_deployment_manager
    from src.codeloc.reload_jobs import get_reload_job_store

    manager = get_deployment_manager()
    spec = manager.resolve(workspace_path, environment=environment)
    job = await manager.start_reload_job(spec, source="cli", actor_user_id=user_id)

    store = get_reload_job_store()
    stages: list[str] = []
    deadline = asyncio.get_event_loop().time() + _RELOAD_WAIT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        current = store.get(job.job_id) or job
        if current.stage not in stages:
            stages.append(current.stage)
        if current.terminal:
            payload = current.to_dict()
            return render.result(
                _EXIT_OK if current.state == "succeeded" else _EXIT_BACKEND_ERROR,
                stdout=(
                    json.dumps(payload, indent=2)
                    if as_json
                    else render.reload_job(payload, stages=stages)
                ),
            )
        await asyncio.sleep(_RELOAD_POLL_SECONDS)

    # The budget ran out, not the job. Reported as a failure with the id, because
    # claiming success for something still running would be a lie — and the
    # caller can still watch it in the UI or ask the status endpoint.
    return render.result(
        _EXIT_BACKEND_ERROR,
        stderr=(
            f"[datacode] the reload is still running after "
            f"{_RELOAD_WAIT_SECONDS}s (job {job.job_id}). Watch it in the UI, or "
            f"poll GET /api/v1/dagster/deployments/reload/status"
            f"?workspace_path=...&job_id={job.job_id}"
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
