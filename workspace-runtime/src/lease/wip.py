"""WIP snapshots — bounding what a non-graceful crash can lose (design 07 Q6).

Working copies live on replica-private storage, so a pod that disappears takes
its uncommitted edits with it. The platform's answer is a **user-facing
durability contract**, not a best effort:

    editor keystrokes            <= 5 s   (frontend drafts -> control-service)
    written but uncommitted, graceful drain    0   (forced snapshot)
    written but uncommitted, hard crash  <= 10 min (periodic snapshot)
    committed but unpushed              0   (lives in .repo, snapshotted too)

This module owns the last two rows. ``wip_snapshot_interval_seconds`` is
therefore NOT a tuning knob: lowering it changes what the product promises, and
raising it silently weakens a published guarantee.

Why a hidden ref rather than a branch or a stash entry
-----------------------------------------------------
* ``git stash create`` (not ``push``) builds the commit without touching the
  index or working tree. A periodic ``stash push`` would repeatedly yank the
  user's edits out from under their editor — far worse than what it prevents.
* the snapshot is pushed to ``refs/datacode-wip/{user}/{env}``, outside
  ``refs/heads/*``, so it never appears in a branch picker, is not fetched by a
  plain clone, and cannot be mistaken for the user's own work.
* it lives on the **Git server**, because the whole point is to survive the
  disappearance of this node's disk.

Failure is loud on purpose
--------------------------
A snapshot that silently stops working is worse than having none: the user
believes they are protected. So every failure is reported, and the last
successful snapshot time is exposed for the frontend's "last protected at"
indicator.
"""

from __future__ import annotations

import asyncio
import typing as t
from dataclasses import dataclass
from datetime import datetime, timezone

from datacode_common.naming import split_lease_key

from src.git.ops import GitAuth, GitOperations, GitWorktreeOperations
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Hidden ref namespace. Deliberately NOT under refs/heads/ or refs/stash.
WIP_REF_PREFIX = "refs/datacode-wip"

# Local ref the snapshot is fetched into when recovering, so the commit is
# reachable (and therefore not garbage-collected) before it is applied.
_WIP_LOCAL_REF = "refs/datacode-wip-recovered"


def wip_ref(user_id: str, environment: str) -> str:
    """Hidden ref holding one working copy's latest snapshot."""
    return f"{WIP_REF_PREFIX}/{user_id}/{environment}"


