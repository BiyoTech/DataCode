"""Dagster deployment binder — the orchestration side of an environment's lifecycle.

The registry owns WHEN an environment exists; this binder owns what that means
inside the Dagster data plane (hosted by workspace-runtime):

* **ensure** — bring the environment's deployment up (a code location process
  loading the asset-dag code of one working copy). Idempotent and safe to call
  repeatedly: an already-running location is not respawned, only its POLICY
  (``env_type`` / ``schedules_enabled``) is refreshed.
* **destroy** — stop the deployment and forget it. Needs nothing but the name:
  a deployment is derived state, rebuildable from any working copy, so tearing
  it down never depends on one being present.
* **list** — read what the service actually serves. The deployment registry is
  IN-PROCESS state on the runtime side, so it is empty after every service
  restart — exactly the drift ``missing_deployment`` repairs (design §11.1).

Addressing
----------
A deployment lives in the process of the replica that spawned its code
location, so with several replicas each one serves a different slice:

* ``ensure`` goes to the replica holding the working copy (it loads that copy's
  code from local disk);
* ``list`` is the union over every live replica, and is withheld entirely
  (``None``) when any replica could not be read — a partial union would show a
  served deployment as missing;
* ``destroy`` is sent to every live replica, because the caller cannot know
  which one serves the name.

All of it goes through :class:`RuntimeClient`, which owns the delivery table
(:mod:`src.runtime.routing`).

Failures are reported, never raised: an unreachable runtime while an
environment is created or destroyed is drift the reconciler repairs, not a
reason to fail the orchestration that owns the registry row.
"""

from __future__ import annotations

import typing as t
from urllib.parse import quote

from src.environments.git_binder import _ids_from_path
from src.runtime.client import RuntimeClient, RuntimeTarget, get_runtime_client
from src.runtime.routing import DAGSTER_DEPLOYMENTS_PATH, DAGSTER_ENSURE_PATH
from src.utils.logging import get_logger

logger = get_logger(__name__)


class DeploymentBinder:
    """Ensures, lists and destroys the Dagster deployments of environments."""

    def __init__(self, *, client: t.Optional[RuntimeClient] = None) -> None:
        self._client = client or get_runtime_client()

    async def list_deployments(self) -> t.Optional[t.List[t.Dict[str, t.Any]]]:
        """Deployments served across the fleet, or ``None`` when not fully readable.

        ``None`` is NOT an empty list: the reconciler must withhold every
        deployment verdict during an outage instead of treating it as "no
        deployment exists", which would rebuild or destroy the whole
        workspace's orchestration on a routine restart window.
        """
        try:
            replies = await self._client.get_all(DAGSTER_DEPLOYMENTS_PATH)
        except Exception as exc:  # noqa: BLE001 - unreachable, not empty
            logger.warning("deployments_read_failed", error=str(exc))
            return None

        merged: dict[str, t.Dict[str, t.Any]] = {}
        for reply in replies:
            if not reply.ok or not isinstance(reply.body, list):
                logger.warning(
                    "deployments_read_incomplete",
                    target=reply.addr,
                    error=str(reply.error) if reply.error else "malformed body",
                )
                return None
            for info in reply.body:
                if isinstance(info, dict):
                    merged.setdefault(str(info.get("deployment_name", "")), info)
        return list(merged.values())

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
            tenant, workspace, user, env = _ids_from_path(workspace_path)
            await self._client.post(
                DAGSTER_ENSURE_PATH,
                {
                    "workspace_path": workspace_path,
                    "environment": environment,
                    "env_type": env_type,
                    "schedules_enabled": schedules_enabled,
                },
                target=RuntimeTarget(
                    tenant_id=tenant,
                    workspace_id=workspace,
                    user_id=user,
                    environment=env,
                ),
            )
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
        """Stop a deployment on every replica; needs only its name.

        Returns False when the name is empty (a row frozen before deployments
        existed) or when any replica could not be reached, so the destroy
        orchestration reports an honest partial outcome the reconciler
        finishes later.
        """
        if not deployment_name:
            return False
        path = f"{DAGSTER_DEPLOYMENTS_PATH}/{quote(deployment_name, safe='')}"
        try:
            replies = await self._client.delete_all(path)
        except Exception as exc:  # noqa: BLE001 - best-effort release
            logger.warning(
                "deployment_destroy_failed",
                deployment_name=deployment_name,
                error=str(exc),
            )
            return False

        failed = [reply.addr for reply in replies if not reply.ok]
        if failed:
            logger.warning(
                "deployment_destroy_incomplete",
                deployment_name=deployment_name,
                unreachable_replicas=failed,
            )
            return False

        logger.info(
            "deployment_destroyed_by_registry",
            deployment_name=deployment_name,
            replicas=len(replies),
        )
        return True


_binder: t.Optional[DeploymentBinder] = None


def get_deployment_binder() -> DeploymentBinder:
    """Get or create the global :class:`DeploymentBinder`."""
    global _binder
    if _binder is None:
        _binder = DeploymentBinder()
    return _binder
