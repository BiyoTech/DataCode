"""SQLMesh binder — the virtual-environment side of an environment's lifecycle.

The registry (control-service, P5) owns WHEN an environment exists; this binder
owns what that means inside SQLMesh:

* **create** — a virtual update: the new environment's views are pointed at the
  base environment's already-materialized snapshots (``create_from`` +
  ``skip_backfill``). No data is computed, so a new environment costs a schema of
  views and nothing else. This is also the only place in the platform allowed to
  pass ``create_from``: through the user-facing plan API an environment can no
  longer come into existence as a side effect (see
  :mod:`src.environments.guard`).
* **destroy** — invalidate the environment and let SQLMesh drop the expired
  views, so the state does not accumulate orphans nobody can promote from.

P5: runs inside workspace-runtime behind the
``/internal/env-control/sqlmesh/*`` API. control-service calls it over HTTP.

P6 close-out:
* **Reads** (state environment names) go through the executor sandbox like
  every other read — no Context is constructed in this process for them.
* **Writes** (create/invalidate a virtual environment) still construct an
  executable Context IN-PROCESS. This is a recorded residual exposure (see the
  roadmap's P6 deviation list): the plan build executes the user's models and
  the apply writes state, and splitting THAT across the Q5 boundary (render in
  L3 / write in L2) is a separate piece of work. It is confined to two
  low-frequency control-plane operations with an explicit lock, so the
  exposure is narrow and visible rather than spread across every read path.

Failures are reported, never raised: a warehouse that is unreachable while a
branch was created successfully is drift, and drift is a registry state (see
``STATUS_DRIFTED``) that the reconciler repairs — not a reason to lose the
branch.
"""

from __future__ import annotations

import asyncio
import typing as t

from src.utils.errors import ContextConfigError, ContextLoadError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Serialize the in-process write operations per working copy: two control-plane
# calls must not build conflicting virtual-environment plans over one project.
_write_locks: dict[str, asyncio.Lock] = {}


def _lock_for(workspace_path: str) -> asyncio.Lock:
    lock = _write_locks.get(workspace_path)
    if lock is None:
        lock = asyncio.Lock()
        _write_locks[workspace_path] = lock
    return lock


def _load_context(workspace_path: str, gateway: t.Optional[str]):
    """Construct the executable Context for a virtual-environment write.

    RECORDED EXPOSURE: this execs the user's Python models/macros in the
    platform process (threat T2). It exists because a virtual environment's
    plan must be built from the project's own models and its apply writes
    state; the Q5-clean split for that is future work. The ContextPool used to
    hide this behind a cache; keeping the pool alive for two low-frequency
    control-plane calls was worse than making the exposure explicit here.
    """
    from sqlmesh.core.context import Context
    from sqlmesh.utils.errors import ConfigError

    from src.files.layout import sqlmesh_root
    from src.sqlmesh.config_ops import reconcile_state_schema
    from src.workspace.identity import resolve_workspace_identity

    project_root = sqlmesh_root(workspace_path)
    identity = resolve_workspace_identity(workspace_path)
    # Same pre-load check every other Context load funnels through: a gateway
    # without the managed state_schema gets it written; a diverging one is a 409.
    reconcile_state_schema(project_root, identity.workspace_id)

    kwargs: dict = {"paths": project_root, "load": True}
    if gateway:
        kwargs["gateway"] = gateway
    try:
        return Context(**kwargs)
    except ConfigError as e:
        raise ContextConfigError(
            workspace_path,
            detail=str(e),
            workspace_id=identity.workspace_id,
            environment=identity.environment,
        ) from e
    except Exception as e:
        raise ContextLoadError(workspace_path, detail=str(e)) from e


def _invalidate_metadata(workspace_path: str) -> None:
    """Drop the sandbox's warm Context and the runtime's cached metadata.

    The state just changed underneath both — environments appear or vanish,
    so every derived view (model listings per environment, lineage, the
    environment list) must be rebuilt on next read.
    """
    from src.executor.process import get_executor_manager
    from src.sqlmesh.metadata_context import get_metadata_context

    get_executor_manager().invalidate(workspace_path)
    get_metadata_context().invalidate(workspace_path)


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
            async with _lock_for(workspace_path):
                context = await asyncio.to_thread(_load_context, workspace_path, gateway)
                try:
                    plan = await asyncio.to_thread(
                        self._build_virtual_plan, context, environment, base_env
                    )
                    await asyncio.to_thread(context.apply, plan)
                finally:
                    await asyncio.to_thread(_close_context, context)
        except Exception as exc:  # noqa: BLE001 - reported as drift, never fatal
            logger.warning(
                "virtual_environment_create_failed",
                workspace_path=workspace_path,
                environment=environment,
                base_env=base_env,
                error=str(exc),
            )
            return False

        _invalidate_metadata(workspace_path)
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

        Reads the state directly through the executor sandbox (raw names, no
        synthesis of ``prod`` / the default target — :func:`format_environments`
        synthesizes both, which is useful for a UI, fatal for reconciliation,
        because a missing production environment would be reported as present).

        Returns ``None`` when the state could not be read at all. That is NOT the
        same as an empty set and the distinction is what keeps a reconciler
        honest: treating an unreachable warehouse as "no environments exist"
        would have it recreate or invalidate every environment of the workspace.
        """
        from src.sqlmesh.metadata_context import get_metadata_context

        names = await get_metadata_context().raw_environment_names(
            workspace_path, gateway=gateway
        )
        if names is None:
            return None
        return set(names)

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
            async with _lock_for(workspace_path):
                context = await asyncio.to_thread(_load_context, workspace_path, gateway)
                try:
                    state_sync = context.state_sync
                    await asyncio.to_thread(state_sync.invalidate_environment, environment)
                    await asyncio.to_thread(state_sync.delete_expired_environments)
                finally:
                    await asyncio.to_thread(_close_context, context)
        except Exception as exc:  # noqa: BLE001 - resource release is best-effort
            logger.warning(
                "virtual_environment_invalidate_failed",
                workspace_path=workspace_path,
                environment=environment,
                error=str(exc),
            )
            return False

        _invalidate_metadata(workspace_path)
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


def _close_context(context) -> None:
    """Close the engine adapter of a binder-built Context (best-effort)."""
    try:
        adapter = getattr(context, "engine_adapter", None)
        if adapter is not None:
            adapter.close()
    except Exception:  # noqa: BLE001 - teardown must not raise
        pass


_binder: t.Optional[SqlMeshBinder] = None


def get_sqlmesh_binder() -> SqlMeshBinder:
    """Get or create the global :class:`SqlMeshBinder` (stateless)."""
    global _binder
    if _binder is None:
        _binder = SqlMeshBinder()
    return _binder
