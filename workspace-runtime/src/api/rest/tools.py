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

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.api.deps import validate_workspace_path
from src.config import settings
from src.files import ops as file_ops
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


class SearchRequest(BaseModel):
    workspace_path: str
    query: str
    pattern: str = "*.py"
    max_results: int = Field(default=50, ge=1, le=500)


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
    """Read one file inside the caller's working copy (L1).

    A path that is not there is a TOOL RESULT, not a server error: slicing the
    ``None`` returned for a missing file used to raise TypeError -> 500, so the
    agent saw an infrastructure failure instead of "no such file".
    """
    ws = validate_workspace_path(req.workspace_path)
    content = file_ops.read_file(ws, req.file_path)
    if content is None:
        return {"content": "", "missing": True, "truncated": False}
    return {
        "content": content[: req.max_bytes],
        "missing": False,
        "truncated": len(content) > req.max_bytes,
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
    entries = file_ops.list_files(ws, req.path, recursive=req.recursive, pattern=req.pattern)
    return {"entries": entries}


@router.post("/search")
async def tool_search(req: SearchRequest):
    """Grep-like content search across the working copy (L1)."""
    from pathlib import Path

    ws = validate_workspace_path(req.workspace_path)
    root = Path(ws)
    matches: list[dict] = []
    for path in root.rglob(req.pattern or "*"):
        if not path.is_file() or len(matches) >= req.max_results:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if req.query in line:
                matches.append(
                    {
                        "path": str(path.relative_to(root)),
                        "line": line_no,
                        "text": line[:500],
                    }
                )
                if len(matches) >= req.max_results:
                    break
    return {"matches": matches, "truncated": len(matches) >= req.max_results}


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
