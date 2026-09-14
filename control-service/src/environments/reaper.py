"""Sandbox reclamation: the TTL side of the environment lifecycle.

Why environments must expire
----------------------------
Every environment costs a Git branch, a set of warehouse schemas, a slice of
the shared SQLMesh state and a Dagster deployment. A personal sandbox is created
freely and abandoned just as freely, so without reclamation the platform's
resource usage grows monotonically and the per-workspace quota eventually
blocks the members who are still working (design §6.5, §13).

Expiry is announced, not silently executed
------------------------------------------
Reaching ``expires_at`` does NOT destroy anything. It records an ``expire``
warning event and lets the environment live through a grace period, because the
data that disappears with it (and any work committed only to its branch) may
still matter to its owner. Reclamation happens only after the grace period, and
it is forced: waiting for a human to resolve uncommitted changes would mean
never reclaiming exactly the environments that were abandoned. The warning event
is what makes that outcome fair, and the audit trail records what was released.

Idempotency
-----------
The scan derives everything from the registry and the audit trail, so a missed
tick, a restart or an overlapping run changes nothing: the warning is recorded at
most once per expiry, and reclamation is the registry's own idempotent
``destroy``. A busy environment (an apply holding the environment lock) is simply
left for the next tick.

All time arithmetic is UTC.
"""

from __future__ import annotations

import asyncio
import typing as t
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from datacode_common import naming

from src.config import settings
from src.environments.repository import EnvironmentRepository
from src.utils.errors import EnvironmentBusyError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Actor recorded for automated reclamation. Path-segment safe, so it can flow
# through the same code paths as a real user id without special-casing.
SYSTEM_ACTOR = "system_reaper"

# Audit trigger type for scheduled (non-human, non-agent) activity.
TRIGGER_SCHEDULE = "schedule"


@dataclass(frozen=True)
class ReapOutcome:
    """Result of one scan, for logging, tests and the manual-trigger endpoint."""

    scanned: int
    warned: int
    reclaimed: int
    skipped_busy: int
    failed: int


def _default_registry() -> t.Any:
    """Resolve the environment registry lazily.

    Imported on use, not at module import: the registry pulls in the whole
    binder stack, and a scheduled job must not widen the import graph of the
    process that merely schedules it.
    """
    from src.environments.registry import get_environment_registry

    return get_environment_registry()


