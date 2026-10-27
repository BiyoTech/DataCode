"""LRU disk reclamation for materialized working copies (design 06 4b).

Replica-private media (``instance_local``) make working copies a CACHE: they
survive restarts, so nothing else bounds their size. The reclaimer is that
bound — a watermark-driven loop that discards the LEAST RECENTLY USED
lease-less working copies until usage is back at the low watermark.

What is reclaimable, precisely
------------------------------
A worktree directory under ``{root}/{tenant}/{ws}/{user}/envs/{env}`` whose
lease key is NOT in the served set. The shared ``.repo`` object database is
deliberately kept (``materialize.discard`` removes only the worktree): it may
serve the same user's other environments and is the expensive part to refetch.

Uncommitted work is already protected before a copy can become reclaimable:
the lease was released (a release-time snapshot ran), so reclaiming a
lease-less tree discards nothing the durability contract covers.

Ordering
--------
In-memory last-use stamps (fed by :meth:`LeaseManager.serve`) are authoritative
while the process lives; after a restart every stamp is unknown and directory
mtime is the fallback. Both are epoch seconds, so they compare directly.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import typing as t

from datacode_common.naming import lease_key as build_lease_key

# Direct function import on purpose: `src.lease.__init__` re-exports a
# FUNCTION named `materialize`, which shadows the submodule attribute once the
# package is fully loaded — `from src.lease import materialize` would hand us
# the function depending on import order.
from src.lease.materialize import discard as _discard_worktree
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Depth from the materialize root to an ``envs`` directory: tenant/workspace/user.
_USER_ROOT_DEPTH = 3


class DiskReclaimer:
    """Periodically reclaims lease-less worktrees at the high watermark."""

    def __init__(
        self,
        *,
        materialize_root: str,
        served_keys: t.Callable[[], t.Iterable[str]],
        last_used: t.Callable[[], dict[str, float]],
        high_watermark_percent: float,
        low_watermark_percent: float,
        interval_seconds: float,
        disk_usage: t.Callable[[str], tuple[float, float]] | None = None,
    ) -> None:
        self._root = str(materialize_root)
        self._served_keys = served_keys
        self._last_used = last_used
        self._high = float(high_watermark_percent)
        self._low = float(low_watermark_percent)
        self._interval = float(interval_seconds)
        # Injectable for tests: returns (used_bytes, total_bytes).
        self._disk_usage = disk_usage or _filesystem_usage
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Begin the watermark loop. Idempotent."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="disk-reclaimer")
        logger.info(
            "disk_reclaimer_started",
            root=self._root,
            high_watermark_percent=self._high,
            low_watermark_percent=self._low,
            interval_seconds=self._interval,
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("disk_reclaimer_stopped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                # Reclamation is blocking filesystem work; run it off the loop.
                await asyncio.to_thread(self.reclaim_once)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                logger.error("disk_reclaim_error", error=str(exc))

    def used_percent(self) -> float:
        """Current usage of the materialize volume, in percent."""
        used, total = self._disk_usage(self._root)
        if total <= 0:
            return 0.0
        return used / total * 100.0

    def reclaim_once(self) -> list[str]:
        """One watermark pass. Returns the lease keys whose copies were dropped.

        No-op below the high watermark. Between watermarks it discards least-
        recently-used lease-less worktrees until usage is at/below the low
        watermark (or candidates run out — being above the low line with
        nothing reclaimable is a capacity fact to alert on, not an error here).
        """
        used = self.used_percent()
        if used <= self._high:
            return []

        served = set(self._served_keys())
        candidates = self._candidates(exclude=served)
        if not candidates:
            logger.warning(
                "disk_reclaim_no_candidates",
                used_percent=round(used, 1),
                high_watermark_percent=self._high,
            )
            return []

        reclaimed: list[str] = []
        for key, _stamp in candidates:
            if self.used_percent() <= self._low:
                break
            # Re-check the served set: a lease may have been taken since the
            # candidate list was built, and discarding a served copy would
            # rip the tree out from under its writer.
            if key in set(self._served_keys()):
                continue
            try:
                _discard_worktree(self._root, key)
            except Exception as exc:  # noqa: BLE001 - one victim must not stop the pass
                logger.error(
                    "disk_reclaim_discard_failed", lease_key=key, error=str(exc)
                )
                continue
            reclaimed.append(key)

        if reclaimed:
            logger.info(
                "disk_reclaimed",
                count=len(reclaimed),
                used_percent=round(self.used_percent(), 1),
                before_percent=round(used, 1),
                low_watermark_percent=self._low,
                lease_keys=reclaimed[:20],
            )
        return reclaimed

    # ── internals ─────────────────────────────────────────────────────

    def _candidates(
        self, *, exclude: set[str]
    ) -> list[tuple[str, float]]:
        """Lease-less worktrees under the root, least recently used first.

        ``exclude`` holds the served keys; a stamp of ``0.0`` sorts unknown
        entries first only when mtime is also unknown, which cannot happen for
        an existing directory — so ordering is always by a real timestamp.
        """
        stamps = self._last_used()
        candidates: list[tuple[str, float]] = []
        for key, worktree in _discover_worktrees(self._root):
            if key in exclude:
                continue
            stamp = stamps.get(key)
            if stamp is None:
                try:
                    stamp = os.path.getmtime(worktree)
                except OSError:
                    continue
            candidates.append((key, stamp))
        candidates.sort(key=lambda item: item[1])
        return candidates


def _discover_worktrees(root: str) -> t.Iterable[tuple[str, str]]:
    """Yield ``(lease_key, worktree_path)`` for every env directory under root.

    Walks exactly the layout ``{root}/{tenant}/{workspace}/{user}/envs/{env}``
    produced by the materializer; anything else on the volume is invisible and
    therefore never reclaimed.
    """
    root_path = os.path.expanduser(root)
    if not os.path.isdir(root_path):
        return
    for tenant in _listdir(root_path):
        for workspace in _listdir(os.path.join(root_path, tenant)):
            for user in _listdir(os.path.join(root_path, tenant, workspace)):
                envs = os.path.join(root_path, tenant, workspace, user, "envs")
                for env in _listdir(envs):
                    worktree = os.path.join(envs, env)
                    if not os.path.isdir(worktree):
                        continue
                    try:
                        yield (
                            build_lease_key(tenant, workspace, user, env),
                            worktree,
                        )
                    except ValueError:
                        # A directory that does not encode a valid identity
                        # (validation happens inside lease_key) is not ours.
                        logger.warning(
                            "disk_reclaim_skip_invalid_layout", path=worktree
                        )


def _listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _filesystem_usage(path: str) -> tuple[float, float]:
    """(used_bytes, total_bytes) of the filesystem holding ``path``.

    ``shutil.disk_usage`` stattes the filesystem, not the subtree — exactly
    what a watermark should watch: the volume the pod's writes and the
    reclaimer's deletes both land on.
    """
    usage = shutil.disk_usage(path)
    return float(usage.used), float(usage.total)
