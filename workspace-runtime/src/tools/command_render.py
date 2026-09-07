"""Rendering governance-plane results as shell output (design 04 §3.6).

The router returns the SAME shape the sandbox does — ``{exit_code, stdout,
stderr, timed_out}`` — so nothing downstream can tell which plane served a
command. That is what keeps agent-service's tool schema, the approval flow and
the result truncation unchanged.

The text itself is carried over from the shell shims verbatim in structure,
because an agent's prompts and skills were written against it: an LLM that
learned to read "Plan Summary:" should keep reading the same thing. Only the
implementation moved (bash -> Python), not the wording.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

# Governance results are already bounded by the services that produce them (a
# plan lists changed models, not rows), so this only guards against a
# pathologically large project rather than shaping normal output.
MAX_OUTPUT_CHARS = 65536


def result(
    exit_code: int,
    *,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> dict[str, Any]:
    """Build the shell-result dict.

    ``timed_out`` exists for shape compatibility and is always False here: the
    governance plane's own timeouts surface as errors with a cause, and reporting
    them as a shell timeout would hide it.
    """
    return {
        "exit_code": exit_code,
        "stdout": stdout[:MAX_OUTPUT_CHARS],
        "stderr": stderr[:MAX_OUTPUT_CHARS],
        "timed_out": timed_out,
    }


def environment_notice(requested: str, authoritative: str) -> str:
    """Warn when the command named an environment other than the active one.

    The command line does NOT decide the target — the working copy is
    per-environment, so it cannot. Reporting the disagreement is what makes a
    hallucinated argument visible instead of silently ineffective.
    """
    if not requested or requested == authoritative:
        return ""
    return (
        f"[datacode] requested environment '{requested}' ignored; this working "
        f"copy is checked out to '{authoritative}'"
    )


def plan_preview(payload: Mapping[str, Any]) -> str:
    """Render a plan preview the way ``sqlmesh plan`` did."""
    if not payload.get("has_changes"):
        return "No changes to apply."

    changes = payload.get("changes") or {}
    modified = changes.get("modified") or {}
    direct = list(modified.get("direct") or [])
    indirect = list(modified.get("indirect") or [])
    metadata = list(modified.get("metadata") or [])

    lines = [
        "Plan Summary:",
        f"  Added: {len(changes.get('added') or [])}, "
        f"Removed: {len(changes.get('removed') or [])}",
        f"  Directly Modified: {len(direct)}",
        f"  Indirectly Modified: {len(indirect)}",
        f"  Metadata Changes: {len(metadata)}",
    ]

    if direct:
        lines.append("")
        lines.append("  Directly Modified Models:")
        for change in direct:
            category = change.get("change_category") or ""
            suffix = f" [{category}]" if category else ""
            lines.append(f"    - {_model_name(change)}{suffix}")

    if indirect:
        lines.append("")
        lines.append("  Indirectly Modified Models:")
        for change in indirect:
            lines.append(f"    - {_model_name(change)}")

    backfills = list(payload.get("backfills") or [])
    if payload.get("requires_backfill"):
        lines.append("")
        lines.append(f"  Backfills ({len(backfills)}):")
        for backfill in backfills:
            interval = ", ".join(backfill.get("interval") or [])
            lines.append(
                f"    - {_model_name(backfill)}: "
                f"{backfill.get('batches', 0)} batch(es)  {interval}".rstrip()
            )

    fingerprint = payload.get("state_fingerprint") or ""
    if fingerprint:
        lines.append("")
        lines.append(f"  State Fingerprint: {fingerprint}")

    return "\n".join(lines)


def plan_apply(payload: Mapping[str, Any]) -> tuple[str, int]:
    """Render an apply outcome, with the exit code that goes with it.

    The exit codes are the shims': 0 applied or nothing to do, 4 tests failed,
    5 the apply itself failed. Distinct codes matter because an agent branches
    on them — "tests failed" is a code fix, "apply failed" is not.
    """
    if payload.get("applied"):
        changes = payload.get("changes") or {}
        modified = changes.get("modified") or {}
        backfills = list(payload.get("backfills") or [])
        lines = [
            "Changes applied successfully to environment.",
            f"  Added: {len(changes.get('added') or [])}, "
            f"Removed: {len(changes.get('removed') or [])}",
            f"  Modified (direct): {len(modified.get('direct') or [])}, "
            f"(indirect): {len(modified.get('indirect') or [])}, "
            f"(metadata): {len(modified.get('metadata') or [])}",
            f"  Backfills: {len(backfills)}",
        ]
        for backfill in backfills:
            lines.append(
                f"    - {_model_name(backfill)}: "
                f"{len(backfill.get('batches') or [])} batch(es)"
            )
        return "\n".join(lines), 0

    if payload.get("tests_failed"):
        failures = list((payload.get("test_results") or {}).get("failures") or [])
        lines = ["Apply aborted: tests failed."]
        if failures:
            lines.append(f"  Failed tests: {len(failures)}")
            lines.extend(f"    - {failure}" for failure in failures)
        return "\n".join(lines), 4

    if not payload.get("has_changes"):
        return "No changes detected — nothing to apply.", 0

    return f"Apply failed: {payload.get('reason', 'unknown')}", 5


def external_models(payload: Mapping[str, Any]) -> str:
    """Render an external-models import."""
    written = list(payload.get("written_files") or [])
    lines = [
        f"External models import complete: {payload.get('success_count', 0)} "
        f"table(s) imported, {payload.get('skipped_count', 0)} skipped."
    ]
    if written:
        lines.append("Written files:")
        lines.extend(f"  {path}" for path in written)
    return "\n".join(lines)


def ontology_list(models: Sequence[Mapping[str, Any]]) -> str:
    """Render the ontology listing."""
    if not models:
        return "No ontologies found in workspace."
    lines = [
        f"Available ontologies ({len(models)}):",
        "",
        "  %-30s Description" % "Name",
        "  %s %s" % ("-" * 30, "-" * 11),
    ]
    for model in models:
        lines.append(
            "  %-30s %s" % (model.get("name", "?"), model.get("description") or "")
        )
    return "\n".join(lines)


def validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render one ontology's validation errors as indented lines."""
    return [
        f"  [{error.get('severity', 'error')}] "
        f"{error.get('path', '?')}: {error.get('message', '')}"
        for error in errors
    ]


