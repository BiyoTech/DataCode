"""``dc deps`` — declare-then-sync, from the CLI and from the agent.

Three subcommands, and the split matters:

* ``dc deps baseline`` lists what the platform already provides. It exists
  because a compatible declaration cannot be written against an invisible list:
  the agent writes most ``pyproject.toml`` files, and before this it had nothing
  to check a version bound against.
* ``dc deps status`` reads the declaration and the manifest, including any
  conflict with that baseline. No install, no sandbox, no wait — the same read
  the asset page performs.
* ``dc deps sync`` installs. It does NOT reload the code location, because those
  are two intents: an agent that just edited ``pyproject.toml`` wants the packages
  importable in its next shell command, while making them live for Dagster is what
  Reload (or ``dc deployment reload``) is for.

The installer is the same one the reload job drives. There is deliberately no
second path: a CLI that installed differently from the button would produce trees
that differ by how they were created.
"""

from __future__ import annotations

from typing import Any, Sequence

from src.tools import command_render as render
from src.tools.command_args import has_flag
from src.utils.logging import get_logger

logger = get_logger(__name__)

_EXIT_OK = 0
_EXIT_USAGE = 1
_EXIT_FAILED = 2

_USAGE = "Usage: dc deps {baseline|status|sync} [--json] [--all]"


