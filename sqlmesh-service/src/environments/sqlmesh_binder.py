"""SQLMesh binder — the virtual-environment side of an environment's lifecycle.

The registry owns WHEN an environment exists; this binder owns what that means
inside SQLMesh:

* **create** — a virtual update: the new environment's views are pointed at the
  base environment's already-materialized snapshots (``create_from`` +
  ``skip_backfill``). No data is computed, so a new environment costs a schema of
  views and nothing else. This is also the only place in the platform allowed to
  pass ``create_from``: through the user-facing plan API an environment can no
  longer come into existence as a side effect (see
  :mod:`src.environments.guard`).
* **destroy** — invalidate the environment and let SQLMesh drop the expired
  views, so the state does not accumulate orphans nobody can promote from.

Both operations go through the shared :class:`~src.core.context_pool.ContextPool`
rather than building their own Context: the pool is what guarantees the
workspace-scoped ``state_schema`` is verified first and that concurrent access to
one working copy stays serialized.

Failures are reported, never raised: a warehouse that is unreachable while a
branch was created successfully is drift, and drift is a registry state (see
``STATUS_DRIFTED``) that the reconciler repairs — not a reason to lose the branch.
"""

from __future__ import annotations

import asyncio
import typing as t

from src.core.context_pool import get_context_pool
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SqlMeshBinder:
    """Creates and releases the SQLMesh virtual environment of an environment."""

    async def ensure_virtual_environment(
        self,
        *,
        workspace_path: str,
        environment: str,
        base_env: str,
        gateway: t.Optional[str] = None,
    ) -> bool:
        """Create ``environment`` as a virtual copy of ``base_env``.

        Idempotent: re-running against an existing environment produces a plan
        with nothing to do.

        Returns True when the virtual environment is ready, False when the engine
        could not be reached or the plan failed (the caller marks the environment
        as drifted).
        """
        try:
            pool = get_context_pool()
            entry = await pool.get_context(workspace_path, gateway)
            async with entry.write_lock:
                await pool.refresh_if_dirty(entry)
                plan = await asyncio.to_thread(
                    self._build_virtual_plan, entry.context, environment, base_env
                )
                await asyncio.to_thread(entry.context.apply, plan)
        except Exception as exc:  # noqa: BLE001 - reported as drift, never fatal
            logger.warning(
                "virtual_environment_create_failed",
                workspace_path=workspace_path,
                environment=environment,
                base_env=base_env,
                error=str(exc),
            )
            return False

        logger.info(
            "virtual_environment_created",
            workspace_path=workspace_path,
            environment=environment,
            base_env=base_env,
        )
        return True

    async def list_state_environments(
        self, *, workspace_path: str, gateway: t.Optional[str] = None
    ) -> t.Optional[t.Set[str]]:
        """Names of the environments that ACTUALLY exist in the SQLMesh state.

        Reads the state directly instead of going through
        :func:`~src.core.format.format_environments`, which synthesizes ``prod``
        and the default target when they are absent — useful for a UI, fatal for
        reconciliation, because a missing production environment would be
        reported as present.

        Returns ``None`` when the state could not be read at all. That is NOT the
        same as an empty set and the distinction is what keeps a reconciler
        honest: treating an unreachable warehouse as "no environments exist"
        would have it recreate or invalidate every environment of the workspace.
        """
        try:
            pool = get_context_pool()
            entry = await pool.get_context(workspace_path, gateway)
            environments = await asyncio.to_thread(
                lambda: [env.name for env in entry.context.state_reader.get_environments()]
            )
        except Exception as exc:  # noqa: BLE001 - unknown, not empty
            logger.warning(
                "state_environments_read_failed",
                workspace_path=workspace_path,
                error=str(exc),
            )
            return None

        return set(environments)

    async def invalidate_virtual_environment(
        self,
        *,
        workspace_path: str,
        environment: str,
        gateway: t.Optional[str] = None,
    ) -> bool:
        """Invalidate an environment and drop its expired views.

        ``invalidate_environment`` marks the state record for removal;
        ``delete_expired_environments`` is what actually releases the views, so
        both are needed for the schemas to disappear.
        """
        try:
            pool = get_context_pool()
            entry = await pool.get_context(workspace_path, gateway)
            async with entry.write_lock:
                state_sync = entry.context.state_sync
                await asyncio.to_thread(state_sync.invalidate_environment, environment)
                await asyncio.to_thread(state_sync.delete_expired_environments)
        except Exception as exc:  # noqa: BLE001 - resource release is best-effort
            logger.warning(
                "virtual_environment_invalidate_failed",
                workspace_path=workspace_path,
                environment=environment,
                error=str(exc),
            )
            return False

        # The cached context still holds the deleted environment in memory.
        await get_context_pool().invalidate_all_for_workspace(workspace_path)
        logger.info(
            "virtual_environment_invalidated",
            workspace_path=workspace_path,
            environment=environment,
        )
        return True

    @staticmethod
    def _build_virtual_plan(context: t.Any, environment: str, base_env: str):
        """Build the zero-cost plan that materializes a new environment.

        ``include_unmodified`` is required so the new environment gets a view for
        EVERY model of the base environment, not only for the ones that differ;
        ``skip_backfill`` keeps it a pure pointer switch.
        """
        builder = context.plan_builder(
            environment=environment,
            create_from=base_env,
            skip_backfill=True,
            skip_tests=True,
            include_unmodified=True,
            no_prompts=True,
        )
        return builder.build()


_binder: t.Optional[SqlMeshBinder] = None


def get_sqlmesh_binder() -> SqlMeshBinder:
    """Get or create the global :class:`SqlMeshBinder` (stateless)."""
    global _binder
    if _binder is None:
        _binder = SqlMeshBinder()
    return _binder
