"""Tool: apply_patch - edit workspace files using the V4A patch format.

This is the coding-agent counterpart of `create_file`. Instead of overwriting a
whole file, the model emits a V4A patch envelope describing precise edits:

    *** Begin Patch
    *** Add File: path/to/new_file.sql
    +line one
    +line two
    *** Update File: path/to/existing.sql
    @@ optional anchor
     context line
    -removed line
    +added line
    *** Delete File: path/to/old.sql
    *** End Patch

For every affected file the tool computes a unified diff plus added/removed line
counts and emits an `artifact.diff` event so the UI can render a Cursor / Claude
Code style file-change card (clickable to open a before/after diff view).

The patch is applied atomically: if any file operation fails to parse or apply,
nothing is written to disk and no events are emitted.
"""

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from agents import RunContextWrapper, function_tool

from src.context.event_bus import event_bus
from src.context.session import session_manager
from src.context.tool_context import ToolContext
from src.protocol.messages import artifact_diff
from src.tools._apply_diff import apply_diff
from src.tools.registry import ToolRegistry
from src.utils.logging import get_logger

logger = get_logger(__name__)

# V4A envelope markers.
_BEGIN_PATCH = "*** Begin Patch"
_END_PATCH = "*** End Patch"
_ADD_FILE = "*** Add File:"
_UPDATE_FILE = "*** Update File:"
_DELETE_FILE = "*** Delete File:"
_MOVE_TO = "*** Move to:"

# File extension -> Monaco language id (used by the before/after diff viewer).
_LANGUAGE_BY_EXT = {
    "sql": "sql",
    "py": "python",
    "yaml": "yaml",
    "yml": "yaml",
    "json": "json",
    "md": "markdown",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "vue": "vue",
    "csv": "csv",
    "txt": "plaintext",
}


@dataclass
class _PatchOp:
    """A single file operation parsed from the patch envelope."""

    op_type: str  # "create" | "update" | "delete"
    path: str
    body: list[str] = field(default_factory=list)
    move_to: str | None = None


@dataclass
class _FileChange:
    """Resolved result of applying one operation (phase-1, in memory)."""

    change_type: str  # "create" | "update" | "delete"
    display_path: str  # path shown in the UI (move target if renamed)
    write_target: Path | None  # absolute path to write (None for delete)
    delete_source: Path | None  # absolute path to remove (delete / rename source)
    old_content: str
    new_content: str
    diff: str
    lines_added: int
    lines_removed: int


@ToolRegistry.register("apply_patch", level=2)
@function_tool
async def apply_patch(ctx: RunContextWrapper[ToolContext], patch: str) -> str:
    """Apply a V4A patch to edit, create, or delete files in the workspace.

    Prefer this over create_file when modifying an existing file: it makes
    surgical line-level edits and renders a diff card in the UI. Always read the
    file first so your context lines match the current content.

    The `patch` argument must be a full V4A envelope:

        *** Begin Patch
        *** Update File: models/staging/stg_users.sql
        @@
         SELECT
        -    old_col
        +    new_col
         FROM source
        *** End Patch

    Supported sections: "*** Add File:", "*** Update File:" (optionally followed
    by "*** Move to:" to rename), and "*** Delete File:". Paths are relative to
    the workspace root.

    Args:
        patch: The complete V4A patch envelope.

    Returns:
        A concise English summary of the files changed and their +added/-removed
        line counts, or an error message if the patch could not be applied.
    """
    workspace_root = Path(ctx.context.workspace_path).resolve()

    try:
        ops = _parse_patch_envelope(patch)
    except ValueError as exc:
        logger.error("apply_patch_parse_failed", error=str(exc))
        return f"Error: failed to parse patch: {exc}"

    if not ops:
        return "Error: empty patch — no file operations found."

    # Phase 1: resolve and apply every operation in memory (no disk writes yet),
    # so the whole patch is atomic.
    changes: list[_FileChange] = []
    for op in ops:
        try:
            changes.append(_resolve_change(workspace_root, op))
        except (ValueError, OSError) as exc:
            logger.error("apply_patch_op_failed", path=op.path, op=op.op_type, error=str(exc))
            return f"Error: failed to apply {op.op_type} on {op.path}: {exc}"

    # Phase 2: commit to disk.
    for change in changes:
        if change.write_target is not None:
            change.write_target.parent.mkdir(parents=True, exist_ok=True)
            change.write_target.write_text(change.new_content, encoding="utf-8")
        if change.delete_source is not None:
            change.delete_source.unlink(missing_ok=True)

    # Phase 3: emit one diff artifact per file for the UI timeline.
    session_id = ctx.context.session_id
    for change in changes:
        event = artifact_diff(
            session_id,
            file_path=change.display_path,
            diff=change.diff,
            change_type=change.change_type,
            lines_added=change.lines_added,
            lines_removed=change.lines_removed,
            old_content=change.old_content,
            new_content=change.new_content,
            language=_guess_language(change.display_path),
        )
        await event_bus.publish(session_id, event)
        await session_manager.add_timeline_event_async(
            session_id=session_id,
            event_type=event.type.value,
            event_id=event.event_id,
            payload=event.payload,
        )

    total_added = sum(c.lines_added for c in changes)
    total_removed = sum(c.lines_removed for c in changes)
    logger.info(
        "apply_patch_applied",
        files=len(changes),
        lines_added=total_added,
        lines_removed=total_removed,
    )
    return _format_summary(changes)