def asset_graph(payload: Mapping[str, Any], *, as_json: bool) -> str:
    """Render the asset dependency graph."""
    if as_json:
        return json.dumps(payload, indent=2)

    nodes = list(payload.get("nodes") or [])
    edges = list(payload.get("edges") or [])
    lines = [f"Asset graph: {len(nodes)} assets, {len(edges)} dependencies"]
    for node in nodes:
        name = node.get("name") or node.get("asset_key_str") or "?"
        group = node.get("group_name") or "-"
        deps = ", ".join(node.get("deps") or [])
        lines.append(f"  [{group}] {name}" + (f"  <- {deps}" if deps else ""))
    return "\n".join(lines)


def deployment(payload: Mapping[str, Any], *, action: str, as_json: bool) -> str:
    """Render a deployment ensure/reload."""
    if as_json:
        return json.dumps(payload, indent=2)

    name = payload.get("deployment_name", "?")
    location = payload.get("location_name") or name
    state = "RUNNING" if payload.get("alive") else "NOT RUNNING"
    schedules = "enabled" if payload.get("schedules_enabled") else "disabled"
    header = (
        f"Deployment reloaded: {name}" if action == "reload" else f"Deployment: {name}"
    )
    return "\n".join(
        [
            header,
            f"  Environment    : {payload.get('environment', '?')} "
            f"({payload.get('env_type', '?')})",
            f"  Code location  : {location} — {state} at "
            f"{payload.get('host', '?')}:{payload.get('port', '?')}",
            f"  Schedules      : {schedules}",
        ]
    )


def job_run(payload: Mapping[str, Any], *, job_name: str) -> str:
    """Render a launched Dagster run."""
    return "\n".join(
        [
            f"Run launched: {job_name}",
            f"  Run ID : {payload.get('run_id', '?')}",
            f"  Status : {payload.get('status', '?')}",
        ]
    )


def _model_name(entry: Mapping[str, Any]) -> str:
    """A change/backfill entry's display name.

    ``view_name`` first: it is the name the user sees in the warehouse, and the
    shims preferred it for the same reason.
    """
    return str(entry.get("view_name") or entry.get("name") or "?")


def error_text(exc: BaseException, *, prefix: str = "[datacode]") -> Optional[str]:
    """Render an exception as CLI-style stderr lines."""
    message = str(exc).strip() or exc.__class__.__name__
    return "\n".join(f"{prefix} {line}" for line in message.splitlines())


__all__ = [
    "MAX_OUTPUT_CHARS",
    "asset_graph",
    "deployment",
    "environment_notice",
    "error_text",
    "external_models",
    "job_run",
    "ontology_list",
    "plan_apply",
    "plan_preview",
    "result",
    "validation_errors",
]
