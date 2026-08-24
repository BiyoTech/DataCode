"""Dagster Deployment lifecycle — one deployment per platform environment.

A **deployment** is the Dagster side of a platform environment: the code
location that loads that environment's branch of the code, plus the run
namespace its executions belong to. The binding is 1:1:1 with the Git branch and
the SQLMesh virtual environment, and the environment registry in sqlmesh-service
is its source of truth (design §5, §7).

What this module adds on top of :mod:`src.core.code_location_manager`

* **Identity.** A deployment is named ``{workspace_id}__{environment}`` through
  the shared naming algebra (:mod:`src.core.naming`), never derived from the
  working-copy path. The name is stable across services, restarts and machines.
* **Policy.** ``env_type`` and ``schedules_enabled`` decide whether the loaded
  code may have running schedules, and are injected into the code location
  process for the scaffold's gate to read (design §7.2). Non-production defaults
  to "no schedules": a branch deployment loads the very same code as production,
  so without the gate a feature branch would drive production data on every tick.
* **Attribution.** Run tags (``agenticdata/environment``, ``.../workspace_id``,
  ``.../deployment``) make every run answerable for which environment produced
  it — the open-source Dagster instance has no deployment isolation of its own,
  so the location namespace plus these tags is what separates environments
  (design §7.3).

State is process-local on purpose: a deployment is derived state that ``ensure``
rebuilds from the registry (or from the working copy) at any time. Persisting it
here would create a second source of truth to reconcile against.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from src.config import Settings, settings
from src.core import naming
from src.core.code_location_manager import (
    CodeLocationManager,
    CodeLocationSpec,
    get_code_location_manager,
)
from src.utils.errors import (
    DeploymentInvalidError,
    DeploymentNotFoundError,
    WorkspacePathInvalidError,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Run tag namespace. Prefixed keys keep platform tags apart from user tags in
# the Dagster UI and in run filters.
TAG_ENVIRONMENT = "agenticdata/environment"
TAG_WORKSPACE_ID = "agenticdata/workspace_id"
TAG_DEPLOYMENT = "agenticdata/deployment"

# Default Dagster repository name inside a code location loaded from a single
# ``definitions.py``.
DEFAULT_REPOSITORY = "__repository__"


@dataclass(frozen=True)
class DeploymentSpec:
    """The identity and policy of one deployment."""

    deployment_name: str
    workspace_path: str
    tenant_id: str
    workspace_id: str
    user_id: str
    environment: str
    env_type: str
    schedules_enabled: bool

    @property
    def is_production(self) -> bool:
        return self.env_type == naming.ENV_TYPE_PRODUCTION

    def code_location_env(self) -> dict[str, str]:
        """Deployment identity injected into the code location process.

        Read by the scaffolded ``defs/deployment.py`` (schedule gating) and
        available to any user asset that needs to know where it is running.
        """
        return {
            "AGENTICDATA_ENVIRONMENT": self.environment,
            "AGENTICDATA_DEPLOYMENT": self.deployment_name,
            "AGENTICDATA_DEPLOYMENT_TYPE": self.env_type,
            "AGENTICDATA_WORKSPACE_ID": self.workspace_id,
            "AGENTICDATA_TENANT_ID": self.tenant_id,
            "AGENTICDATA_SCHEDULES_ENABLED": "1" if self.schedules_enabled else "0",
        }

    def run_tags(self) -> dict[str, str]:
        """Tags attributing a run to this deployment."""
        return {
            TAG_ENVIRONMENT: self.environment,
            TAG_WORKSPACE_ID: self.workspace_id,
            TAG_DEPLOYMENT: self.deployment_name,
        }


@dataclass
class DeploymentInfo:
    """A deployment plus the observed state of its code location."""

    spec: DeploymentSpec
    host: str
    port: int
    definitions: str
    alive: bool

    def as_dict(self) -> dict:
        return {
            "deployment_name": self.spec.deployment_name,
            "location_name": self.spec.deployment_name,
            "workspace_path": self.spec.workspace_path,
            "tenant_id": self.spec.tenant_id,
            "workspace_id": self.spec.workspace_id,
            "environment": self.spec.environment,
            "env_type": self.spec.env_type,
            "schedules_enabled": self.spec.schedules_enabled,
            "host": self.host,
            "port": self.port,
            "definitions": self.definitions,
            "alive": self.alive,
        }


class DeploymentManager:
    """Creates, refreshes, inspects and tears down deployments."""

    def __init__(
        self,
        cfg: t.Optional[Settings] = None,
        code_locations: t.Optional[CodeLocationManager] = None,
    ) -> None:
        self._settings = cfg or settings
        self._code_locations = code_locations or get_code_location_manager()
        self._deployments: dict[str, DeploymentSpec] = {}

    # --- Identity ------------------------------------------------------------

    def resolve(
        self,
        workspace_path: str,
        *,
        environment: str = "",
        env_type: str = "",
        schedules_enabled: t.Optional[bool] = None,
    ) -> DeploymentSpec:
        """Derive a deployment spec from a working copy, with registry overrides.

        The working copy is authoritative for IDENTITY: its path encodes the
        tenant, workspace, user and environment, and the deployment name is a
        pure function of the last two. A caller may only CONFIRM the
        environment, never redirect it — accepting a different value would load
        one environment's code under another's name, silently mixing them.

        Policy (``env_type``, ``schedules_enabled``) is the registry's to state,
        because it lives in the registry row. When absent, the safe default is
        applied: production is recognized by its environment name and everything
        else is treated as a sandbox with schedules stopped.

        Raises:
            WorkspacePathInvalidError: the path is not an environment working copy.
            DeploymentInvalidError: the caller's environment/env_type contradicts it.
        """
        try:
            working_copy = naming.parse_working_copy(
                self._settings.workspace_base_path, workspace_path
            )
        except naming.WorkingCopyLayoutError as exc:
            raise WorkspacePathInvalidError(workspace_path, reason=str(exc)) from exc

        if environment and environment != working_copy.environment:
            raise DeploymentInvalidError(
                "Requested environment does not match the working copy",
                detail=(
                    f"working copy is checked out to {working_copy.environment!r}, "
                    f"but the request asked for {environment!r}; open the "
                    f"environment first so its own working copy is used"
                ),
            )

        resolved_type = env_type or working_copy.default_env_type
        if not naming.is_valid_env_type(resolved_type):
            raise DeploymentInvalidError(
                f"Unknown deployment type: {resolved_type}",
                detail=f"expected one of {', '.join(naming.ENV_TYPES)}",
            )
        if (
            resolved_type == naming.ENV_TYPE_PRODUCTION
            and working_copy.environment != naming.PRODUCTION_ENV
        ):
            # Typing a branch deployment as production would hand it running
            # schedules through the gate's own rule. Enabling schedules for a
            # shared environment is what ``schedules_enabled`` is for.
            raise DeploymentInvalidError(
                "Only the production environment may be a production deployment",
                detail=(
                    f"environment {working_copy.environment!r} cannot be typed "
                    f"{naming.ENV_TYPE_PRODUCTION!r}; set schedules_enabled "
                    f"instead if this environment must be scheduled"
                ),
            )

        if schedules_enabled is None:
            schedules_enabled = resolved_type == naming.ENV_TYPE_PRODUCTION

        return DeploymentSpec(
            deployment_name=working_copy.deployment_name,
            workspace_path=working_copy.workspace_path,
            tenant_id=working_copy.tenant_id,
            workspace_id=working_copy.workspace_id,
            user_id=working_copy.user_id,
            environment=working_copy.environment,
            env_type=resolved_type,
            schedules_enabled=bool(schedules_enabled),
        )

    def name_for(self, workspace_path: str) -> str:
        """Deployment (and code location) name of a working copy. Side-effect free."""
        return self.resolve(workspace_path).deployment_name

    # --- Lifecycle -----------------------------------------------------------

    async def ensure(self, spec: DeploymentSpec) -> DeploymentInfo:
        """Idempotently make a deployment ready to serve.

        Safe to call on every page load: an already-running code location is not
        respawned, only its spec is refreshed.
        """
        self._deployments[spec.deployment_name] = spec
        runtime = await self._code_locations.ensure(self._location_spec(spec))
        logger.info(
            "deployment_ensured",
            deployment=spec.deployment_name,
            environment=spec.environment,
            env_type=spec.env_type,
            schedules_enabled=spec.schedules_enabled,
            port=runtime.port,
            alive=runtime.alive,
        )
        return DeploymentInfo(
            spec=spec,
            host=runtime.host,
            port=runtime.port,
            definitions=runtime.definitions,
            alive=runtime.alive,
        )

    async def reload(self, spec: DeploymentSpec) -> DeploymentInfo:
        """Re-import a deployment's code after the working copy changed.

        This is the explicit refresh that follows an apply, a promote or a user
        edit. Nothing watches the filesystem on purpose: a code change becomes
        live through an audited action, never because a file was saved.
        """
        self._deployments[spec.deployment_name] = spec
        runtime = await self._code_locations.reload(self._location_spec(spec))
        logger.info(
            "deployment_reloaded",
            deployment=spec.deployment_name,
            environment=spec.environment,
            port=runtime.port,
            alive=runtime.alive,
        )
        return DeploymentInfo(
            spec=spec,
            host=runtime.host,
            port=runtime.port,
            definitions=runtime.definitions,
            alive=runtime.alive,
        )

    async def destroy(self, deployment_name: str) -> bool:
        """Stop a deployment and forget it.

        Called by the environment destroy orchestration. Returns whether a code
        location was actually running, so the caller can report honestly instead
        of claiming a cleanup it did not perform.
        """
        spec = self._deployments.pop(deployment_name, None)
        stopped = self._code_locations.remove(deployment_name)
        logger.info(
            "deployment_destroyed",
            deployment=deployment_name,
            environment=spec.environment if spec else "",
            code_location_stopped=stopped,
        )
        return stopped

    # --- Inspection ----------------------------------------------------------

    def list(self) -> list[DeploymentInfo]:
        """Every deployment this service currently serves (for reconciliation)."""
        return [self._info(spec) for spec in self._deployments.values()]

    def get(self, deployment_name: str) -> t.Optional[DeploymentInfo]:
        spec = self._deployments.get(deployment_name)
        return None if spec is None else self._info(spec)

    def require(self, deployment_name: str) -> DeploymentSpec:
        """Return a known deployment's spec, or raise 404."""
        spec = self._deployments.get(deployment_name)
        if spec is None:
            raise DeploymentNotFoundError(deployment_name)
        return spec

    async def diff(
        self,
        *,
        deployment_name: str,
        base_deployment_name: str,
        repository_name: str = DEFAULT_REPOSITORY,
    ) -> dict:
        """Compare two deployments' asset and schedule definitions.

        This is the ``assets`` segment of the environment diff (design §7.4): what
        the branch's code would change in the orchestration graph, independent of
        what SQLMesh would change in the data. Both deployments must be loaded —
        a definition set can only be read from a running code location, and
        guessing an empty one would report every asset as removed.
        """
        self.require(deployment_name)
        self.require(base_deployment_name)

        head = await self._definition_set(deployment_name, repository_name)
        base = await self._definition_set(base_deployment_name, repository_name)

        added = sorted(head.assets.keys() - base.assets.keys())
        removed = sorted(base.assets.keys() - head.assets.keys())
        changed = sorted(
            key
            for key in head.assets.keys() & base.assets.keys()
            if head.assets[key] != base.assets[key]
        )

        schedule_changes: list[dict] = []
        for name in sorted(head.schedules.keys() | base.schedules.keys()):
            current, previous = head.schedules.get(name), base.schedules.get(name)
            if current == previous:
                continue
            schedule_changes.append(
                {
                    "name": name,
                    "change": (
                        "added" if previous is None
                        else "removed" if current is None
                        else "modified"
                    ),
                    "cron": (current or {}).get("cron", ""),
                    "base_cron": (previous or {}).get("cron", ""),
                    "job": (current or previous or {}).get("job", ""),
                }
            )

        logger.info(
            "deployment_diff_computed",
            deployment=deployment_name,
            base=base_deployment_name,
            added=len(added),
            changed=len(changed),
            removed=len(removed),
            schedule_changes=len(schedule_changes),
        )
        return {
            "deployment": deployment_name,
            "base": base_deployment_name,
            "assets": {"added": added, "changed": changed, "removed": removed},
            "schedule_changes": schedule_changes,
        }

    # --- Internals -----------------------------------------------------------

    def _location_spec(self, spec: DeploymentSpec) -> CodeLocationSpec:
        return CodeLocationSpec(
            location_name=spec.deployment_name,
            workspace_path=spec.workspace_path,
            # Scaffolded files document themselves with the workspace they serve.
            workspace_name=spec.workspace_id,
            env=spec.code_location_env(),
        )

    def _info(self, spec: DeploymentSpec) -> DeploymentInfo:
        """Join a deployment's identity with the observed code location state.

        A missing runtime means the location is not (or no longer) running: the
        deployment is still reported, with ``alive=False``, so a reconciler sees
        the difference between "unknown" and "down".
        """
        runtime = self._code_locations.get(spec.deployment_name)
        return DeploymentInfo(
            spec=spec,
            host=runtime.host if runtime else self._settings.code_location_host,
            port=(
                runtime.port
                if runtime
                else (self._code_locations.port_of(spec.deployment_name) or 0)
            ),
            definitions=runtime.definitions if runtime else "",
            alive=bool(runtime and runtime.alive),
        )

    async def _definition_set(
        self, deployment_name: str, repository_name: str
    ) -> "_DefinitionSet":
        """Read one deployment's asset and schedule definitions over GraphQL."""
        from src.core.dagster_client import get_dagster_client

        client = get_dagster_client()
        nodes = await client.list_assets(deployment_name, repository_name)
        schedules = await client.list_schedules(deployment_name, repository_name)

        assets = {
            "__".join(node.get("assetKey", {}).get("path", [])): {
                "group": node.get("groupName") or "",
                "deps": sorted(
                    "__".join(dep.get("path", []))
                    for dep in node.get("dependencyKeys", [])
                ),
            }
            for node in nodes
        }
        return _DefinitionSet(
            assets=assets,
            schedules={
                str(s.get("name")): {
                    "cron": s.get("cronSchedule") or "",
                    "job": s.get("pipelineName") or "",
                }
                for s in schedules
            },
        )


@dataclass(frozen=True)
class _DefinitionSet:
    """Comparable snapshot of a code location's definitions."""

    assets: dict[str, dict]
    schedules: dict[str, dict]


_deployment_manager: t.Optional[DeploymentManager] = None


def get_deployment_manager() -> DeploymentManager:
    """Return the process-wide deployment manager singleton."""
    global _deployment_manager
    if _deployment_manager is None:
        _deployment_manager = DeploymentManager()
    return _deployment_manager
