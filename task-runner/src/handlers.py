"""Task kind dispatch — which function serves which task kind.

The registry is explicit rather than derived from names, because it is also a
security statement: a runner asked for a kind it does not implement must refuse.
Deriving the handler from the kind string would turn a typo, or a spec forged by
something that got hold of the ConfigMap, into an attempt to call arbitrary
code paths.

The ``sql_only`` flag per kind is what separates L2 from L3-base work:

* ``sqlmesh.plan`` / ``sqlmesh.apply`` / ``sqlmesh.query`` run in L2, which
  holds the WRITABLE warehouse account, so the project must contain no
  host-language code (checked before the Context loads);
* ``sqlmesh.plan_with_python`` and ``sqlmesh.render_project`` run in L3-base with
  a READ-ONLY account, so Python models are expected and permitted.

The write split (design 04 §3.5, Q5)
------------------------------------
An ``apply`` on a project containing Python models needs both a writable account
and the execution of user code, which Q5 forbids combining. Two kinds implement
the split:

  ``sqlmesh.render_project``  L3-base, read-only  — executes the Python, writes an
                                                    SQL-only copy of the project
  ``sqlmesh.apply``           L2, writable        — applies that copy

Phase 2 keeps its ``sql_only`` check switched ON. The rendered project genuinely
contains no Python, so the check passes on its own merits; a design where L2 had
to skip the check for this case would have moved the risk rather than removed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from datacode_common.task_protocol import ERR_UNSUPPORTED_KIND

from src import shell as shell_ops
from src import sqlmesh_ops
from src.sqlmesh_ops import ProgressFn, SqlMeshOpError

# A handler receives (workspace_path, params) plus keyword context and returns
# pure data. Anything not expressible as data must be reduced first.
HandlerFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class Handler:
    """One task kind's implementation and its execution constraints.

    Attributes:
        fn: The operation.
        sql_only: Enforce "no host-language code in the project" before running.
            True for every kind whose profile may hold a writable account.
        writes: Whether the task mutates the warehouse. Recorded so the audit
            trail distinguishes a failed read from a partially applied write.
    """

    fn: HandlerFn
    sql_only: bool
    writes: bool = False


_REGISTRY: dict[str, Handler] = {
    # ── L2: SQL only, writable warehouse account ──────────────────────
    "sqlmesh.plan": Handler(sqlmesh_ops.op_plan, sql_only=True),
    "sqlmesh.apply": Handler(sqlmesh_ops.op_apply, sql_only=True, writes=True),
    "sqlmesh.query": Handler(sqlmesh_ops.op_query, sql_only=True),
    # ── L3-base: user code expected, read-only warehouse account ──────
    "sqlmesh.plan_with_python": Handler(sqlmesh_ops.op_plan, sql_only=False),
    # Phase 1 of the write split. Reads and executes, never writes to the
    # warehouse — the output is a rendered project on the working copy.
    "sqlmesh.render_project": Handler(
        sqlmesh_ops.op_render_project, sql_only=False, writes=False
    ),
    # Agent shell (P6c): arbitrary commands, in whatever L3 vehicle the policy
    # picks — a container in production, the hardened process in dev. Result
    # shape is the one the executor's shell op used to return (that op is gone;
    # shell is a task now), so the agent tool schema is unchanged.
    "agent.shell": Handler(shell_ops.op_shell, sql_only=False, writes=False),
}

# Parameter through which a caller selects an alternative project directory. Read
# from the spec's params rather than added to the protocol as a field, because it
# is meaningful to exactly one kind pair and a protocol field would suggest every
# task may redirect the engine.
PARAM_PROJECT_SUBDIR = "project_subdir"


def supported_kinds() -> tuple[str, ...]:
    """Task kinds this runner implements, for diagnostics and health checks."""
    return tuple(sorted(_REGISTRY))


def resolve(kind: str) -> Handler:
    """Return the handler for a task kind.

    Raises:
        SqlMeshOpError: Not implemented here, tagged ``unsupported_kind`` so the
            submitter can tell "wrong image" apart from "the task failed".
    """
    handler = _REGISTRY.get(kind)
    if handler is None:
        raise SqlMeshOpError(
            ERR_UNSUPPORTED_KIND,
            f"task kind {kind!r} is not implemented by this runner; "
            f"supported: {', '.join(supported_kinds())}",
        )
    return handler


def run(
    kind: str,
    workspace_path: str,
    params: Mapping[str, Any],
    *,
    gateway: str = "",
    environment: str = "",
    progress: Optional[ProgressFn] = None,
) -> dict[str, Any]:
    """Execute one task kind and return its pure-data result."""
    handler = resolve(kind)
    kwargs: dict[str, Any] = {
        "gateway": gateway,
        "environment": environment,
        "sql_only": handler.sql_only,
        # Validated inside resolve_sqlmesh_root: a value escaping the working copy
        # is refused there, so a forged spec cannot point the engine elsewhere.
        "project_subdir": str(params.get(PARAM_PROJECT_SUBDIR) or ""),
    }
    if progress is not None:
        kwargs["progress"] = progress
    return handler.fn(workspace_path, params, **kwargs)


__all__ = [
    "PARAM_PROJECT_SUBDIR",
    "Handler",
    "HandlerFn",
    "resolve",
    "run",
    "supported_kinds",
]
