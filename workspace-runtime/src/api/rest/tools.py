"""Agent tool execution API — ``/rt/v1/tools/*`` (design 06, P3d).

agent-service no longer touches the filesystem or spawns processes itself:
every gated tool call lands here, where the isolation boundary lives.

Levels (conf/isolation-profiles.yaml):
  L1 in-process  — pure file IO / listing / search, path-validated against
                   the 5-segment working-copy layout (``parse_workspace_path``);
  L3-proc        — ``shell`` and anything that may execute user code, routed
                   to the persistent executor child (cleared env + rlimits,
                   design 04 §2.3).

The response shapes intentionally mirror what agent-service's tool wrappers
returned in-process, so the LLM-facing tool schemas are unchanged.
"""

from __future__ import annotations

import base64
import re

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.api.deps import validate_workspace_path
from src.config import settings
from src.files import ops as file_ops
from src.files import search as file_search
from src.utils.errors import InvalidRequestError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Slack added to the command's own timeout when budgeting the TASK: the vehicle
# needs a moment to start and to report, and a task that dies exactly when the
# command's timer fires loses the "timed_out" report the agent relies on.
_SHELL_SUBMIT_MARGIN_SECONDS = 30

router = APIRouter(prefix="/rt/v1/tools", tags=["tools"])


# ── request / response models ─────────────────────────────────────────────


class FileReadRequest(BaseModel):
    workspace_path: str
    file_path: str
    max_bytes: int = Field(default=1_000_000, ge=1, le=10_000_000)


class FileReadBytesRequest(BaseModel):
    workspace_path: str
    file_path: str
    # Lower ceiling than the text read: the payload is base64 (4/3 expansion)
    # and the only caller parses office documents, which are small next to the
    # data files a working copy may hold.
    max_bytes: int = Field(default=20_000_000, ge=1, le=50_000_000)


class FileWriteRequest(BaseModel):
    workspace_path: str
    file_path: str
    content: str


class FileDeleteRequest(BaseModel):
    workspace_path: str
    file_path: str


class FileListRequest(BaseModel):
    workspace_path: str
    path: str = ""
    pattern: str = "*"
    recursive: bool = False
    include_dirs: bool = False


class SearchRequest(BaseModel):
    workspace_path: str
    query: str
    pattern: str = "*"
    regex: bool = False
    context_before: int = Field(default=0, ge=0, le=20)
    context_after: int = Field(default=0, ge=0, le=20)
    max_results: int = Field(default=50, ge=1, le=500)
    # The counting output modes need per-file totals only; collecting the lines
    # for them would be work the caller throws away.
    include_matches: bool = True


class ShellRequest(BaseModel):
    workspace_path: str
    command: str
    timeout_s: int = Field(default=120, ge=1, le=1800)
    # Attribution for the apply history when the command turns out to be a
    # governed verb. Supplied by the caller (agent-service knows the session);
    # never read from the sandbox, which is why routing had to move here.
    user_id: str = ""
    session_id: str = ""
    trigger_type: str = "agent"


# ── L1: in-process file tools ────────────────────────────────────────────


@router.post("/read")
async def tool_read(req: FileReadRequest):
    """Read one file as TEXT inside the caller's working copy (L1).

    Two answers that are results rather than failures:

    * a path that is not there — slicing the ``None`` returned for a missing
      file used to raise TypeError -> 500, so the agent saw an infrastructure
      failure instead of "no such file";
    * a file that is not text — decoding it would either raise or hand the
      model a screenful of replacement characters. It is reported as
      ``binary`` so the caller can say so, or fetch the bytes instead.
    """
    ws = validate_workspace_path(req.workspace_path)
    raw = file_ops.read_bytes(ws, req.file_path, max_bytes=req.max_bytes)
    if raw is None:
        return {"content": "", "missing": True, "binary": False, "truncated": False}

    data, truncated = raw
    if file_ops.looks_binary(data[:8192]):
        return {"content": "", "missing": False, "binary": True, "truncated": False}
    return {
        "content": data.decode("utf-8", errors="replace"),
        "missing": False,
        "binary": False,
        "truncated": truncated,
    }


