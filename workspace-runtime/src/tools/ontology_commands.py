"""``sqlmesh ontology`` subcommands, served in-process (design 04 §3.6).

The shim used to reach these over HTTP from inside the sandbox. Here they are
plain calls into the ontology route handlers — the same functions the HTTP
surface exposes, so the CLI spelling and the API cannot diverge in what they
validate or in how they report it.

Calling the route handlers rather than the registry directly is deliberate: the
handlers own the workspace-containment check and the error translation, and a
second path into the registry would be a second place for both to drift.
"""

from __future__ import annotations

from typing import Any, Sequence

from src.tools import command_render as render
from src.tools.command_args import has_flag
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Exit codes carried over from the shim: 0 valid, 1 validation errors found.
_EXIT_OK = 0
_EXIT_INVALID = 1
_EXIT_USAGE = 1

_USAGE = (
    "Usage: sqlmesh ontology <subcommand> [options]\n"
    "\n"
    "Subcommands:\n"
    "  validate <name>   Validate cross-references in a specific ontology\n"
    "  validate --all    Validate every ontology in the workspace\n"
    "  list              List all available ontologies"
)


async def run_ontology_command(
    argv: Sequence[str], *, workspace_path: str
) -> dict[str, Any]:
    """Dispatch one ``sqlmesh ontology`` subcommand."""
    tokens = list(argv)
    subcommand = tokens[0] if tokens else ""

    if subcommand == "list":
        return await _list(workspace_path)
    if subcommand == "validate":
        return await _validate(tokens[1:], workspace_path)
    if subcommand in ("", "--help", "-h"):
        return render.result(_EXIT_OK if subcommand else _EXIT_USAGE, stderr=_USAGE)
    return render.result(
        _EXIT_USAGE,
        stderr=f"[datacode] unknown ontology subcommand: '{subcommand}'\n{_USAGE}",
    )


async def _list(workspace_path: str) -> dict[str, Any]:
    from src.api.rest.ontology import list_ontologies

    summaries = await list_ontologies(workspace_path=workspace_path, keyword=None)
    payloads = [_as_dict(summary) for summary in summaries]
    return render.result(_EXIT_OK, stdout=render.ontology_list(payloads))


async def _validate(argv: Sequence[str], workspace_path: str) -> dict[str, Any]:
    if has_flag(argv, "--all"):
        return await _validate_all(workspace_path)

    name = next((token for token in argv if not token.startswith("-")), "")
    if not name:
        return render.result(
            _EXIT_USAGE,
            stderr=(
                "[datacode] ontology validate requires a name or --all\n"
                "  sqlmesh ontology validate <name>   Validate a specific ontology\n"
                "  sqlmesh ontology validate --all    Validate all ontologies"
            ),
        )

    from src.api.rest.ontology import validate_ontology

    outcome = await validate_ontology(name=name, workspace_path=workspace_path)
    errors = [_as_dict(error) for error in outcome.get("errors") or []]
    if outcome.get("valid"):
        return render.result(
            _EXIT_OK,
            stdout=f'Ontology "{name}" is VALID. No validation errors found.',
        )
    lines = [
        f'Ontology "{name}" is INVALID ({len(errors)} error(s)):',
        "",
        *render.validation_errors(errors),
    ]
    return render.result(_EXIT_INVALID, stdout="\n".join(lines))


async def _validate_all(workspace_path: str) -> dict[str, Any]:
    from src.api.rest.ontology import list_ontologies, validate_ontology

    summaries = await list_ontologies(workspace_path=workspace_path, keyword=None)
    if not summaries:
        return render.result(_EXIT_OK, stdout="No ontologies found in workspace.")

    total = len(summaries)
    lines = [f"Validating {total} ontology(ies)...", ""]
    valid_count = 0
    invalid_count = 0

    # Sequential on purpose: each validation fetches the SQLMesh model list, and
    # running them concurrently would multiply that read by the number of
    # ontologies for output nobody reads out of order anyway.
    for index, summary in enumerate(summaries, start=1):
        name = _as_dict(summary).get("name", "?")
        outcome = await validate_ontology(name=name, workspace_path=workspace_path)
        errors = [_as_dict(error) for error in outcome.get("errors") or []]
        if outcome.get("valid"):
            lines.append(f"[{index}/{total}] {name} ... VALID")
            valid_count += 1
        else:
            lines.append(f"[{index}/{total}] {name} ... INVALID ({len(errors)} error(s))")
            lines.extend(render.validation_errors(errors))
            lines.append("")
            invalid_count += 1

    lines.append("")
    lines.append(
        f"Summary: {valid_count} valid, {invalid_count} invalid, {total} total."
    )
    return render.result(
        _EXIT_INVALID if invalid_count else _EXIT_OK, stdout="\n".join(lines)
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """Normalize a pydantic model or mapping to a plain dict.

    The handlers return pydantic models (they are typed FastAPI responses) while
    the renderers take mappings, and going through ``model_dump`` keeps the
    renderers usable from tests that pass plain dicts.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["run_ontology_command"]
