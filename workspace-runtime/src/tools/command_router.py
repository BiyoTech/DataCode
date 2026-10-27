"""Command router — decides whether a shell command belongs to the governance
plane or to the sandbox (design 04 §3.6).

The problem it solves
---------------------
A handful of CLI verbs are not really shell commands. ``sqlmesh apply`` needs an
environment lock, an apply-history record and a writable warehouse account that
must never coexist with user code (Q5). ``dc run`` needs the run stamped with the
deployment's tags. Those are platform operations that happen to have a CLI
spelling.

Before this module the dispatch happened INSIDE the sandbox, by PATH ordering: a
shim was prepended so it shadowed the native binary and called back into the
platform over HTTP. Three consequences followed, all of them structural:

* the sandbox needed the platform's address and the caller's identity, which is
  exactly the network path the L3-base NetworkPolicy denies (threats T3/T6);
* dev and production decided differently, because the deciding factor was
  whether the shim could be injected and whether the network was reachable;
* the shim's bash logic and the Web UI's ``PlanService`` were two implementations
  of one operation.

Moving the decision to this side removes all three at once. The sandbox gets no
platform address, no identity and no callback permission; the governance plane is
reached in-process, by the same code the Web buttons use.

What is routed, and what deliberately is not
--------------------------------------------
Routed: the verbs declared in ``datacode_common.command_verbs``. Everything else
— ``ls``, ``grep``, ``pytest``, ``git log``, and notably ``sqlmesh test`` /
``audit`` / ``dag`` — goes to the sandbox, because those execute the user's own
Python models and test code, which is what L3 exists for.

NOT routed: anything that is not a single simple command. ``sqlmesh plan dev &&
rm -rf models`` would be routed on its first two tokens and the rest silently
dropped, so a command carrying shell operators goes to the sandbox as a whole.
That is not a bypass: the sandbox's PATH carries generated guards
(``src/isolation/guards.py``) which refuse the governed verbs with the same
message this module would have produced.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Optional

from datacode_common.command_verbs import (
    governance_hint,
    is_simple_command,
    match_governed_verb,
)

from src.tools import command_render as render
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Exit codes the router reports. They mirror what the CLI shims returned, so an
# agent reading exit codes sees no change from the shim era.
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BACKEND_ERROR = 2
EXIT_TESTS_FAILED = 4
EXIT_APPLY_FAILED = 5


@dataclass(frozen=True)
class RoutedCommand:
    """A command the governance plane will serve, with its parsed arguments."""

    verb: tuple[str, ...]
    argv: tuple[str, ...]


def classify(command: str) -> Optional[RoutedCommand]:
    """Return the governed command *command* is, or ``None`` for the sandbox.

    ``None`` is the answer for everything the platform does not own AND for
    everything it cannot parse with confidence — an unknown verb, a compound
    command, an unbalanced quote. Defaulting to the sandbox keeps the router from
    becoming a second place that can refuse a legitimate command.
    """
    text = (command or "").strip()
    if not text or not is_simple_command(text):
        return None

    try:
        argv = shlex.split(text)
    except ValueError:
        # Unbalanced quoting: bash would interpret this differently from us, so
        # any argv we derived would be a guess.
        return None

    verb = match_governed_verb(argv)
    if verb is None:
        return None
    return RoutedCommand(verb=verb, argv=tuple(argv))


class CommandRouter:
    """Serves governed verbs against the runtime's own services."""

    async def execute(
        self,
        routed: RoutedCommand,
        *,
        workspace_path: str,
        environment: str,
        user_id: str = "",
        session_id: str = "",
        trigger_type: str = "agent",
    ) -> dict[str, Any]:
        """Run one governed command and render it as a shell result.

        Args:
            routed: The classified command.
            workspace_path: Working copy, already validated by the caller.
            environment: The AUTHORITATIVE environment — the one the user
                selected, resolved on this side of the boundary. An environment
                named in the command line is only reported when it disagrees;
                it never takes effect (anti-hallucination).
            user_id: Attribution, for the apply history.
            session_id: Attribution, for the apply history.
            trigger_type: ``agent`` or ``human``, for the apply history.

        Returns:
            ``{exit_code, stdout, stderr, timed_out}`` — the same shape the
            sandbox path returns, so nothing downstream can tell which plane
            served the command.
        """
        handler = _HANDLERS.get(routed.verb)
        if handler is None:
            # Declared in command_verbs but not implemented here. A refusal, not
            # a fall-through to the sandbox: the verb IS governed, and running it
            # natively is what the declaration forbids.
            return render.result(
                EXIT_USAGE, stderr=governance_hint(routed.verb)
            )

        logger.info(
            "command_routed_to_governance",
            verb=" ".join(routed.verb),
            workspace_path=workspace_path,
            environment=environment,
            trigger_type=trigger_type,
        )
        return await handler(
            self,
            routed,
            workspace_path=workspace_path,
            environment=environment,
            user_id=user_id,
            session_id=session_id,
            trigger_type=trigger_type,
        )

    # ── sqlmesh plan / apply ───────────────────────────────────────────

    async def _sqlmesh_plan(
        self,
        routed: RoutedCommand,
        *,
        workspace_path: str,
        environment: str,
        **_attribution: str,
    ) -> dict[str, Any]:
        """``sqlmesh plan <env>`` — preview only, never deploys."""
        from src.tools.command_args import parse_plan_args

        args = parse_plan_args(routed.argv[2:])
        if args.auto_apply:
            return render.result(
                EXIT_USAGE,
                stderr=(
                    "'plan --auto-apply' is disabled on this platform. Preview "
                    "and deploy are separate commands:\n"
                    "  1) Preview : sqlmesh plan <env>\n"
                    "  2) Deploy  : sqlmesh apply <env>   (after user confirmation)"
                ),
            )

        from src.api.deps import get_plan_service

        notice = render.environment_notice(args.environment, environment)
        result = await get_plan_service().preview(
            workspace_path=workspace_path,
            environment=environment,
            gateway=args.gateway or None,
            plan_dates=args.plan_dates(),
            plan_options=args.plan_options(),
        )
        return render.result(
            EXIT_OK, stdout=render.plan_preview(result), stderr=notice
        )

    async def _sqlmesh_apply(
        self,
        routed: RoutedCommand,
        *,
        workspace_path: str,
        environment: str,
        user_id: str = "",
        session_id: str = "",
        trigger_type: str = "agent",
    ) -> dict[str, Any]:
        """``sqlmesh apply <env>`` — the deploy, with full governance."""
        from src.tools.command_args import parse_plan_args

        args = parse_plan_args(routed.argv[2:])
        options = args.plan_options()
        # The CLI's `apply` IS an auto-applied plan; the flag is not the user's
        # to set here (a `plan --auto-apply` is refused above).
        options["auto_apply"] = True

        from src.api.deps import get_plan_service

        notice = render.environment_notice(args.environment, environment)
        result = await get_plan_service().apply(
            workspace_path=workspace_path,
            environment=environment,
            gateway=args.gateway or None,
            plan_dates=args.plan_dates(),
            plan_options=options,
            user_id=user_id,
            session_id=session_id,
            trigger_type=trigger_type,
        )
        stdout, exit_code = render.plan_apply(result)
        return render.result(exit_code, stdout=stdout, stderr=notice)

    # ── sqlmesh run (refused) ──────────────────────────────────────────

    async def _sqlmesh_run(
        self, routed: RoutedCommand, **_context: str
    ) -> dict[str, Any]:
        """Refused: data refresh outside the guardrail, the lock and the audit."""
        return render.result(EXIT_USAGE, stderr=governance_hint(routed.verb))

    # ── sqlmesh create_external_models ─────────────────────────────────

    async def _create_external_models(
        self,
        routed: RoutedCommand,
        *,
        workspace_path: str,
        **_context: str,
    ) -> dict[str, Any]:
        """Import external table schemas into the working copy."""
        from src.tools.command_args import parse_external_models_args

        args = parse_external_models_args(routed.argv[2:])
        if not args.schema:
            return render.result(
                EXIT_USAGE,
                stderr=(
                    "--schema is required.\n"
                    "Usage: sqlmesh create_external_models --schema <db> "
                    "[--gateway <gw>] [--include <patterns>] "
                    "[--exclude <patterns>] [--samples <N>] [--strict]"
                ),
            )

        from src.api.deps import get_external_models_service

        result = await get_external_models_service().import_external_models(
            workspace_path=workspace_path,
            gateway=args.gateway or None,
            schema=args.schema,
            tables=[],
            include=args.include,
            exclude=args.exclude,
            samples=args.samples,
            strict=args.strict,
        )
        return render.result(EXIT_OK, stdout=render.external_models(result))

    # ── sqlmesh ontology ───────────────────────────────────────────────

    async def _ontology(
        self,
        routed: RoutedCommand,
        *,
        workspace_path: str,
        **_context: str,
    ) -> dict[str, Any]:
        """``sqlmesh ontology list`` / ``validate <name>|--all``."""
        from src.tools.ontology_commands import run_ontology_command

        return await run_ontology_command(
            routed.argv[2:], workspace_path=workspace_path
        )

    # ── dc: the Dagster data plane ─────────────────────────────────────

    async def _dc_assets(
        self,
        routed: RoutedCommand,
        *,
        workspace_path: str,
        environment: str,
        **_context: str,
    ) -> dict[str, Any]:
        """``dc assets [--json]`` — the environment's asset dependency graph."""
        from src.tools.dagster_commands import run_assets_command

        return await run_assets_command(
            routed.argv[2:], workspace_path=workspace_path, environment=environment
        )

    async def _dc_deployment(
        self,
        routed: RoutedCommand,
        *,
        workspace_path: str,
        environment: str,
        user_id: str = "",
        **_context: str,
    ) -> dict[str, Any]:
        """``dc deployment status|reload|deps``."""
        from src.tools.dagster_commands import run_deployment_command

        return await run_deployment_command(
            routed.argv[2:],
            workspace_path=workspace_path,
            environment=environment,
            user_id=user_id,
        )

    # ── dc deps: the working copy's Python packages ────────────────────

    async def _dc_deps(
        self,
        routed: RoutedCommand,
        *,
        workspace_path: str,
        environment: str,
        user_id: str = "",
        **_context: str,
    ) -> dict[str, Any]:
        """``dc deps status|sync`` — read or install the declared packages."""
        from src.tools.deps_commands import run_deps_command

        return await run_deps_command(
            routed.argv[2:],
            workspace_path=workspace_path,
            environment=environment,
            user_id=user_id,
        )

    # ── pip install (refused) ──────────────────────────────────────────

    async def _pip_install(
        self, routed: RoutedCommand, **_context: str
    ) -> dict[str, Any]:
        """Refused, with the alternative spelled out.

        A hand-installed package lives only inside the vehicle that ran the
        command, so it disappears when that vehicle exits and nothing records why
        it was ever there. Declaring it makes the install reproducible for the
        whole working copy — which is what the user actually wanted.
        """
        return render.result(EXIT_USAGE, stderr=governance_hint(routed.verb))

    async def _dc_run(
        self,
        routed: RoutedCommand,
        *,
        workspace_path: str,
        **_context: str,
    ) -> dict[str, Any]:
        """``dc run <job_name> [--config-json J]``."""
        from src.tools.dagster_commands import run_job_command

        return await run_job_command(routed.argv[2:], workspace_path=workspace_path)