def wip_ref_for_lease(lease_key: str) -> str:
    """Hidden ref for a lease key."""
    _tenant, _workspace, user_id, environment = split_lease_key(lease_key)
    return wip_ref(user_id, environment)


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of one snapshot attempt."""

    lease_key: str
    # Empty when the working copy was clean: nothing to protect, which is a
    # success, not a skip-because-of-error.
    commit: str
    taken_at: datetime
    # False only when the working copy was dirty AND the snapshot did not
    # reach the server. This is the state the user must be warned about.
    protected: bool
    detail: str = ""

    @property
    def was_clean(self) -> bool:
        return not self.commit


@dataclass(frozen=True)
class RecoveredSnapshot:
    """A snapshot found on the server when materializing a working copy."""

    lease_key: str
    commit: str
    ref: str
    # When the snapshot commit was created, read from the commit object itself
    # (the only clock that was present when it happened), normalized to UTC.
    # ``None`` when unreadable; the recovery prompt then omits the age hint
    # rather than guessing.
    taken_at: t.Optional[datetime] = None


def take_snapshot(
    *,
    lease_key: str,
    workspace_path: str,
    repo_path: str,
    auth: t.Optional[GitAuth] = None,
) -> SnapshotResult:
    """Snapshot uncommitted work to the Git server, changing nothing locally.

    Safe to call at any moment, including mid-edit: it neither stages nor
    reverts anything. A clean working copy costs one ``git stash create`` and no
    network round trip, which is what makes a 10-minute cadence affordable for
    thousands of idle leases.
    """
    now = datetime.now(timezone.utc)

    commit = GitWorktreeOperations.stash_create(workspace_path)
    if not commit:
        # Clean tree (or a stash failure, already logged). Nothing at risk.
        return SnapshotResult(
            lease_key=lease_key, commit="", taken_at=now, protected=True
        )

    ref = wip_ref_for_lease(lease_key)
    # Force: each snapshot supersedes the previous one without descending from
    # it, so a fast-forward check would reject every update after the first.
    result = GitWorktreeOperations.push_ref(
        repo_path, commit, ref, auth=auth, force=True
    )
    if not result.ok:
        detail = result.detail or result.reason or "push failed"
        # Loud: the user currently believes they are protected and are not.
        logger.error(
            "wip_snapshot_failed",
            lease_key=lease_key,
            ref=ref,
            commit=commit[:8],
            detail=detail,
        )
        return SnapshotResult(
            lease_key=lease_key,
            commit=commit,
            taken_at=now,
            protected=False,
            detail=detail,
        )

    logger.info(
        "wip_snapshot_taken", lease_key=lease_key, ref=ref, commit=commit[:8]
    )
    return SnapshotResult(
        lease_key=lease_key, commit=commit, taken_at=now, protected=True
    )


def find_snapshot(
    *,
    lease_key: str,
    repo_path: str,
    auth: t.Optional[GitAuth] = None,
) -> t.Optional[RecoveredSnapshot]:
    """Look for a snapshot on the server. ``None`` when there is none.

    Called at materialization time. The absence of a ref is the normal case, so
    it is not an error. The snapshot is fetched into a LOCAL ref first: an
    unreferenced commit could be pruned between discovery and application.
    """
    ref = wip_ref_for_lease(lease_key)
    if not GitWorktreeOperations.fetch_ref(
        repo_path, ref, local_ref=_WIP_LOCAL_REF, auth=auth
    ):
        return None

    commit = GitWorktreeOperations.rev_parse(repo_path, _WIP_LOCAL_REF)
    if not commit:
        return None

    taken_at = GitWorktreeOperations.commit_timestamp(repo_path, commit)
    logger.info(
        "wip_snapshot_found", lease_key=lease_key, ref=ref, commit=commit[:8]
    )
    return RecoveredSnapshot(
        lease_key=lease_key, commit=commit, ref=ref, taken_at=taken_at
    )


def apply_snapshot(
    *, workspace_path: str, snapshot: RecoveredSnapshot
) -> bool:
    """Restore a snapshot into a working copy.

    Only ever called after the USER chose to recover (design 07 Q6 requires the
    prompt): silently applying it would resurrect edits someone deliberately
    abandoned, and could conflict with work done since.

    Refuses to run on a dirty working copy — merging a snapshot into unrelated
    uncommitted changes is exactly the situation with no safe automatic answer.
    """
    if GitOperations.list_changes(workspace_path):
        logger.warning(
            "wip_apply_refused_dirty_worktree",
            lease_key=snapshot.lease_key,
            commit=snapshot.commit[:8],
        )
        return False

    result = GitWorktreeOperations.stash_apply_commit(
        workspace_path, snapshot.commit
    )
    if not result:
        logger.error(
            "wip_apply_failed",
            lease_key=snapshot.lease_key,
            commit=snapshot.commit[:8],
        )
        return False

    logger.info(
        "wip_snapshot_applied",
        lease_key=snapshot.lease_key,
        commit=snapshot.commit[:8],
    )
    return True


def discard_snapshot(
    *,
    lease_key: str,
    repo_path: str,
    auth: t.Optional[GitAuth] = None,
) -> bool:
    """Drop a snapshot from the server after it was recovered or refused.

    Leaving it behind would make the recovery prompt reappear on every
    materialization, training users to dismiss it — which would defeat the
    mechanism the next time it mattered.
    """
    ref = wip_ref_for_lease(lease_key)
    deleted = GitWorktreeOperations.delete_remote_ref(repo_path, ref, auth=auth)
    if deleted:
        logger.info("wip_snapshot_discarded", lease_key=lease_key, ref=ref)
    return deleted


class SnapshotScheduler:
    """Runs periodic snapshots for every lease this instance holds.

    One task for all leases rather than one per lease: the work is IO-bound and
    proportional to the number of DIRTY copies, while a task per lease would
    make thousands of mostly-idle timers.
    """

    def __init__(
        self,
        *,
        interval_seconds: float,
        lease_provider: t.Callable[[], t.Iterable[tuple[str, str, str]]],
        auth_provider: t.Optional[
            t.Callable[[str], t.Optional[GitAuth]]
        ] = None,
    ) -> None:
        # (lease_key, workspace_path, repo_path) for each held lease.
        self._lease_provider = lease_provider
        self._auth_provider = auth_provider
        self._interval = float(interval_seconds)
        self._task: asyncio.Task[None] | None = None
        # Exposed for the frontend's "last protected at" indicator and for
        # degradation warnings.
        self._last_success: dict[str, datetime] = {}
        self._unprotected: set[str] = set()

    def last_protected_at(self, lease_key: str) -> t.Optional[datetime]:
        """When this working copy was last successfully snapshotted (UTC)."""
        return self._last_success.get(lease_key)

    def is_protected(self, lease_key: str) -> bool:
        """False when the last attempt left changes unprotected."""
        return lease_key not in self._unprotected

    def unprotected_leases(self) -> tuple[str, ...]:
        """Leases whose latest snapshot failed — surfaced via /health."""
        return tuple(sorted(self._unprotected))

    async def start(self) -> None:
        """Begin the snapshot loop. Idempotent."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="wip-snapshot")
        logger.info("wip_scheduler_started", interval_seconds=self._interval)

    async def stop(self, *, final_snapshot: bool = True) -> None:
        """Stop the loop, taking a final snapshot by default.

        ``final_snapshot`` is what turns a graceful drain into ZERO loss rather
        than "up to one interval". It is the difference between the two
        guarantees the contract publishes, so it defaults to on.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if final_snapshot:
            await self.snapshot_all(reason="drain")
        logger.info("wip_scheduler_stopped")

    async def snapshot_all(self, *, reason: str = "periodic") -> list[SnapshotResult]:
        """Snapshot every held lease once."""
        results: list[SnapshotResult] = []
        for lease_key, workspace_path, repo_path in self._lease_provider():
            auth = self._auth_provider(lease_key) if self._auth_provider else None
            try:
                result = await asyncio.to_thread(
                    take_snapshot,
                    lease_key=lease_key,
                    workspace_path=workspace_path,
                    repo_path=repo_path,
                    auth=auth,
                )
            except Exception as exc:  # noqa: BLE001 - one copy must not stop the rest
                logger.error(
                    "wip_snapshot_error",
                    lease_key=lease_key,
                    reason=reason,
                    error=str(exc),
                )
                self._unprotected.add(lease_key)
                continue

            results.append(result)
            if result.protected:
                self._unprotected.discard(lease_key)
                if not result.was_clean:
                    self._last_success[lease_key] = result.taken_at
            else:
                self._unprotected.add(lease_key)
        return results

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.snapshot_all()
