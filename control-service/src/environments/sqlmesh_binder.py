"""SQLMesh binder — the HTTP client for virtual environments.

The registry (this service) owns WHEN an environment exists; what that means
inside the SQLMesh engine is executed by workspace-runtime behind the
``/internal/env-control/sqlmesh/*`` API:

* **create** — a virtual update: the new environment's views are pointed at the
  base environment's already-materialized snapshots (``create_from`` +
  ``skip_backfill``). No data is computed, so a new environment costs a schema
  of views and nothing else.
* **destroy** — invalidate the environment and let SQLMesh drop the expired
  views, so the state does not accumulate orphans nobody can promote from.

Failures are reported, never raised: a warehouse that is unreachable while a
branch was created successfully is drift, and drift is a registry state (see
``STATUS_DRIFTED``) that the reconciler repairs — not a reason to lose the
branch.

Addressing
----------
All three endpoints reach the engine THROUGH a working copy that the runtime
locates by scanning its own disk, so they are addressed at the WORKSPACE: any
replica holding a copy of it can answer, and a replica holding none answers
404 no matter how healthy it is (see :mod:`src.runtime.routing`).
"""

from __future__ import annotations

import typing as t

from src.environments.git_binder import _ids_from_path
from src.runtime.client import RuntimeClient, RuntimeTarget, get_runtime_client
from src.utils.errors import RuntimeUnavailableError
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SqlMeshBinder:
    """Creates and releases the SQLMesh virtual environment of an environment."""

    def __init__(self, *, client: RuntimeClient | None = None) -> None:
        self._client = client or get_runtime_client()

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
        tenant, workspace, _, _ = _ids_from_path(workspace_path)
        try:
            body = await self._client.post(
                "/internal/env-control/sqlmesh/ensure-virtual-environment",
                {
                    "tenant_id": tenant,
                    "workspace_id": workspace,
                    "environment": environment,
                    "base_env": base_env,
                    "gateway": gateway or "",
                },
                target=RuntimeTarget(tenant_id=tenant, workspace_id=workspace),
            )
        except Exception as exc:  # noqa: BLE001 - reported as drift, never fatal
            logger.warning(
                "virtual_environment_create_failed",
                workspace_path=workspace_path,
                environment=environment,
                base_env=base_env,
                error=str(exc),
            )
            return False

        ok = bool(body.get("ok", False))
        if ok:
            logger.info(
                "virtual_environment_created",
                workspace_path=workspace_path,
                environment=environment,
                base_env=base_env,
            )
        return ok

    async def list_state_environments(
        self, *, workspace_path: str, gateway: t.Optional[str] = None
    ) -> t.Optional[t.Set[str]]:
        """Names of the environments that ACTUALLY exist in the SQLMesh state.

        Returns ``None`` when the state could not be read at all. That is NOT the
        same as an empty set and the distinction is what keeps a reconciler
        honest: treating an unreachable warehouse as "no environments exist"
        would have it recreate or invalidate every environment of the workspace.
        """
        tenant, workspace, _, env = _ids_from_path(workspace_path)
        try:
            replies = await self._client.get_all(
                "/internal/env-control/sqlmesh/state-environments",
                params={
                    "tenant_id": tenant,
                    "workspace_id": workspace,
                    # Any materialized environment of the workspace reaches the
                    # same shared state; prefer the caller's own.
                    "environment": env,
                    "gateway": gateway or "",
                },
                target=RuntimeTarget(tenant_id=tenant, workspace_id=workspace),
            )
        except Exception as exc:  # noqa: BLE001 - unknown, not empty
            logger.warning(
                "state_environments_read_failed",
                workspace_path=workspace_path,
                error=str(exc),
            )
            return None

        # Every replica reads the SAME shared state, so the first replica that
        # actually reached it is authoritative. A replica with no working copy
        # answers ``environments: null`` — no verdict, not an empty state.
        for reply in replies:
            if reply.body is not None and reply.body.get("environments") is not None:
                return set(reply.body["environments"])
        return None

    async def invalidate_virtual_environment(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        environment: str,
        gateway: t.Optional[str] = None,
    ) -> bool:
        """Invalidate an environment and drop its expired views.

        Addressed by tenant and workspace rather than by a working-copy path:
        the runtime resolves a usable copy itself, so the path was only ever a
        carrier for these two ids. Synthesizing one (with a placeholder user
        segment) used to be how this was called — and it raised on the layout
        validator, which meant a destroy blew up right after writing
        ``archiving`` and left the environment stuck in it.

        When no copy is materialized anywhere the runtime answers 404, which
        maps to ``False`` — the release is left to the reconciler rather than
        silently reported as done.
        """
        try:
            body = await self._client.post(
                "/internal/env-control/sqlmesh/invalidate-virtual-environment",
                {
                    "tenant_id": tenant_id,
                    "workspace_id": workspace_id,
                    "environment": environment,
                    "gateway": gateway or "",
                },
                target=RuntimeTarget(
                    tenant_id=tenant_id, workspace_id=workspace_id
                ),
            )
        except RuntimeUnavailableError as exc:
            logger.warning(
                "virtual_environment_invalidate_failed",
                workspace_id=workspace_id,
                environment=environment,
                error=str(exc),
            )
            return False
        except Exception:  # noqa: BLE001 - no working copy -> False, never fatal
            return False

        ok = bool(body.get("ok", False))
        if ok:
            logger.info(
                "virtual_environment_invalidated",
                workspace_id=workspace_id,
                environment=environment,
            )
        return ok


_binder: t.Optional[SqlMeshBinder] = None


def get_sqlmesh_binder() -> SqlMeshBinder:
    """Get or create the global :class:`SqlMeshBinder` (stateless)."""
    global _binder
    if _binder is None:
        _binder = SqlMeshBinder()
    return _binder
