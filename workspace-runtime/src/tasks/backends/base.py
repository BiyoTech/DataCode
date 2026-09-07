"""Task execution backends — the vehicles a task can run in.

A backend answers one question: given a validated spec and the isolation profile
it must honour, produce a :class:`TaskResult`. Everything above it (choosing the
profile, auditing, timeouts as policy) belongs to the submitter; everything below
it (Jobs, forks, mounts) is the backend's own business.

The interface is deliberately narrow, because P6c adds a third vehicle (a warm
sandbox reused across tasks) and P7 may add a fourth (a remote cell). A wide
interface would have leaked Kubernetes vocabulary into all of them.

Contract every backend must uphold:

1.  **Always return a result, never raise for task-level failure.** A task that
    failed, timed out or produced no result at all is data the caller records;
    an exception would force every call site to reinvent that mapping.
2.  **The structured result line is authoritative.** Exit codes and Job statuses
    are corroborating evidence, used only when no result line was produced.
3.  **Clean up on every path.** A backend that returns must leave no running
    vehicle behind, including when it timed out or was cancelled.
4.  **Secrets travel out of band.** The credential environment is passed
    separately from the spec because the spec is stored in a ConfigMap and written
    to audit records, while these values must reach the sandbox through a
    memory-backed Secret. Merging them into ``spec.params`` would defeat the
    credential check the protocol performs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from datacode_common.isolation import IsolationLevel, IsolationProfile
from datacode_common.task_protocol import TaskResult, TaskSpec

from src.tasks.credentials import SandboxCredentials


class TaskBackendError(RuntimeError):
    """The backend itself is unusable — misconfiguration, not task failure.

    Raised at startup or on submission (no cluster reachable, no image
    configured, no working-copy mount available). Distinct from a failed task on
    purpose: a task failure is a user-visible outcome, while this is an operator
    problem that must not be recorded as "the user's plan failed".
    """


class TaskBackend(ABC):
    """A vehicle that can execute one task."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier used in logs and audit records."""

    @property
    @abstractmethod
    def supported_levels(self) -> frozenset[IsolationLevel]:
        """Isolation levels this backend can actually provide."""

    @abstractmethod
    async def execute(
        self,
        spec: TaskSpec,
        profile: IsolationProfile,
        credentials: Optional[SandboxCredentials] = None,
    ) -> TaskResult:
        """Run the task to completion and return its result.

        Args:
            spec: Validated, credential-free task description.
            profile: Isolation profile to honour.
            credentials: Task token and proxy configuration to inject. ``None``
                means the sandbox gets none, which under the NetworkPolicy leaves
                it with no egress — a valid state for a task that needs none.

        Must not raise for task-level failure; see the module contract.
        """

    async def close(self) -> None:
        """Release backend-held resources. Idempotent."""


__all__ = ["TaskBackend", "TaskBackendError"]