def _parse_patch_envelope(patch: str) -> list[_PatchOp]:
    """Split a V4A patch envelope into per-file operations.

    Each operation owns the body lines that follow its header up to the next
    section marker or the end of the patch.
    """
    lines = patch.splitlines()

    # Skip everything up to and including the optional Begin Patch marker.
    start = 0
    for idx, line in enumerate(lines):
        if line.strip() == _BEGIN_PATCH:
            start = idx + 1
            break

    ops: list[_PatchOp] = []
    current: _PatchOp | None = None
    for line in lines[start:]:
        stripped = line.strip()
        if stripped == _END_PATCH:
            break
        if line.startswith(_ADD_FILE):
            current = _PatchOp("create", line[len(_ADD_FILE):].strip())
            ops.append(current)
        elif line.startswith(_UPDATE_FILE):
            current = _PatchOp("update", line[len(_UPDATE_FILE):].strip())
            ops.append(current)
        elif line.startswith(_DELETE_FILE):
            current = _PatchOp("delete", line[len(_DELETE_FILE):].strip())
            ops.append(current)
        elif line.startswith(_MOVE_TO) and current is not None and current.op_type == "update":
            current.move_to = line[len(_MOVE_TO):].strip()
        elif current is not None:
            current.body.append(line)

    for op in ops:
        if not op.path:
            raise ValueError("a file section is missing its path")
    return ops


def _resolve_change(workspace_root: Path, op: _PatchOp) -> _FileChange:
    """Apply a single operation in memory and compute its diff/stats."""
    if op.op_type == "create":
        target = _resolve_in_workspace(workspace_root, op.path)
        new_content = apply_diff("", "\n".join(op.body), mode="create")
        diff, added, removed = _compute_diff("", new_content)
        return _FileChange(
            change_type="create",
            display_path=op.path,
            write_target=target,
            delete_source=None,
            old_content="",
            new_content=new_content,
            diff=diff,
            lines_added=added,
            lines_removed=removed,
        )

    if op.op_type == "delete":
        source = _resolve_in_workspace(workspace_root, op.path)
        if not source.is_file():
            raise ValueError("file does not exist")
        old_content = source.read_text(encoding="utf-8")
        diff, added, removed = _compute_diff(old_content, "")
        return _FileChange(
            change_type="delete",
            display_path=op.path,
            write_target=None,
            delete_source=source,
            old_content=old_content,
            new_content="",
            diff=diff,
            lines_added=added,
            lines_removed=removed,
        )

    # update (optionally a rename via move_to)
    source = _resolve_in_workspace(workspace_root, op.path)
    if not source.is_file():
        raise ValueError("file does not exist")
    old_content = source.read_text(encoding="utf-8")
    new_content = apply_diff(old_content, "\n".join(op.body), mode="default")
    diff, added, removed = _compute_diff(old_content, new_content)

    if op.move_to:
        target = _resolve_in_workspace(workspace_root, op.move_to)
        return _FileChange(
            change_type="update",
            display_path=op.move_to,
            write_target=target,
            delete_source=source if target != source else None,
            old_content=old_content,
            new_content=new_content,
            diff=diff,
            lines_added=added,
            lines_removed=removed,
        )

    return _FileChange(
        change_type="update",
        display_path=op.path,
        write_target=source,
        delete_source=None,
        old_content=old_content,
        new_content=new_content,
        diff=diff,
        lines_added=added,
        lines_removed=removed,
    )


def _resolve_in_workspace(workspace_root: Path, relative: str) -> Path:
    """Resolve a relative path inside the workspace, rejecting traversal."""
    candidate = Path(relative)
    target = candidate if candidate.is_absolute() else (workspace_root / candidate)
    target = target.resolve()
    try:
        target.relative_to(workspace_root)
    except ValueError:
        raise ValueError(f"path is outside the workspace: {relative}") from None
    return target


def _compute_diff(before: str, after: str) -> tuple[str, int, int]:
    """Build a unified diff string and count added/removed lines via difflib."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    diff_lines = list(difflib.unified_diff(before_lines, after_lines, lineterm="", n=3))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return "\n".join(diff_lines), added, removed


def _guess_language(file_path: str) -> str:
    """Map a file extension to a Monaco language id for syntax highlighting."""
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    return _LANGUAGE_BY_EXT.get(ext, "plaintext")


def _format_summary(changes: list[_FileChange]) -> str:
    """Produce a concise English summary returned to the model."""
    verb = {"create": "Created", "update": "Updated", "delete": "Deleted"}
    parts = [
        f"{verb[c.change_type]} {c.display_path} (+{c.lines_added}/-{c.lines_removed})"
        for c in changes
    ]
    return f"Applied patch to {len(changes)} file(s): " + "; ".join(parts)
