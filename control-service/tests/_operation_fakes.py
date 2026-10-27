"""In-memory stand-in for :class:`src.operations.recorder.OperationRecorder`.

For tests of the CALLERS (workspace manager, sweep): they must observe which
steps were recorded and which terminal status was reached, without a database.
The recorder itself is tested against real SQLite in ``test_operations.py``.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

from src.operations.recorder import OperationHandle


@dataclass
class MemoryRecorder:
    """Records every call as ``(op_id, event, payload)``."""

    events: list = field(default_factory=list)
    _next: int = 0

    async def begin(self, *, kind, workspace_id, tenant_id, actor_user_id, detail=None):
        self._next += 1
        op_id = f"op{self._next}"
        self.events.append((op_id, "begin", {"kind": kind, "workspace_id": workspace_id}))
        return OperationHandle(op_id=op_id)

    async def advance(self, handle, step, **detail):
        self._log(handle, "advance", {"step": step, **detail})

    async def succeed(self, handle, **detail):
        self._log(handle, "succeeded", detail)

    async def rolled_back(self, handle, **detail):
        self._log(handle, "rolled_back", detail)

    async def fail(self, handle, *, error):
        self._log(handle, "failed", {"error": error})

    def _log(self, handle: t.Optional[OperationHandle], event: str, payload: dict) -> None:
        if handle is not None:
            self.events.append((handle.op_id, event, payload))

    # ── views for assertions ───────────────────────────────────────────

    def steps(self) -> list:
        return [p["step"] for _, e, p in self.events if e == "advance"]

    def terminal(self) -> t.Optional[str]:
        terminals = [e for _, e, _ in self.events if e in ("succeeded", "rolled_back", "failed")]
        return terminals[-1] if terminals else None
