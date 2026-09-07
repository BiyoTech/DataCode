"""TaskSubmitter — routes a task to the sandbox its kind requires.

The one place that decides WHERE work runs. Callers name what they want done
(``sqlmesh.plan`` on this working copy) and never a level, a Job or a process:
that indirection is what lets P6b add credentials and P6c swap the vehicle
without touching a single call site.

Sequence for one submission:

1.  Resolve the isolation profile for the kind, from the runtime's effective
    policy (declared profiles ∩ what this process can execute).
2.  Refuse the submission if the profile's level has no backend. Refusing is the
    point — the alternative is running gated work in a weaker sandbox than the
    policy demands, which is exactly the silent downgrade the policy exists to
    prevent.
3.  Build and validate a spec (credential-free by construction).
4.  Derive the task's egress allow-list from the working copy and publish it, then
    mint the task's own credentials (P6b). Both are per task and both expire with
    it; a task whose network policy could not be published is REFUSED rather than
    launched with no reachable warehouse.
5.  Record it, run it, record the outcome, revoke the grant.

Timeouts are resolved here rather than in a backend, because the bound is the
minimum of three statements: what the caller asked for, what the profile allows,
and what the deployment permits. A backend seeing only its own number could not
apply the strictest.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any, Mapping, Optional

from datacode_common.egress_grant import (
    EgressGrant,
    EgressGrantStore,
    EgressGrantUnavailable,
)
from datacode_common.isolation import (
    IsolationLevel,
    IsolationProfile,
    NoIsolationAvailable,
)
from datacode_common.task_protocol import (
    ERR_SUBMIT_FAILED,
    TaskProtocolError,
    TaskResult,
    TaskSpec,
    TaskStatus,
)

from src.config import settings
from src.isolation.policy import BACKEND_DISABLED, runtime_isolation_policy
from src.tasks.audit import TaskAuditor, get_task_auditor
from src.tasks.backends import TaskBackend, TaskBackendError, build_backend
from src.tasks.credentials import (
    CredentialPolicyError,
    SandboxCredentials,
    build_sandbox_credentials,
)
from src.tasks.egress import resolve_working_copy_egress
from src.utils.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    # Only for annotations: importing the API-layer error type at module load
    # would make every backend import FastAPI's dependency graph.
    from src.utils.errors import ServiceError


class TaskSubmissionError(RuntimeError):
    """A task could not be submitted at all.

    Distinct from a failed task: nothing ran, so there is no user-facing result
    to interpret — the caller should surface an operator-facing error instead.
    """


def sandbox_verdict_for(task_result) -> Optional["ServiceError"]:
    """Translate a failed task's protocol code into its contract answer.

    Returns ``None`` when the failure is the WORK's, not the sandbox's — the
    caller then keeps its own domain code (an apply that failed linting is
    not a sandbox problem, whatever vehicle ran it). Only vehicle-level
    verdicts get contract statuses:

    * ``task_timeout``   -> SANDBOX_TIMEOUT  (504): killed by its own deadline
    * ``sandbox_oom``    -> SANDBOX_OOM      (507): killed by the memory limit
    * ``no_result``      -> SANDBOX_UNAVAILABLE (503): the vehicle finished
      without a classifiable reason — an honest "we could not tell", never a
      guess dressed as a verdict
    """
    # Imported lazily: submitter.py sits below the API layer and must stay
    # importable from backends that never touch FastAPI error types.
    from datacode_common.task_protocol import (
        ERR_NO_RESULT,
        ERR_SANDBOX_OOM,
        ERR_TIMEOUT,
    )

    from src.utils.errors import ErrorCode, ServiceError

    verdicts = {
        ERR_TIMEOUT: (ErrorCode.SANDBOX_TIMEOUT, 504),
        ERR_SANDBOX_OOM: (ErrorCode.SANDBOX_OOM, 507),
        ERR_NO_RESULT: (ErrorCode.SANDBOX_UNAVAILABLE, 503),
    }
    code = task_result.error_code or ""
    if code not in verdicts:
        return None
    error_code, status = verdicts[code]
    return ServiceError(
        code=error_code,
        message=task_result.error_message or code,
        detail=str(getattr(task_result, "duration_ms", "") or ""),
        status_code=status,
    )


def submission_unavailable(exc: TaskSubmissionError) -> "ServiceError":
    """Translate a TaskSubmissionError into the contract's SANDBOX_UNAVAILABLE.

    Nothing ran: no backend could provide the required isolation level, or
    the vehicle could not be created (image pull, node capacity, fork
    failure). 503 tells the caller a retry may succeed — which is exactly
    the caller's best move, unlike a 422 that invites code fixes.
    """
    from src.utils.errors import ErrorCode, ServiceError

    return ServiceError(
        code=ErrorCode.SANDBOX_UNAVAILABLE,
        message=str(exc),
        status_code=503,
        headers={"Retry-After": "5"},
    )


class TaskSubmitter:
    """Selects the isolation profile for a task and runs it on a backend."""

    def __init__(
        self,
        backend: Optional[TaskBackend] = None,
        auditor: Optional[TaskAuditor] = None,
        grant_store: Optional[EgressGrantStore] = None,
    ) -> None:
        """Build a submitter.

        Args:
            backend: Execution backend. Defaults to the configured one; injected
                in tests and by the P6c warm-sandbox wiring.
            auditor: Audit sink. Defaults to the shared one.
            grant_store: Where egress grants are published for the proxy to read.
                Defaults to the shared store.
        """
        self._backend = backend
        self._auditor = auditor or get_task_auditor()
        self._grants = grant_store or EgressGrantStore(
            allow_local_fallback=settings.egress_allow_local_grants
        )

    # ── backend lifecycle ──────────────────────────────────────────────

    @property
    def backend(self) -> TaskBackend:
        """The execution backend, built on first use.

        Lazy so that importing this module never requires a cluster connection,
        and so a misconfiguration surfaces on the first submission with a message
        naming the setting rather than at import time with a traceback.
        """
        if self._backend is None:
            configured = (settings.task_backend or BACKEND_DISABLED).strip().lower()
            if configured == BACKEND_DISABLED:
                raise TaskSubmissionError(
                    "the task execution plane is disabled (task_backend=disabled); "
                    "set it to 'local' for a single node or 'kubernetes' in a cluster"
                )
            try:
                self._backend = build_backend(configured)
            except TaskBackendError as exc:
                raise TaskSubmissionError(str(exc)) from exc
        return self._backend

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()

    # ── submission ─────────────────────────────────────────────────────

    async def submit(
        self,
        kind: str,
        workspace_path: str,
        *,
        environment: str = "",
        tenant_id: str = "",
        workspace_id: str = "",
        params: Optional[Mapping[str, Any]] = None,
        gateway: str = "",
        actor_user_id: str = "",
        timeout_seconds: int = 0,
    ) -> TaskResult:
        """Run a task in whatever sandbox its kind requires.

        Args:
            kind: Task kind, e.g. ``sqlmesh.plan``. Selects the profile.
            workspace_path: Working copy, as this runtime sees it. Backends
                translate it to whatever their vehicle sees.
            environment: Target SQLMesh environment.
            tenant_id: Owning tenant, for audit and quota attribution.
            workspace_id: Owning workspace, for audit.
            params: Kind-specific arguments. Must contain no credentials.
            gateway: Warehouse gateway, empty for the project default.
            actor_user_id: The user the task is attributed to.
            timeout_seconds: Caller's budget. 0 takes the profile's own.

        Returns:
            The task's result, including for failures — the caller branches on
            ``status`` / ``error_code``.

        Raises:
            TaskSubmissionError: Nothing ran (no backend for the required level,
                an invalid spec, or the backend could not create the vehicle).
        """
        # Backend FIRST, profile second. Both can fail, and when the execution
        # plane is misconfigured both do — but "task_backend=disabled" is the
        # actionable message, while "no fail-safe profile is enabled" is only its
        # consequence. Resolving in this order puts the cause in the error.
        backend = self.backend
        profile = self._resolve_profile(kind, backend)
        spec = self._build_spec(
            kind=kind,
            profile=profile,
            workspace_path=workspace_path,
            environment=environment,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            params=params or {},
            gateway=gateway,
            actor_user_id=actor_user_id,
            timeout_seconds=timeout_seconds,
        )

        await self._auditor.record_submitted(spec, backend=backend.name)

        # Network and credentials, both scoped to this one task (P6b). Prepared
        # AFTER the audit record so a failure here still leaves a trace of the
        # attempt, and BEFORE the vehicle starts so a sandbox never exists without
        # its policy having been published.
        credentials = await self._prepare_sandbox_access(spec, profile)

        started = time.monotonic()
        try:
            result = await self._execute(spec, profile, credentials, backend)
        except TaskBackendError as exc:
            # The vehicle could not be created. Recorded as a failure so the
            # audit trail is complete, then raised: the user's work never ran,
            # and reporting it as "your plan failed" would be a lie.
            failure = TaskResult.failure(
                spec.task_id,
                ERR_SUBMIT_FAILED,
                str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await self._auditor.record_finished(spec, failure)
            logger.error(
                "task_submit_failed",
                task_id=spec.task_id,
                kind=kind,
                backend=backend.name,
                error=str(exc),
            )
            raise TaskSubmissionError(str(exc)) from exc
        finally:
            # The grant expires on its own, but revoking closes the window in which
            # a leaked token could still reach the warehouse after the task ended.
            await self._grants.revoke(spec.task_id)

        await self._auditor.record_finished(spec, result)
        return result

    # ── vehicle selection ──────────────────────────────────────────────

    async def _execute(
        self,
        spec: TaskSpec,
        profile: IsolationProfile,
        credentials: Optional[SandboxCredentials],
        backend: TaskBackend,
    ) -> TaskResult:
        """Run the task on a resident runner when one is available, else a Job.

        The warm path is an OPTIMIZATION and is treated as one: every condition
        that makes it unavailable (disabled, pool full, Pod busy, image too old,
        transport broken) returns None from the pool and falls through to the
        one-shot vehicle. The task therefore never fails BECAUSE of the pool —
        the worst the pool can do is not help.

        Audit is unaffected: the record was written before this point and the
        result is recorded after, so a warm task and a one-shot task leave the
        same trail.
        """
        from src.tasks.warm_pool import get_warm_pool

        pool = get_warm_pool()
        if pool.poolable(spec.kind, profile):
            result = await pool.run(spec, profile, credentials)
            if result is not None:
                logger.info(
                    "task_served_warm",
                    task_id=spec.task_id,
                    kind=spec.kind,
                    status=result.status.value,
                )
                return result

        return await backend.execute(spec, profile, credentials)

    # ── per-task network and credentials (P6b) ─────────────────────────

    async def _prepare_sandbox_access(
        self, spec: TaskSpec, profile: IsolationProfile
    ) -> Optional[SandboxCredentials]:
        """Publish the task's egress grant and mint its credentials.

        Returns None when the deployment has no egress proxy: the sandbox then gets
        no proxy configuration and, under the NetworkPolicy, no egress. That is a
        legitimate state (a task acting only on the working copy needs none), and it
        is a far better default than granting direct network access because the
        proxy is missing.

        Raises:
            TaskSubmissionError: The grant could not be published while it is
                required, or the credential policy refuses the combination. Both
                mean the sandbox must not start — a task whose egress policy was
                never delivered would fail on its first warehouse connection, with
                the cause several layers away from the symptom.
        """
        if not settings.egress_proxy_url:
            logger.debug(
                "task_egress_proxy_not_configured",
                task_id=spec.task_id,
                hint="sandboxes get no egress; set egress_proxy_url to enable it",
            )
            return None

        discovered = await resolve_working_copy_egress(
            spec.workspace_path, gateway=spec.gateway
        )
        grant = EgressGrant(
            task_id=spec.task_id,
            tenant_id=spec.tenant_id,
            workspace_id=spec.workspace_id,
            isolation_profile=profile.name,
            destinations=discovered.destinations,
            # The profile's ranges travel with the grant: the proxy has no way to
            # know which profile applied, so without this they would be declared
            # and never enforced.
            denied_cidrs=_denied_cidrs_of(profile),
            byte_quota=settings.egress_byte_quota,
            expires_at=int(time.time()) + spec.timeout_seconds,
        )

        try:
            await self._grants.publish(grant, ttl_seconds=spec.timeout_seconds)
        except EgressGrantUnavailable as exc:
            if settings.egress_require_grant:
                raise TaskSubmissionError(
                    f"cannot publish the egress grant for {spec.task_id}: {exc}. "
                    "The sandbox would start with no reachable destination; "
                    "refusing rather than launching it blind."
                ) from exc
            logger.error(
                "task_egress_grant_unpublished",
                task_id=spec.task_id,
                error=str(exc),
                hint="egress_require_grant is off, so the task proceeds with no "
                "egress at all",
            )

        try:
            return build_sandbox_credentials(spec, profile)
        except CredentialPolicyError as exc:
            await self._grants.revoke(spec.task_id)
            raise TaskSubmissionError(str(exc)) from exc

    # ── internals ──────────────────────────────────────────────────────

    def _resolve_profile(self, kind: str, backend: TaskBackend) -> IsolationProfile:
        """Pick the isolation profile for a kind and check it is executable.

        An in-process profile means the caller is wrong, not the configuration:
        L1 work (file IO, YAML loading) runs directly in the runtime and must not
        pay for a vehicle.
        """
        policy = runtime_isolation_policy()
        try:
            profile = policy.profile_for(kind)
        except NoIsolationAvailable as exc:
            # No usable sandbox for this kind. Surfaced as a submission error
            # because that is what it is — the work never started — and the
            # message already names the setting to change.
            raise TaskSubmissionError(str(exc)) from exc

        if profile.level is IsolationLevel.IN_PROCESS:
            raise TaskSubmissionError(
                f"task kind {kind!r} resolves to the in-process profile "
                f"{profile.name!r}; run it directly instead of submitting it"
            )

        supported = backend.supported_levels
        if profile.level not in supported:
            # The policy layer normally prevents this by disabling unavailable
            # profiles, so reaching here means backend and policy disagree.
            available = ", ".join(sorted(level.value for level in supported))
            raise TaskSubmissionError(
                f"task kind {kind!r} requires profile {profile.name!r} "
                f"({profile.level.value}), which backend "
                f"{backend.name!r} cannot provide (supports: {available})"
            )
        return profile

    def _build_spec(
        self,
        *,
        kind: str,
        profile: IsolationProfile,
        workspace_path: str,
        environment: str,
        tenant_id: str,
        workspace_id: str,
        params: Mapping[str, Any],
        gateway: str,
        actor_user_id: str,
        timeout_seconds: int,
    ) -> TaskSpec:
        """Assemble and validate the spec."""
        spec = TaskSpec(
            task_id=self._new_task_id(),
            kind=kind,
            workspace_path=workspace_path,
            environment=environment,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            isolation_profile=profile.name,
            params=dict(params),
            timeout_seconds=self._resolve_timeout(profile, timeout_seconds),
            actor_user_id=actor_user_id,
            gateway=gateway,
        )
        try:
            return spec.validate()
        except TaskProtocolError as exc:
            # Includes the credential check: a caller that put a password in
            # params gets a refusal, not a ConfigMap containing it.
            raise TaskSubmissionError(f"invalid task spec: {exc}") from exc

    @staticmethod
    def _resolve_timeout(profile: IsolationProfile, requested: int) -> int:
        """Take the strictest of caller, profile and deployment bounds."""
        candidates = [
            value
            for value in (
                requested,
                profile.timeout_seconds,
                settings.task_max_timeout_seconds,
            )
            if value and value > 0
        ]
        # Every candidate is optional, so fall back to the profile default rather
        # than to "no limit": an unbounded task would hold a slot forever.
        return min(candidates) if candidates else 1800

    @staticmethod
    def _new_task_id() -> str:
        """Generate a task id usable as a Kubernetes object name."""
        return uuid.uuid4().hex[:20]


_submitter: Optional[TaskSubmitter] = None


def _denied_cidrs_of(profile: IsolationProfile) -> tuple[str, ...]:
    """Extra denied ranges the profile declares, on top of the shared baseline."""
    network = profile.settings.get("network") or {}
    if not isinstance(network, Mapping):
        return ()
    raw = network.get("denied_cidrs") or ()
    if isinstance(raw, (str, bytes)):
        return ()
    return tuple(str(cidr) for cidr in raw)


def get_task_submitter() -> TaskSubmitter:
    """Return the shared submitter."""
    global _submitter
    if _submitter is None:
        _submitter = TaskSubmitter()
    return _submitter


async def shutdown_task_submitter() -> None:
    """Release the shared submitter's backend. Called from the app lifespan."""
    global _submitter
    if _submitter is not None:
        await _submitter.close()
        _submitter = None


__all__ = [
    "TaskStatus",
    "TaskSubmissionError",
    "TaskSubmitter",
    "get_task_submitter",
    "shutdown_task_submitter",
]