async def run_deps_command(
    argv: Sequence[str],
    *,
    workspace_path: str,
    environment: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """Serve one ``dc deps`` invocation.

    Args:
        argv: Tokens after ``dc deps``.
        workspace_path: Working copy, already validated by the caller.
        environment: For task audit attribution.
        user_id: Recorded in the manifest, so a surprising tree can be traced
            back to who asked for it.

    Returns:
        ``{exit_code, stdout, stderr, timed_out}`` — the shell result shape, so
        the caller cannot tell which plane served the command.
    """
    from src.deps.service import get_deps_service

    tokens = list(argv)
    subcommand = tokens[0] if tokens else ""
    as_json = has_flag(tokens, "--json")
    service = get_deps_service()

    if subcommand == "baseline":
        return render.result(
            _EXIT_OK,
            stdout=render.deps_baseline(
                _baseline_payload(full=has_flag(tokens, "--all")), as_json=as_json
            ),
        )

    if subcommand == "status":
        return render.result(
            _EXIT_OK,
            stdout=render.deps_status(
                service.inspect(workspace_path).to_dict(), as_json=as_json
            ),
        )

    if subcommand != "sync":
        return render.result(
            _EXIT_USAGE,
            stderr=(
                f"[datacode] unknown deps subcommand: "
                f"'{subcommand or '<empty>'}'\n{_USAGE}"
            ),
        )

    # Progress lines are collected rather than streamed: a shell result is one
    # payload, and an agent reading it needs the END of the transcript (where the
    # error is) far more than a live feed.
    lines: list[str] = []

    async def _collect(line: str) -> None:
        lines.append(line)

    outcome = await service.sync(
        workspace_path,
        source="cli",
        actor_user_id=user_id,
        environment=environment,
        on_line=_collect,
    )

    status = (
        outcome.status.to_dict()
        if outcome.status is not None
        else service.inspect(workspace_path).to_dict()
    )
    if not outcome.ok:
        return render.result(
            _EXIT_FAILED,
            stdout=render.deps_status(status, as_json=as_json),
            stderr=_failure_text(outcome, lines),
        )

    header = (
        "Dependencies already up to date."
        if outcome.skipped
        else f"Installed {outcome.installed} package(s)."
    )
    body = render.deps_status(status, as_json=as_json)
    # Reported on SUCCESS, in stdout, because the install worked and this is the
    # only place the consequence is stated: from here on this copy imports a
    # different version of these packages than the platform itself does.
    notes = _relaxed_text(outcome)
    return render.result(_EXIT_OK, stdout=f"{header}\n{notes}{body}")


def _baseline_payload(*, full: bool) -> dict[str, Any]:
    """The platform baseline, curated by default and complete with ``--all``.

    Curated by default because the full answer is several hundred
    distributions, nearly all of them transitive, and a wall of those is not
    something a reader — human or agent — extracts an answer from. ``--all``
    exists for the case where the question is about a specific transitive
    package.
    """
    from datacode_common.python_deps import (
        interpreter_platform_tag,
        interpreter_python_version,
    )

    from src.api.rest.deps import HIGHLIGHTED
    from src.deps.baseline import platform_baseline

    baseline = platform_baseline()
    names = sorted(baseline) if full else [n for n in HIGHLIGHTED if n in baseline]
    return {
        "packages": [
            {"name": name, "version": baseline[name], "suggested": f"{name}>={baseline[name]}"}
            for name in names
        ],
        "total": len(baseline),
        "truncated": not full,
        "python_version": interpreter_python_version(),
        "platform_tag": interpreter_platform_tag(),
    }


def _relaxed_text(outcome) -> str:
    """Lines naming the platform pins this install overrode, or ``""``.

    Phrased as a warning rather than a note: nothing is broken, but the
    workspace now diverges from the platform on these packages, and a reader who
    hits odd behaviour later needs to have been told once.
    """
    relaxed = getattr(outcome, "relaxed", ()) or ()
    if not relaxed:
        return ""
    parts = [
        "[datacode] the platform yielded the following packages so the "
        "declaration could install; this working copy now uses its own version:"
    ]
    for entry in relaxed:
        required_by = entry.get("required_by") or ""
        parts.append(
            f"  {entry['name']}: platform {entry['platform_version']} -> "
            f"{entry['overlay_version']}"
            + (f" (required by {required_by})" if required_by else "")
        )
    return "\n".join(parts) + "\n"


def _failure_text(outcome, lines: Sequence[str]) -> str:
    """Stderr for a failed sync: the reason, where to read more, then output."""
    parts = [f"[datacode] {outcome.message}"]
    if outcome.failure_code:
        parts.append(f"  Reason  : {outcome.failure_code}")
    for conflict in getattr(outcome, "conflicts", ()) or ():
        # The edit to make, named exactly. This is the line an agent acts on.
        parts.append(
            f"  Fix     : declare {conflict.suggested!r} instead of "
            f"{conflict.requirement!r}"
        )
    for conflict in getattr(outcome, "transitive_conflicts", ()) or ():
        # Same idea, but the blocking package is one the declaration never
        # mentions — so it is named too, together with the dependency that
        # asked for it, otherwise the edit looks arbitrary.
        for blocker in conflict.blockers:
            asker = blocker.get("required_by") or ""
            parts.append(
                f"  Blocked : {conflict.requirement!r} needs "
                f"{blocker['name']} {blocker['required']}"
                + (f" (via {asker})" if asker else "")
                + f", the platform provides "
                f"{blocker['name']}=={blocker['platform_version']}"
            )
        if conflict.suggested:
            parts.append(
                f"  Fix     : declare {conflict.suggested!r} instead of "
                f"{conflict.requirement!r}"
                + (f" (installs {conflict.resolved_version})" if conflict.resolved_version else "")
            )
        elif conflict.protected:
            # The honest dead end, with the name an operator has to act on.
            parts.append(
                f"  Fix     : none for you — {', '.join(conflict.protected)} is "
                f"part of the platform's own runtime and cannot be overridden. "
                f"Ask an operator to upgrade the baseline, or to allow it via "
                f"deps.relaxable_packages"
            )
        else:
            parts.append(
                f"  Fix     : none — no release of {conflict.name!r} fits this "
                f"platform baseline; ask an operator to upgrade it"
            )
    if outcome.error_log_id:
        # The archived record holds the full context; naming it means the agent
        # can open the file instead of asking the API again.
        parts.append(f"  Details : logs/errors/ (error_id {outcome.error_log_id})")
    tail = outcome.output_tail or "\n".join(lines)
    if tail.strip():
        parts.append("  Output  :")
        parts.extend(f"    {line}" for line in tail.splitlines()[-40:])
    return "\n".join(parts)


__all__ = ["run_deps_command"]