@router.post("/read-bytes")
async def tool_read_bytes(req: FileReadBytesRequest):
    """Read one file as base64 bytes (L1).

    Exists for the formats text cannot carry: office documents are archives,
    and the parsers that turn them into Markdown live in agent-service (they
    are LLM-facing presentation, and their dependency footprint has no place
    in the service that holds working copies). So the bytes travel and the
    parsing stays where it belongs.
    """
    ws = validate_workspace_path(req.workspace_path)
    raw = file_ops.read_bytes(ws, req.file_path, max_bytes=req.max_bytes)
    if raw is None:
        return {"content_base64": "", "missing": True, "truncated": False}

    data, truncated = raw
    return {
        "content_base64": base64.b64encode(data).decode("ascii"),
        "missing": False,
        "truncated": truncated,
    }


@router.post("/write")
async def tool_write(req: FileWriteRequest):
    """Write one file inside the caller's working copy (L1)."""
    ws = validate_workspace_path(req.workspace_path)
    path = file_ops.write_file(ws, req.file_path, req.content)
    return {"path": path}


@router.post("/delete")
async def tool_delete(req: FileDeleteRequest):
    """Delete a file, symlink or directory inside the caller's working copy (L1).

    A directory is removed with everything under it. The path is still checked
    against the working copy, so the tool cannot reach the copy's Git metadata
    or anything outside the copy.
    """
    ws = validate_workspace_path(req.workspace_path)
    return {"deleted": file_ops.delete_path(ws, req.file_path)}


@router.post("/list")
async def tool_list(req: FileListRequest):
    ws = validate_workspace_path(req.workspace_path)
    # Keyword arguments on purpose: the positional order is
    # (workspace, directory, recursive, pattern), so passing them by position
    # swapped the two and made every listing recursive while ignoring the
    # pattern.
    entries = file_ops.list_files(
        ws,
        req.path,
        recursive=req.recursive,
        pattern=req.pattern,
        include_dirs=req.include_dirs,
    )
    return {"entries": entries}


@router.post("/search")
async def tool_search(req: SearchRequest):
    """Grep-like content search across the working copy (L1).

    The walk, the visibility policy and the binary skip live in
    ``src.files.search``; what comes back is structured, and the caller renders
    it. An invalid regular expression is the caller's usage error, so it is
    answered as a 400 with the message the engine gave — not a 500.
    """
    ws = validate_workspace_path(req.workspace_path)
    try:
        outcome = file_search.search_workspace(
            ws,
            req.query,
            file_pattern=req.pattern,
            regex=req.regex,
            context_before=req.context_before,
            context_after=req.context_after,
            max_results=req.max_results,
            include_matches=req.include_matches,
        )
    except re.error as exc:
        raise InvalidRequestError(f"invalid regular expression: {exc}") from exc

    return {
        "matches": [
            {
                "path": match.path,
                "line": match.line,
                "text": match.text,
                "context": match.context,
            }
            for match in outcome.matches
        ],
        "files": outcome.files,
        "truncated": outcome.truncated,
    }


# ── L3: gated execution ───────────────────────────────────────────────────


@router.post("/shell")
async def tool_shell(req: ShellRequest):
    """Execute a shell command for the agent — routed, then executed.

    Two planes, and the choice between them is made HERE, on the trusted side of
    the isolation boundary (design 04 §3.6):

    * **governance plane** — a handful of verbs (``sqlmesh plan/apply``,
      ``sqlmesh ontology``, ``dc *``) are platform operations with a CLI
      spelling. They need an environment lock, an audit record and credentials
      that user code must never hold, so they run in this process through the
      same services the Web UI calls;
    * **sandbox** — everything else, including ``sqlmesh test`` / ``audit``
      (they execute the user's own Python) and every ordinary command. Submitted
      through the task plane, so the vehicle is whatever the isolation policy
      picks for ``agent.shell``: an L3-base container in production, the hardened
      one-shot process in dev. Never this process, so user code cannot read
      platform credentials (threat T2).

    Deciding here is what lets the sandbox stay unable to reach the platform: it
    receives no runtime URL and no identity, so the L3-base NetworkPolicy needs
    no exception (threats T3/T6).

    The result shape is identical either way (exit_code/stdout/stderr/timed_out),
    so agent-service's tool schema is unchanged by the split.
    """
    ws = validate_workspace_path(req.workspace_path)
    tenant_id, workspace_id, user_id, environment = _identity(ws)

    from src.tools.command_router import classify

    routed = classify(req.command)
    if routed is not None:
        return await _run_governed(req, routed, ws, environment, user_id)

    return await _run_in_sandbox(req, ws, tenant_id, workspace_id, user_id, environment)