# Explicit table, mirroring the task-runner's handler registry: it is a security
# statement as much as a dispatch mechanism. A verb declared governed but absent
# here is REFUSED (see `execute`), never quietly forwarded to the sandbox.
_HANDLERS = {
    ("sqlmesh", "plan"): CommandRouter._sqlmesh_plan,
    ("sqlmesh", "apply"): CommandRouter._sqlmesh_apply,
    ("sqlmesh", "run"): CommandRouter._sqlmesh_run,
    ("sqlmesh", "create_external_models"): CommandRouter._create_external_models,
    ("sqlmesh", "ontology"): CommandRouter._ontology,
    ("dc", "assets"): CommandRouter._dc_assets,
    ("dc", "deployment"): CommandRouter._dc_deployment,
    ("dc", "deps"): CommandRouter._dc_deps,
    ("dc", "run"): CommandRouter._dc_run,
    ("pip", "install"): CommandRouter._pip_install,
    ("pip3", "install"): CommandRouter._pip_install,
}

_router: Optional[CommandRouter] = None


def get_command_router() -> CommandRouter:
    """Return the shared router. Stateless, so one instance is enough."""
    global _router
    if _router is None:
        _router = CommandRouter()
    return _router


__all__ = [
    "EXIT_APPLY_FAILED",
    "EXIT_BACKEND_ERROR",
    "EXIT_OK",
    "EXIT_TESTS_FAILED",
    "EXIT_USAGE",
    "CommandRouter",
    "RoutedCommand",
    "classify",
    "get_command_router",
]
