"""Local single-mount guard — the lease's second line of defence (design 05 §5.2).

Redis ``SET NX`` decides who *should* own a working copy. This module enforces,
on the machine that actually holds the files, that only one process ever does.
The two are not redundant:

* Redis is a coordination backend the platform must survive losing. While it is
  down the gateway falls back to stable-hash routing, and a replica-count
  change during that window can route one working copy to two instances;
* an ``flock`` is held by the kernel, so it keeps working exactly when the
  coordination layer does not.

What this guard does and does not cover
---------------------------------------
``flock`` is scoped to one kernel, hence to one node. It therefore covers:

* two processes on the same node (the common case: a rolling update where the
  old pod has not exited, or a stale executor child);
* a re-entrant acquisition inside one process (a bug, caught immediately here
  rather than as corrupted working copies later).

It does NOT cover two different nodes. That case is left to the lease itself
plus Git's own worktree locking, which refuses a second checkout of a branch —
the residual risk accepted in design 05 §5.2. The value of this layer is that
it converts the most likely double-mount into an immediate, attributable
failure instead of interleaved writes to one worktree.

Why the lock file lives beside the working copy, not in ``/var/run``
-------------------------------------------------------------------
The lock must share the lifetime of the thing it protects. A lock keyed by path
under ``/var/run`` would vanish on restart while the working copy survived
(``instance_local``), and would survive while the working copy vanished
(``ephemeral``) — both leave the guard describing something that is no longer
true. Placing it inside the user root ties the two together by construction.
"""

from __future__ import annotations

import errno
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path

from datacode_common.naming import split_lease_key

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Name of the lock file inside a user root. Dot-prefixed so it never collides
# with an environment name under ``envs/``.
LOCK_FILENAME = ".datacode-lease"


class SingleMountViolation(RuntimeError):
    """Raised when this process cannot prove exclusive ownership of a copy.

    Callers must treat this as "do not touch these files" and surrender the
    lease, so the gateway retries on the instance that does hold the lock.
    Proceeding anyway is the one outcome that corrupts user data silently.
    """

    def __init__(self, lease_key: str, lock_path: Path, holder: str | None = None):
        self.lease_key = lease_key
        self.lock_path = lock_path
        self.holder = holder
        held_by = f" (held by {holder})" if holder else ""
        super().__init__(
            f"working copy {lease_key!r} is already mounted by another "
            f"process{held_by}; refusing to serve it from this instance"
        )


@dataclass
class MountGuard:
    """An acquired exclusive claim on one working copy.

    Held for the lifetime of the lease, not the lifetime of one operation:
    the invariant being protected is "one writer for this working copy", which
    spans every request routed to this instance for that lease.
    """

    lease_key: str
    lock_path: Path
    _fd: int

    def release(self) -> None:
        """Release the claim. Idempotent.

        The lock file itself is deliberately left in place: unlinking it races
        with another instance that has already opened the same path and is
        waiting on the lock, which would hand the lock on a now-orphaned inode
        to two processes at once. An empty file costs nothing.
        """
        if self._fd < 0:
            return
        fd, self._fd = self._fd, -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:  # pragma: no cover - unlock of a live fd
            logger.warning(
                "mount_guard_unlock_failed", lease_key=self.lease_key, error=str(exc)
            )
        finally:
            os.close(fd)
        logger.info("mount_guard_released", lease_key=self.lease_key)

    def __enter__(self) -> "MountGuard":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.release()


def lock_path_for(materialize_root: str, lease_key: str) -> Path:
    """Return the lock file path for one working copy.

    Keyed by the user root (``{root}/{tenant}/{workspace}/{user}``) rather than
    the environment directory, because the ``.repo`` object database is shared
    by all of that user's worktrees: two environments of one user must not be
    materialized concurrently while ``.repo`` is being created.
    """
    tenant_id, workspace_id, user_id, _environment = split_lease_key(lease_key)
    return (
        Path(materialize_root).expanduser()
        / tenant_id
        / workspace_id
        / user_id
        / LOCK_FILENAME
    )


def acquire_mount(materialize_root: str, lease_key: str) -> MountGuard:
    """Claim exclusive local ownership of a working copy, or raise.

    Non-blocking on purpose. Waiting would turn a routing mistake into a
    request that hangs until a timeout somewhere else fires; failing straight
    away lets the caller surrender the lease and the gateway retry against the
    real holder, which is both faster and diagnosable.

    Raises:
        SingleMountViolation: another process holds the claim.
        OSError: the lock file could not be created (e.g. the mount is
            read-only) — a deployment fault that must not be mistaken for
            contention.
    """
    path = lock_path_for(materialize_root, lease_key)
    path.parent.mkdir(parents=True, exist_ok=True)

    # O_CLOEXEC: executor children (design 04 §2.3) must not inherit the fd.
    # An inherited fd keeps the lock alive after this process dies, which would
    # make a crashed instance's working copy permanently unclaimable.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        holder = _read_holder(fd)
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            logger.warning(
                "mount_guard_contended",
                lease_key=lease_key,
                lock_path=str(path),
                holder=holder,
            )
            raise SingleMountViolation(lease_key, path, holder) from exc
        raise

    _write_holder(fd)
    logger.info("mount_guard_acquired", lease_key=lease_key, lock_path=str(path))
    return MountGuard(lease_key=lease_key, lock_path=path, _fd=fd)


def _write_holder(fd: int) -> None:
    """Record who holds the lock, for diagnosis only.

    The kernel owns the mutual exclusion; this content is what turns "someone
    else has it" into an actionable log line. Never read as authority.
    """
    identity = f"{os.getenv('DATACODE_REPLICA_ID') or os.uname().nodename}:{os.getpid()}"
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        os.write(fd, identity.encode())
    except OSError as exc:  # pragma: no cover - diagnostics must not fail ops
        logger.debug("mount_guard_holder_write_failed", error=str(exc))


def _read_holder(fd: int) -> str | None:
    """Best-effort read of the current holder's identity."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 256).decode(errors="replace").strip() or None
    except OSError:  # pragma: no cover
        return None