async def _run_governed(
    req: ShellRequest,
    routed,
    workspace_path: str,
    environment: str,
    path_user_id: str,
) -> dict:
    """Serve a governed verb through the platform's own services.

    Failures are rendered as a FAILED COMMAND (non-zero exit, message on stderr)
    rather than raised as an HTTP error: the caller asked to run a command, and a
    command that ran and failed is a tool result. Reporting a plan's lint error
    as a 5xx would tell the agent the infrastructure broke.
    """
    from src.tools import command_render as render
    from src.tools.command_args import CommandArgumentError
    from src.tools.command_router import EXIT_BACKEND_ERROR, EXIT_USAGE, get_command_router
    from src.utils.errors import ServiceError

    try:
        result = await get_command_router().execute(
            routed,
            workspace_path=workspace_path,
            environment=environment,
            # The path's user id is the owner of the working copy and is
            # authoritative; the request's is attribution the caller supplies.
            user_id=req.user_id or path_user_id,
            session_id=req.session_id,
            trigger_type=req.trigger_type or "agent",
        )
    except CommandArgumentError as exc:
        logger.info(
            "agent_shell_governed_usage_error",
            verb=" ".join(routed.verb),
            error=str(exc),
        )
        return render.result(EXIT_USAGE, stderr=render.error_text(exc) or "")
    except ServiceError as exc:
        # A domain verdict (state-schema mismatch, environment busy, optimistic
        # lock lost). Its message is the actionable part, and the error code is
        # kept so the agent can recognise the contract verdicts it knows.
        logger.warning(
            "agent_shell_governed_failed",
            verb=" ".join(routed.verb),
            error_code=exc.code.value,
            workspace_path=workspace_path,
        )
        return render.result(
            EXIT_BACKEND_ERROR,
            stderr=f"[datacode] {exc.code.value}: {exc.message}",
        )

    logger.info(
        "agent_shell_governed_executed",
        verb=" ".join(routed.verb),
        workspace_path=workspace_path,
        environment=environment,
        exit_code=result.get("exit_code"),
    )
    return result


async def _run_in_sandbox(
    req: ShellRequest,
    workspace_path: str,
    tenant_id: str,
    workspace_id: str,
    user_id: str,
    environment: str,
) -> dict:
    """Submit the command to the sandbox plane."""
    from src.tasks.submitter import (
        TaskSubmissionError,
        get_task_submitter,
        submission_unavailable,
    )

    try:
        task_result = await get_task_submitter().submit(
            "agent.shell",
            workspace_path,
            environment=environment,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            actor_user_id=user_id,
            params={"command": req.command, "timeout_s": req.timeout_s},
            timeout_seconds=req.timeout_s + _SHELL_SUBMIT_MARGIN_SECONDS,
        )
    except TaskSubmissionError as exc:
        # The vehicle never started (no backend, image pull, node capacity).
        # Raised as the contract's SANDBOX_UNAVAILABLE (503) rather than an
        # in-band dict: the agent's client already maps 5xx to a failed
        # command, and a 200 would read as "the command ran and failed".
        logger.warning("agent_shell_submit_error", error=str(exc))
        raise submission_unavailable(exc) from exc

    if not task_result.succeeded:
        logger.warning(
            "agent_shell_failed",
            workspace_path=workspace_path,
            tenant_id=tenant_id,
            error_code=task_result.error_code,
        )
        return {
            "error_code": task_result.error_code or "task_failed",
            "message": task_result.error_message or "shell execution failed",
        }

    logger.info(
        "agent_shell_executed",
        workspace_path=workspace_path,
        tenant_id=tenant_id,
        exit_code=task_result.result.get("exit_code"),
        timed_out=task_result.result.get("timed_out", False),
    )
    return task_result.result


def _identity(workspace_path: str) -> tuple[str, str, str, str]:
    from datacode_common.workspace_paths import parse_workspace_path

    return parse_workspace_path(settings.workspace_base_path, workspace_path)
