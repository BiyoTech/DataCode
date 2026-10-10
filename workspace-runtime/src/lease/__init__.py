"""Workspace lease client (design 03 §3.3).

The holder side of the lease: it proves and defends this instance's exclusive
ownership of a working copy, and publishes the capacity that lets the
controller place new leases here.

The shared vocabulary (lease keys, Redis layout, TTL semantics) lives in
:mod:`datacode_common.lease`; granting and placement policy belong to
control-service.
"""

from src.lease.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TTL_INTERVALS,
    HeartbeatReporter,
)
from src.lease.holder import LeaseHolder, LeaseUnavailable, runtime_id
from src.lease.manager import LeaseManager, RepositorySource, ServedCopy
from src.lease.materialize import (
    MaterializationError,
    MaterializedCopy,
    discard,
    is_valid_bare_repo,
    is_valid_worktree,
    materialize,
)
from src.lease.mount_guard import (
    LOCK_FILENAME,
    MountGuard,
    SingleMountViolation,
    acquire_mount,
    lock_path_for,
)
from src.lease.wip import (
    WIP_REF_PREFIX,
    RecoveredSnapshot,
    SnapshotResult,
    SnapshotScheduler,
    apply_snapshot,
    discard_snapshot,
    find_snapshot,
    take_snapshot,
    wip_ref,
    wip_ref_for_lease,
)

__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_TTL_INTERVALS",
    "HeartbeatReporter",
    "LOCK_FILENAME",
    "LeaseHolder",
    "LeaseManager",
    "LeaseUnavailable",
    "MaterializationError",
    "MaterializedCopy",
    "MountGuard",
    "RecoveredSnapshot",
    "RepositorySource",
    "ServedCopy",
    "SingleMountViolation",
    "SnapshotResult",
    "SnapshotScheduler",
    "WIP_REF_PREFIX",
    "acquire_mount",
    "apply_snapshot",
    "discard",
    "discard_snapshot",
    "find_snapshot",
    "is_valid_bare_repo",
    "is_valid_worktree",
    "lock_path_for",
    "materialize",
    "runtime_id",
    "take_snapshot",
    "wip_ref",
    "wip_ref_for_lease",
]
