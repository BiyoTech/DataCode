"""Working-copy storage contract (design 07 ADR-003 / Q7).

A working copy is DERIVED state: Git is the only source of truth, so the
directory a runtime materializes into is disposable. That makes the medium a
deployment concern rather than a code concern, and this module exists to keep
it that way:

* there is exactly ONE materialization code path (clone --bare -> .repo ->
  worktree add). It takes a writable directory and never asks what is behind
  it;
* what IS behind it is *declared* here as a :class:`MountKind`, purely so the
  process can REFUSE a combination that silently corrupts working copies.

Why a declaration is needed at all
----------------------------------
The single-writer invariant (design 05 §5.2 I1) is upheld by the workspace
LEASE, not by the filesystem. Leases and media are therefore not independent:
each one is only safe in the presence of the matching other half.

    mount_kind      lease enabled   verdict
    -------------   -------------   ---------------------------------------
    ephemeral       yes             OK   private dir + single writer
    instance_local  yes             OK   private dir + single writer
    shared          yes, distributed      OK*  the P4a transitional shape:
                         env lock        the lease routes the happy path and
                                          the Redis env lock still serializes
                                          the degradation path (Redis outage +
                                          replica churn). A warning, because
                                          4b moves to instance_local and lets
                                          the lock degrade to in-process.
    shared          yes, in-process  FATAL a second replica can write behind
                         env lock          the lease's back with nothing left
                                          to serialize it
    ephemeral       no              FATAL replicas serve the SAME workspace
    instance_local  no                    from DIFFERENT private dirs, so
                                          writes silently diverge
    shared          no              OK   the 1.x status quo

Fatal vs. degraded
------------------
Only combinations that are wrong *by construction* are fatal, and both fatal
cells above lose user data without raising anything at the time of the write —
refusing to boot is the last place an operator can still be told. A shared
medium guarded only by in-process env locks is different: it is the existing
1.x behaviour, it degrades rather than corrupts silently (the env lock keeps
its Redis path and Git's own worktree locks still refuse a double checkout),
and failing startup on the status quo would be a regression. That case is
reported as a warning on the returned contract instead.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

__all__ = [
    "MountKind",
    "MountContractError",
    "StorageContract",
    "resolve_mount_kind",
    "validate_storage_contract",
]


class MountKind(str, Enum):
    """What the medium behind ``materialize.root`` actually is.

    The materialization code never branches on this value; only the startup
    check below reads it. It exists to make an unsafe deployment impossible,
    not to select an implementation.
    """

    # emptyDir. Nothing survives a pod restart; recovery is the WIP ref.
    # Cheapest and fastest to start, lowest cache hit rate.
    EPHEMERAL = "ephemeral"

    # A volume bound to THIS replica only (StatefulSet volumeClaimTemplates,
    # ReadWriteOnce). Working copies and .repo survive rolling updates and
    # OOMKills, so those events stop consuming the Q6 durability window.
    # Recommended for production.
    INSTANCE_LOCAL = "instance_local"

    # A volume writable by several replicas (NFS/EFS/RWX PVC). The pre-lease
    # status quo. Incompatible with leases -- see module docstring.
    SHARED = "shared"

    def is_replica_private(self) -> bool:
        """Whether each replica gets its own copy of the directory."""
        return self is not MountKind.SHARED


class MountContractError(RuntimeError):
    """Raised when the declared storage contract cannot be served safely.

    Always fatal at startup: every case it reports is one that would otherwise
    lose or diverge user data without surfacing an error at write time.
    """


class StorageContract:
    """The validated combination of medium, lease routing and env locking.

    Construct it once during startup and let it fail the process. Holding the
    validated triple as one object keeps the invariant in a single place
    instead of spreading `if mount_kind == ...` over the call sites -- which
    is exactly what this design is meant to avoid.
    """

    def __init__(
        self,
        *,
        root: str,
        mount_kind: MountKind,
        lease_enabled: bool,
        distributed_env_lock: bool,
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.root = root
        self.mount_kind = mount_kind
        self.lease_enabled = lease_enabled
        self.distributed_env_lock = distributed_env_lock
        # Configurations that are allowed but weaker than the target shape.
        # Surfaced so startup can log them rather than hiding the trade-off.
        self.warnings = warnings

    @property
    def resolved_root(self) -> Path:
        """``materialize.root`` as an absolute path (``~`` expanded)."""
        return Path(self.root).expanduser()

    def describe(self) -> dict:
        """Structured form for startup logs and /health."""
        return {
            "root": str(self.resolved_root),
            "mount_kind": self.mount_kind.value,
            "lease_enabled": self.lease_enabled,
            "distributed_env_lock": self.distributed_env_lock,
            "replica_private": self.mount_kind.is_replica_private(),
            "warnings": list(self.warnings),
        }


def resolve_mount_kind(value: str | MountKind) -> MountKind:
    """Parse a configured ``mount_kind``, rejecting anything unrecognized.

    An unknown value must not fall back to a default: the whole point of the
    declaration is that the operator states what the medium is, and guessing
    on their behalf reintroduces the silent-corruption path.
    """
    if isinstance(value, MountKind):
        return value

    normalized = (value or "").strip().lower()
    if not normalized:
        raise MountContractError(
            "materialize.mount_kind is not set. Declare what the medium behind "
            f"materialize.root is, one of: {_known_kinds()}."
        )
    try:
        return MountKind(normalized)
    except ValueError as exc:
        raise MountContractError(
            f"unknown materialize.mount_kind {value!r}; expected one of: "
            f"{_known_kinds()}."
        ) from exc


def validate_storage_contract(
    *,
    root: str,
    mount_kind: str | MountKind,
    lease_enabled: bool,
    distributed_env_lock: bool,
) -> StorageContract:
    """Validate the medium against the guarantees it depends on.

    Raises :class:`MountContractError` for the two combinations that lose data
    silently. Returns the validated contract otherwise, with ``warnings``
    describing any allowed-but-weaker choice.

    ``distributed_env_lock`` is only consulted for ``shared``, where it is the
    sole remaining serialization once the lease is absent. Its absence is a
    warning rather than a failure: that is the pre-P4 status quo, and refusing
    to boot on it would break existing deployments.
    """
    kind = resolve_mount_kind(mount_kind)

    if not str(root or "").strip():
        raise MountContractError(
            "materialize.root must be a non-empty writable directory path."
        )

    if lease_enabled and kind is MountKind.SHARED and not distributed_env_lock:
        raise MountContractError(
            "materialize.mount_kind='shared' cannot be combined with workspace "
            "leases while environment locks are in-process. The lease is what "
            "makes in-process locks sufficient; a medium writable by several "
            "replicas lets a second writer in behind the lease's back, so "
            "working copies are corrupted with no error at write time. Keep "
            "the Redis env lock while on a shared volume (the P4a transitional "
            "shape), or move to 'instance_local' / 'ephemeral'."
        )

    if not lease_enabled and kind.is_replica_private():
        raise MountContractError(
            f"materialize.mount_kind={kind.value!r} requires workspace leases to "
            "be enabled. Without lease routing, requests for one working copy "
            "reach any replica, and each replica would serve it from its OWN "
            "private directory -- the copies diverge and writes are lost "
            "silently. Enable the lease subsystem, or declare 'shared' while "
            "still running on a shared volume."
        )

    warnings: list[str] = []
    if kind is MountKind.SHARED and lease_enabled:
        # Allowed (distributed lock held), but this is the P4a transitional
        # shape: the design moves to instance_local in 4b, at which point the
        # env lock may degrade to in-process. Say so, because staying here
        # forever would keep the Redis lock on the hot path.
        warnings.append(
            "mount_kind='shared' with leases enabled is the P4a transitional "
            "shape: the Redis env lock must stay enabled to serialize the "
            "degradation path. Move to 'instance_local' (per-replica RWO "
            "volume) in P4b."
        )
    elif kind is MountKind.SHARED and not distributed_env_lock:
        warnings.append(
            "mount_kind='shared' with a non-distributed environment lock: "
            "concurrent writes from different replicas are serialized only by "
            "Git's own worktree locks. Set concurrency.env_lock_backend to "
            "'redis' (or 'auto' with Redis reachable), or move to "
            "'instance_local' once leases are enabled."
        )

    return StorageContract(
        root=root,
        mount_kind=kind,
        lease_enabled=lease_enabled,
        distributed_env_lock=distributed_env_lock,
        warnings=tuple(warnings),
    )


def _known_kinds() -> str:
    return ", ".join(repr(kind.value) for kind in MountKind)


def mount_kind_from_env(default: str = "") -> str:
    """Read ``DATACODE_MATERIALIZE_MOUNT_KIND`` for deployment-level override.

    Kept here so the env var name has one definition; services still pass the
    value through their own settings layer.
    """
    return os.environ.get("DATACODE_MATERIALIZE_MOUNT_KIND", default)
