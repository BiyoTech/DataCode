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
* **Attribution.** Run tags (``datacode/environment``, ``.../workspace_id``,
  ``.../deployment``) make every run answerable for which environment produced
  it — the open-source Dagster instance has no deployment isolation of its own,
  so the location namespace plus these tags is what separates environments
  (design §7.3).

State is process-local on purpose: a deployment is derived state that ``ensure``
rebuilds from the registry (or from the working copy) at any time. Persisting it
here would create a second source of truth to reconcile against.
"""

from __future__ import annotations

import asyncio
import typing as t
from dataclasses import dataclass

from datacode_common import naming

from src.codeloc.errors import (
    DeploymentInvalidError,
    DeploymentNotFoundError,
    ErrorCode,
    WorkspacePathInvalidError,
)
from src.codeloc.manager import (
    CodeLocationManager,
    CodeLocationSpec,
    get_code_location_manager,
)
from src.codeloc.reload_jobs import (
    FAILURE_CODE_LOAD,
    FAILURE_DEPS_INSTALL,
    FAILURE_INTERNAL,
    STAGE_INSTALLING_DEPS,
    STAGE_LOADING_DEFINITIONS,
    STAGE_RESOLVING_DEPS,
    STAGE_STARTING_LOCATION,
    ReloadJob,
    get_reload_job_store,
)
from src.config import Settings, settings
from src.deps.service import archive_failure, get_deps_service
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Run tag namespace. Prefixed keys keep platform tags apart from user tags in
# the Dagster UI and in run filters.
TAG_ENVIRONMENT = "datacode/environment"
TAG_WORKSPACE_ID = "datacode/workspace_id"
TAG_DEPLOYMENT = "datacode/deployment"

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
            "DATACODE_ENVIRONMENT": self.environment,
            "DATACODE_DEPLOYMENT": self.deployment_name,
            "DATACODE_DEPLOYMENT_TYPE": self.env_type,
            "DATACODE_WORKSPACE_ID": self.workspace_id,
            "DATACODE_TENANT_ID": self.tenant_id,
            "DATACODE_SCHEDULES_ENABLED": "1" if self.schedules_enabled else "0",
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
    # Read-only dependency state of the working copy, when the caller asked for
    # it. Attached by ``ensure`` so opening the asset page can say "dependencies
    # changed, reload to install them" WITHOUT installing anything: that endpoint
    # is on every page load and must stay instant.
    deps: t.Optional[dict] = None

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
            "deps": self.deps,
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
        # Strong references to the detached reload tasks. Without this the event
        # loop is free to collect a running task, which would abandon a reload
        # mid-install with no terminal state anywhere.
        self._jobs: set[asyncio.Task] = set()

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

        It also NEVER installs dependencies. It reports their state — three file
        reads — and leaves the decision to the user, because this endpoint is on
        the asset page's load path and an implicit multi-minute install behind a
        page load is not something anyone asked for.
        """
        self._deployments[spec.deployment_name] = spec
        runtime = await self._code_locations.ensure(self._location_spec(spec))
        deps = get_deps_service().inspect(spec.workspace_path)
        logger.info(
            "deployment_ensured",
            deployment=spec.deployment_name,
            environment=spec.environment,
            env_type=spec.env_type,
            schedules_enabled=spec.schedules_enabled,
            port=runtime.port,
            alive=runtime.alive,
            deps_state=deps.state,
        )
        return DeploymentInfo(
            spec=spec,
            host=runtime.host,
            port=runtime.port,
            definitions=runtime.definitions,
            alive=runtime.alive,
            deps=deps.to_dict(),
        )

    async def reload(self, spec: DeploymentSpec) -> DeploymentInfo:
        """Re-import a deployment's code after the working copy changed.

        The code-location half of a reload. Callers that also have to install the
        working copy's declared dependencies — every user-facing entry point — go
        through :meth:`reload_job` instead, which owns the whole sequence and can
        report where it failed.

        Nothing watches the filesystem on purpose: a code change becomes live
        through an audited action, never because a file was saved.
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

    # --- Reload as a job -----------------------------------------------------

    async def start_reload_job(
        self, spec: DeploymentSpec, *, source: str = "ui", actor_user_id: str = ""
    ) -> "ReloadJob":
        """Accept a reload and run it in the background.

        Returns immediately with the envelope. A reload that installs a
        scientific stack takes minutes, which is not a shape an HTTP request
        should have: the caller watches the SSE ``deployment`` topic and can read
        the authoritative state from the status endpoint at any time.

        A second request for the same working copy ATTACHES to the running job
        rather than starting a competing one — three buttons in the UI and a CLI
        all mean the same thing.
        """
        store = get_reload_job_store()
        job, created = await store.start_or_attach(
            spec.workspace_path, spec.deployment_name
        )
        if not created:
            logger.info(
                "deployment_reload_attached",
                deployment=spec.deployment_name,
                job_id=job.job_id,
                source=source,
            )
            return job

        self._deployments[spec.deployment_name] = spec
        # Detached on purpose: the request must not wait for it. The task holds a
        # reference through the store, so it cannot be garbage-collected mid-run.
        task = asyncio.create_task(
            self._run_reload_job(job.job_id, spec, source, actor_user_id)
        )
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        logger.info(
            "deployment_reload_started",
            deployment=spec.deployment_name,
            job_id=job.job_id,
            source=source,
        )
        return job

    async def _run_reload_job(
        self, job_id: str, spec: DeploymentSpec, source: str, actor_user_id: str
    ) -> None:
        """Dependencies, then the code location. In that order, and gated.

        The gate is the point of the whole design: a code location started before
        its declared packages are installed loads definitions that fail to import,
        and reporting THAT as a successful reload is the outcome this sequence
        exists to prevent. There is deliberately no "install and retry
        automatically" — a reload is a user action, and turning it into an
        unattended retry loop hides the failure it was meant to surface.
        """
        store = get_reload_job_store()
        try:
            if not await self._install_dependencies(
                job_id, spec, source=source, actor_user_id=actor_user_id
            ):
                return

            await store.advance(
                job_id,
                STAGE_STARTING_LOCATION,
                "starting the code location server",
            )
            runtime = await self._code_locations.reload(self._location_spec(spec))

            await store.advance(
                job_id, STAGE_LOADING_DEFINITIONS, "loading asset definitions"
            )
            if not runtime.alive:
                await self._fail_code_load(job_id, spec)
                return

            info = DeploymentInfo(
                spec=spec,
                host=runtime.host,
                port=runtime.port,
                definitions=runtime.definitions,
                alive=runtime.alive,
            )
            await store.succeed(
                job_id,
                info.as_dict(),
                deps_state=get_deps_service().inspect(spec.workspace_path).state,
            )
            logger.info(
                "deployment_reloaded",
                deployment=spec.deployment_name,
                environment=spec.environment,
                port=runtime.port,
                alive=True,
                job_id=job_id,
            )
        except asyncio.CancelledError:
            await store.cancel(job_id, "the reload was cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — a job must always end somewhere
            error_log_id = archive_failure(
                spec.workspace_path,
                operation="deployment_reload",
                error_code=ErrorCode.CODE_LOCATION_LOAD_FAILED,
                failure_code=FAILURE_INTERNAL,
                message=f"{type(exc).__name__}: {exc}",
                context={"deployment": spec.deployment_name, "source": source},
            )
            logger.error(
                "deployment_reload_job_crashed",
                deployment=spec.deployment_name,
                job_id=job_id,
                error_log_id=error_log_id,
                exc_info=True,
            )
            await store.fail(
                job_id,
                failure_code=FAILURE_INTERNAL,
                message=f"the reload failed unexpectedly: {exc}",
                error_log_id=error_log_id,
            )

    async def _install_dependencies(
        self, job_id: str, spec: DeploymentSpec, *, source: str, actor_user_id: str
    ) -> bool:
        """Run the dependency stages. Returns whether the reload may continue."""
        store = get_reload_job_store()
        deps = get_deps_service()

        await store.advance(
            job_id, STAGE_RESOLVING_DEPS, "reading the dependency declaration"
        )
        status = deps.inspect(spec.workspace_path)
        if status.needs_install:
            await store.advance(
                job_id,
                STAGE_INSTALLING_DEPS,
                f"installing {status.declared} declared package(s)",
                deps_state=status.state,
            )
        else:
            await store.advance(
                job_id,
                STAGE_INSTALLING_DEPS,
                "dependencies are already up to date",
                deps_state=status.state,
            )

        outcome = await deps.sync(
            spec.workspace_path,
            source=source,
            actor_user_id=actor_user_id,
            tenant_id=spec.tenant_id,
            workspace_id=spec.workspace_id,
            environment=spec.environment,
            on_line=lambda line: store.append_output(job_id, line),
        )
        if outcome.ok:
            if outcome.relaxed:
                # The install succeeded by overriding platform packages for this
                # copy. Surfaced on the job, not only in the manifest: the user
                # is looking at the reload panel right now, and this is the one
                # moment the difference can be explained in context.
                await store.note_relaxed(
                    job_id, [dict(entry) for entry in outcome.relaxed]
                )
            return True

        await store.fail(
            job_id,
            failure_code=outcome.failure_code or FAILURE_DEPS_INSTALL,
            message=outcome.message,
            output_tail=outcome.output_tail,
            error_log_id=outcome.error_log_id,
            deps_state=outcome.status.state if outcome.status else "",
            # A platform conflict is fixable by one edit per line, and the
            # rewrite was already computed. Carrying it here means the failed
            # reload panel offers the same action as the read-only status card.
            conflicts=[item.to_dict() for item in outcome.conflicts],
            # The transitive shape is only knowable after a resolve, so a failed
            # reload is the FIRST moment it can be shown — which is exactly when
            # the user is looking at the panel.
            transitive_conflicts=[
                item.to_dict() for item in outcome.transitive_conflicts
            ],
        )
        return False

    async def _fail_code_load(self, job_id: str, spec: DeploymentSpec) -> None:
        """Report a location that started but never served.

        Before this existed the endpoint answered ``alive=false`` with a 200 and
        the user had nothing to act on. The process's own transcript ends with the
        import traceback, so that is what is carried into the job and archived.
        """
        detail = self._code_locations.load_failure(spec.deployment_name)
        tail = str(detail.pop("output_tail", "") or "")
        summary = (
            "the code location started but did not serve its definitions"
            if not detail
            else (
                "the code location process exited while importing the project "
                f"({', '.join(f'{k}={v}' for k, v in detail.items())})"
            )
        )
        error_log_id = archive_failure(
            spec.workspace_path,
            operation="deployment_reload",
            error_code=ErrorCode.CODE_LOCATION_LOAD_FAILED,
            failure_code=FAILURE_CODE_LOAD,
            message=summary,
            output_tail=tail,
            context={"deployment": spec.deployment_name, **detail},
        )
        await get_reload_job_store().fail(
            job_id,
            failure_code=FAILURE_CODE_LOAD,
            message=summary,
            output_tail=tail,
            error_log_id=error_log_id,
        )

    async def destroy(self, deployment_name: str) -> bool:
        """Stop a deployment and forget it.

        Called by the environment destroy orchestration. Returns whether a code
        location was actually running, so the caller can report honestly instead
        of claiming a cleanup it did not perform.
        """
        spec = self._deployments.pop(deployment_name, None)
        stopped = await self._code_locations.remove(deployment_name)
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
        from src.codeloc.client import get_dagster_client

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
