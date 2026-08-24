"""Dagster deployment binder — the orchestration side of an environment's lifecycle.

The registry owns WHEN an environment exists; this binder owns what that means
inside dagster-service:

* **ensure** — bring the environment's deployment up (a code location process
  loading the asset-dag code of one working copy). Idempotent and safe to call
  repeatedly: an already-running location is not respawned, only its POLICY
  (``env_type`` / ``schedules_enabled``) is refreshed.
* **destroy** — stop the deployment and forget it. Needs nothing but the name:
  a deployment is derived state, rebuildable from any working copy, so tearing
  it down never depends on one being present.
* **list** — read what the service actually serves. The deployment registry is
  IN-PROCESS state on the dagster-service side, so it is empty after every
  service restart — exactly the drift ``missing_deployment`` repairs (design
  §11.1).

All calls are direct service-to-service HTTP (the ``services.dagster`` entry of
conf/platform.yaml), not through the gateway: like the dagster-side
SQLMeshBridge, this is a trusted internal hop with no user context to
authorize.

Failures are reported, never raised: an unreachable dagster-service while an
environment is created or destroyed is drift the reconciler repairs, not a
reason to fail the orchestration that owns the registry row. The one nuance is
:meth:`list_deployments`, whose ``None`` return keeps "cannot read" distinct
from "nothing exists" — the same honesty rule as
:meth:`src.environments.sqlmesh_binder.SqlMeshBinder.list_state_environments`.
"""

from __future__ import annotations

import typing as t
from urllib.parse import quote

import httpx

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Dagster-service REST endpoints (Phase 4 contract).
_DEPLOYMENTS_PATH = "/api/v1/dagster/deployments"
_ENSURE_PATH = f"{_DEPLOYMENTS_PATH}/ensure"


class DeploymentBinder:
    """Ensures, lists and destroys the Dagster deployments of environments."""

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: float = 60.0,
        transport: t.Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        # The timeout is generous because ``ensure`` may spawn a code location
        # subprocess and import the user's asset-dag code before answering.
        from src.config import settings

        self._base_url = (base_url or settings.dagster_service_url).rstrip("/")
        self._timeout = timeout
        # Injected by tests; ``None`` means real network.
        self._transport = transport

    async def list_deployments(self) -> t.Optional[t.List[t.Dict[str, t.Any]]]:
        """Deployments the service currently serves, or ``None`` when unreachable.

        ``None`` is NOT an empty list: the reconciler must withhold every
        deployment verdict during an outage instead of treating it as "no
        deployment exists", which would rebuild or destroy the whole
        workspace's orchestration on a routine service restart window.
        """
        try:
            async with self._client() as client:
                resp = await client.get(f"{self._base_url}{_DEPLOYMENTS_PATH}")
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - unreachable, not empty
            logger.warning(
                "deployments_read_failed",
                base_url=self._base_url,
                error=str(exc),
            )
            return None

        if not isinstance(payload, list):
            # A 200 with a malformed body says nothing about what is served;
            # withholding the verdict is the only safe reading.
            logger.warning(
                "deployments_read_malformed",
                base_url=self._base_url,
                payload_type=type(payload).__name__,
            )
            return None
        return payload

    async def ensure_deployment(
        self,
        *,
        workspace_path: str,
        environment: str,
        env_type: str,
        schedules_enabled: bool,
    ) -> bool:
        """Idempotently make the environment's deployment ready to serve.

        The working copy is authoritative for IDENTITY; ``environment`` only
        confirms it, while ``env_type`` / ``schedules_enabled`` carry the
        registry's POLICY, which no path can express. Returns False when the
        service could not be reached or refused — the caller records drift,
        never an exception.
        """
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{self._base_url}{_ENSURE_PATH}",
                    json={
                        "workspace_path": workspace_path,
                        "environment": environment,
                        "env_type": env_type,
                        "schedules_enabled": schedules_enabled,
                    },
                )
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - reported as drift, never fatal
            logger.warning(
                "deployment_ensure_failed",
                workspace_path=workspace_path,
                environment=environment,
                error=str(exc),
            )
            return False

        logger.info(
            "deployment_ensured_by_registry",
            workspace_path=workspace_path,
            environment=environment,
            env_type=env_type,
            schedules_enabled=schedules_enabled,
        )
        return True

    async def destroy_deployment(self, *, deployment_name: str) -> bool:
        """Stop a deployment and forget it; needs only its name.

        Returns False when unreachable, refused, or the name is empty (a row
        frozen before deployments existed), so the destroy orchestration
        reports an honest partial outcome the reconciler finishes later.
        """
        if not deployment_name:
            return False
        path = (
            f"{self._base_url}{_DEPLOYMENTS_PATH}/"
            f"{quote(deployment_name, safe='')}"
        )
        try:
            async with self._client() as client:
                resp = await client.delete(path)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - best-effort release
            logger.warning(
                "deployment_destroy_failed",
                deployment_name=deployment_name,
                error=str(exc),
            )
            return False

        logger.info(
            "deployment_destroyed_by_registry",
            deployment_name=deployment_name,
        )
        return True

    def _client(self) -> httpx.AsyncClient:
        """Per-call client: reconcile is low-frequency, so no pool to manage."""
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)


_binder: t.Optional[DeploymentBinder] = None


def get_deployment_binder() -> DeploymentBinder:
    """Get or create the global :class:`DeploymentBinder` (stateless)."""
    global _binder
    if _binder is None:
        _binder = DeploymentBinder()
    return _binder