class EnvironmentReaper:
    """Announces and then reclaims expired sandbox environments.

    Collaborators are injected so the reclamation POLICY (when to warn, when to
    destroy, what to skip) can be verified without a runtime, a Git remote or
    a warehouse — this class decides, the registry executes.
    """

    def __init__(
        self,
        *,
        repository: t.Optional[EnvironmentRepository] = None,
        registry_factory: t.Optional[t.Callable[[], t.Any]] = None,
    ) -> None:
        self._repo = repository or EnvironmentRepository()
        self._registry_factory = registry_factory or _default_registry

    async def run_once(self, now: t.Optional[datetime] = None) -> ReapOutcome:
        """Scan expired sandboxes once: warn, then reclaim past the grace period."""
        moment = now or datetime.now(timezone.utc)
        grace = timedelta(hours=max(0, settings.sandbox_expiry_grace_hours))

        expired = await self._repo.list_expired(moment)
        warned = reclaimed = skipped_busy = failed = 0

        for model in expired:
            expires_at = _as_utc(model.expires_at)
            if expires_at is None:
                # Defensive: the query filters on this column, so a missing value
                # means the row changed under us. Leave it to the next tick.
                continue

            try:
                if moment < expires_at + grace:
                    if await self._announce_expiry(model, expires_at, moment):
                        warned += 1
                    continue
                if await self._reclaim(model, expires_at):
                    reclaimed += 1
            except EnvironmentBusyError:
                # An apply or promote holds the environment: reclaiming it now
                # would interrupt work in progress. The next tick retries.
                skipped_busy += 1
                logger.info(
                    "environment_reap_skipped_busy",
                    environment_id=model.id,
                    workspace_id=model.workspace_id,
                )
            except Exception as exc:  # noqa: BLE001 - one row must not stop the scan
                failed += 1
                logger.error(
                    "environment_reap_failed",
                    environment_id=model.id,
                    workspace_id=model.workspace_id,
                    error=str(exc),
                )

        outcome = ReapOutcome(
            scanned=len(expired),
            warned=warned,
            reclaimed=reclaimed,
            skipped_busy=skipped_busy,
            failed=failed,
        )
        if expired:
            logger.info(
                "environment_reap_completed",
                scanned=outcome.scanned,
                warned=outcome.warned,
                reclaimed=outcome.reclaimed,
                skipped_busy=outcome.skipped_busy,
                failed=outcome.failed,
            )
        return outcome

    async def start_periodic(self, interval_seconds: int) -> None:
        """Run :meth:`run_once` forever, one scan per interval.

        Never propagates: a scheduled job that dies on a transient database
        error would silently stop reclaiming for the rest of the process's life.
        """
        interval = max(60, interval_seconds)
        logger.info("environment_reaper_started", interval_seconds=interval)
        while True:
            try:
                await asyncio.sleep(interval)
                await self.run_once()
            except asyncio.CancelledError:
                logger.info("environment_reaper_stopped")
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.error("environment_reaper_cycle_failed", error=str(exc))

    # ---- Internals ----

    async def _announce_expiry(
        self, model: t.Any, expires_at: datetime, now: datetime
    ) -> bool:
        """Record the one-off warning that precedes reclamation.

        Returns True when a warning was written. Recorded at most once per
        expiry: the audit trail is queried for a warning newer than
        ``expires_at``, so an extended TTL (a later ``expires_at``) correctly
        announces itself again.
        """
        already = await self._repo.count_events(
            model.id, naming.ACTION_EXPIRE, since=expires_at
        )
        if already:
            return False

        reclaim_at = expires_at + timedelta(
            hours=max(0, settings.sandbox_expiry_grace_hours)
        )
        await self._repo.record_event(
            model.id,
            naming.ACTION_EXPIRE,
            actor_user_id=SYSTEM_ACTOR,
            trigger_type=TRIGGER_SCHEDULE,
            succeeded=True,
            detail={
                "stage": "warned",
                "expires_at": expires_at.isoformat(),
                "reclaim_at": reclaim_at.isoformat(),
                "grace_hours": settings.sandbox_expiry_grace_hours,
            },
        )
        logger.info(
            "environment_expiry_announced",
            environment_id=model.id,
            workspace_id=model.workspace_id,
            owner_user_id=model.owner_user_id,
            reclaim_at=reclaim_at.isoformat(),
        )
        return True

    async def _reclaim(self, model: t.Any, expires_at: datetime) -> bool:
        """Destroy an environment whose grace period has elapsed.

        Forced on purpose: an abandoned sandbox is precisely the case where
        nobody will come to resolve its uncommitted changes, and the warning
        event recorded at expiry is the notice that made this fair. What is
        released is reported in the audit trail by ``destroy`` itself.

        The remote branch survives here exactly as it does for a user-initiated
        destroy: an expiry is a capacity decision about platform state, never a
        licence to delete the team's code.
        """
        result = await self._registry_factory().destroy(
            environment_id=model.id,
            tenant_id=model.tenant_id,
            actor_user_id=SYSTEM_ACTOR,
            force=True,
            trigger_type=TRIGGER_SCHEDULE,
        )
        await self._repo.record_event(
            model.id,
            naming.ACTION_EXPIRE,
            actor_user_id=SYSTEM_ACTOR,
            trigger_type=TRIGGER_SCHEDULE,
            succeeded=result.archived,
            detail={
                "stage": "reclaimed",
                "expires_at": expires_at.isoformat(),
                "ttl_hours": model.ttl_hours,
                "worktrees_removed": result.worktrees_removed,
            },
        )
        logger.info(
            "environment_reclaimed",
            environment_id=model.id,
            workspace_id=model.workspace_id,
            owner_user_id=model.owner_user_id,
            archived=result.archived,
        )
        return result.archived


def _as_utc(value: t.Optional[datetime]) -> t.Optional[datetime]:
    """Interpret a stored timestamp as UTC.

    SQLite hands back naive datetimes even for ``DateTime(timezone=True)``
    columns, and comparing those against an aware "now" raises. Everything the
    platform stores is UTC by contract, so a naive value is labelled as such.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


_reaper: t.Optional[EnvironmentReaper] = None


def get_environment_reaper() -> EnvironmentReaper:
    """Get or create the global :class:`EnvironmentReaper` (stateless)."""
    global _reaper
    if _reaper is None:
        _reaper = EnvironmentReaper()
    return _reaper
