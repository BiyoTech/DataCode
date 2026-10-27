"""Structured stdout for the runner — the result channel (design 04 §3).

Everything the caller must reliably receive goes through here, prefixed so it
can be told apart from the noise the task itself writes (SQLMesh progress bars,
user ``print()`` calls, engine warnings).

Two invariants make the channel trustworthy:

* **Every structured line is flushed.** A container can be killed the moment
  after it exits; a buffered result line would be lost exactly when it matters
  most (a task that OOMed or hit its deadline).
* **Task output never reaches stdout unfiltered.** The runner writes its own
  lines to stdout and pushes the task's own chatter to stderr, so a task that
  prints something looking like a structured line cannot forge a result.
"""

from __future__ import annotations

import sys
from typing import Any, Mapping

from datacode_common.task_protocol import (
    TaskProgress,
    TaskResult,
    encode_stream_line,
)


def emit(payload: Mapping[str, Any]) -> None:
    """Write one structured line to stdout and flush it."""
    print(encode_stream_line(payload), file=sys.stdout, flush=True)


def emit_progress(
    task_id: str, stage: str, percent: int | None = None, detail: str = ""
) -> None:
    """Report progress. Best-effort by contract: losing it changes nothing."""
    emit(TaskProgress(task_id=task_id, stage=stage, percent=percent, detail=detail).to_dict())


def emit_result(result: TaskResult) -> None:
    """Report the final outcome. Exactly one of these per run."""
    emit(result.to_dict())


def log(message: str) -> None:
    """Human-readable diagnostics.

    Goes to STDERR on purpose: stdout is the result channel, and mixing the two
    is how a chatty task ends up corrupting a result.
    """
    print(message, file=sys.stderr, flush=True)


__all__ = ["emit", "emit_progress", "emit_result", "log"]
