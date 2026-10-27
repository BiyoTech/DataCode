"""HTTP client for workspace-runtime's env-control channel (design 06).

The control plane owns the registry and the state machine but must have NO
filesystem access; this client is the single hop through which every Git /
SQLMesh-engine / provisioning side effect is dispatched to a runtime replica.

Addressing: the part that is not optional
-----------------------------------------
In Kubernetes ``services.runtime`` is a HEADLESS Service — its DNS returns
every pod — and each replica has its OWN volume, with exactly one holding the
lease on a given working copy. Dialing the Service address therefore reaches an
arbitrary volume, which in an N-replica cell is the wrong one (N-1)/N of the
time. That failure is invisible with a single replica (dev, compose) and nearly
total in production, and it is not always loud: a probe served by a replica
that never materialized the copy answers 200 with "no worktree" / "nothing
pending" — an honest report about the wrong disk.

So every call is addressed according to :mod:`src.runtime.routing`:

* ``TargetKey.COPY``       — resolved to the replica holding that copy's lease,
                             through the same :class:`LeaseRouter` the gateway
                             and agent-service use;
* ``TargetKey.WORKSPACE``  — tried against the replicas that hold copies of the
                             workspace, holders first;
* ``TargetKey.NONE``       — sent to the Service address, correctly.

``Fanout.ALL`` visits every candidate and hands back one reply per replica, for
the answers that are unions (which members exist on disk) and the operations
that must be complete (removals, purges).

Error translation contract
--------------------------
The runtime answers errors in the platform's structured shape
(``{error_code, message, ...}``). This client maps them back onto the
LOCAL ServiceError hierarchy so registry code keeps raising typed errors
instead of parsing HTTP bodies:

* ``ENVIRONMENT_BUSY``            -> EnvironmentBusyError (409)
* ``WORKSPACE_NOT_FOUND``         -> WorkspaceNotFoundError (404)
* ``WORKSPACE_PATH_INVALID``      -> WorkspacePathInvalidError (400)
* ``GIT_REMOTE_ACCESS_DENIED``    -> GitRemoteAccessError (422)
* ``GIT_OPERATION_FAILED``        -> GitOperationError (502)
* other ``GIT_*`` / unknown codes -> generic ServiceError preserving the code
* transport failures / 5xx        -> RuntimeUnavailableError (503, retryable)

Three routing verdicts are handled HERE rather than relayed, because only this
layer can act on them (design 08 §7):

* ``409 LEASE_NOT_HELD``          — the cached route is stale: invalidate,
                                    re-resolve, retry once;
* ``202 WORKSPACE_MATERIALIZING`` — valid request, files still being cloned:
                                    wait out the advertised window;
* ``503 WORKSPACE_RELOCATING``    — the copy is moving: same, then re-resolve.

Where the Git fields come from
------------------------------
``ServiceError.to_response_dict`` INLINES a structured detail at the top level
of the body, so the runtime's Git errors arrive as top-level
``git_error_reason`` / ``git_operation`` / ``git_remote_host`` / ``git_detail``
— NOT nested under ``detail``. Reading them from ``detail`` (as an earlier
version did) always found nothing, so every relayed Git failure degraded to
reason ``unknown`` and the user lost the actual cause. They are read from the
body itself here, with the nested form still accepted for robustness.

Callers that prefer a boolean outcome (binder repair paths) catch
``RuntimeUnavailableError`` themselves; callers that surface the error to the
API let it propagate.
"""

from __future__ import annotations

import asyncio
import typing as t
from dataclasses import dataclass

import httpx
from datacode_common.errors import PlatformErrorCode
from datacode_common.git_reasons import GitRemoteReason
from datacode_common.lease import normalize_runtime_addr
from datacode_common.lease_routing import (
    LeaseCapacityRefused,
    LeasePlacement,
    LeaseRouter,
    LeaseUnavailable,
)
from datacode_common.naming import LeaseKeyError, lease_key

from src.config import settings
from src.runtime.fleet import RuntimeFleet, get_runtime_fleet
from src.runtime.routing import Delivery, Fanout, TargetKey, delivery_of
from src.utils.errors import (
    EnvironmentBusyError,
    ErrorCode,
    GitRemoteAccessError,
    RuntimeUnavailableError,
    ServiceError,
    WorkspaceNotFoundError,
    WorkspacePathInvalidError,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# The internal-token header the runtime expects on /internal/* routes.
INTERNAL_TOKEN_HEADER = "X-Internal-Token"

# One re-resolution after a LEASE_NOT_HELD. A contract decision rather than a
# tuning knob: either the re-resolution finds the new holder, or the lease
# subsystem itself is degraded and further attempts would turn a routing
# hiccup into a request storm against Redis.
_MAX_ROUTE_REPAIRS = 1

# Waiting out a copy that is valid but not ready yet (MATERIALIZING /
# RELOCATING). Bounded separately from route repairs because the condition is
# different: nothing is wrong, the files are simply still being produced.
_MAX_WAIT_ATTEMPTS = 3
_MAX_WAIT_SECONDS = 5.0

# Verdicts that mean "retry the same request shortly"; nothing failed.
_WAIT_CODES = frozenset(
    {
        PlatformErrorCode.WORKSPACE_MATERIALIZING.value,
        PlatformErrorCode.WORKSPACE_RELOCATING.value,
    }
)

# Every answer that is addressed to the ROUTER rather than to the caller.
_ROUTING_VERDICTS = _WAIT_CODES | {PlatformErrorCode.LEASE_NOT_HELD.value}

# Runtime error_code -> local exception factory. Codes not listed fall through
# to a generic ServiceError that preserves code, message and status.
#
# The Git entries take the WHOLE body rather than (message, detail): the Git
# fields are inlined at the top level (see the module docstring), and the
# structured detail must survive the hop untouched — the control plane relays a
# verdict it is not entitled to re-interpret.
_ERROR_MAP: dict = {
    "ENVIRONMENT_BUSY": lambda body: EnvironmentBusyError(_message_of(body)),
    "WORKSPACE_NOT_FOUND": lambda body: WorkspaceNotFoundError(_message_of(body)),
    "WORKSPACE_PATH_INVALID": lambda body: WorkspacePathInvalidError(
        _message_of(body), _detail_string_of(body)
    ),
    "GIT_REMOTE_ACCESS_DENIED": lambda body: GitRemoteAccessError(
        *_git_fields_of(body)
    ),
    "GIT_OPERATION_FAILED": lambda body: _relay_git_operation_error(body),
}


def _message_of(body: dict) -> str:
    """The body's message, or a stable placeholder when absent."""
    return str(body.get("message") or "workspace-runtime reported an error")


def _detail_string_of(body: dict) -> str:
    """The body's nested ``detail`` as a string (empty when structured/absent)."""
    detail = body.get("detail")
    return detail if isinstance(detail, str) else ""


def _git_fields_of(body: dict) -> tuple[str, str, str, str]:
    """Extract ``(operation, reason, host, detail)`` from a relayed Git error.

    Top-level first (that is where ``to_response_dict`` inlines them), falling
    back to a nested ``detail`` dict so a caller that nests still works.
    """
    nested = body.get("detail")
    nested = nested if isinstance(nested, dict) else {}

    def field(name: str, default: str = "") -> str:
        value = body.get(name)
        if value in (None, ""):
            value = nested.get(name)
        return str(value) if value not in (None, "") else default

    return (
        field("git_operation", "git operation"),
        field("git_error_reason", GitRemoteReason.UNKNOWN),
        field("git_remote_host"),
        field("git_detail"),
    )


def _relay_git_operation_error(body: dict) -> ServiceError:
    """Relay a runtime ``GIT_OPERATION_FAILED`` without re-wrapping its message.

    The upstream message already reads "Git operation failed: <operation>".
    Constructing :class:`GitOperationError` from it would prepend the prefix a
    second time — the "Git operation failed: Git operation failed: ..." the
    user saw. The message is therefore forwarded verbatim while the structured
    Git fields are rebuilt so the frontend's single Git parsing path applies.
    """
    operation, reason, host, detail = _git_fields_of(body)
    return ServiceError(
        code=ErrorCode.GIT_OPERATION_FAILED,
        message=_message_of(body),
        detail={
            "git_error_reason": reason,
            "git_operation": operation,
            "git_remote_host": host,
            "git_detail": detail,
        },
        status_code=502,
    )


class RoutingVerdictError(ServiceError):
    """A runtime answer that names WHERE to retry rather than a failure.

    ``LEASE_NOT_HELD`` / ``WORKSPACE_MATERIALIZING`` / ``WORKSPACE_RELOCATING``
    are addressed to the router, not to the caller: they say "this replica is
    not the one" or "the files are not there yet". The dispatch loop reads
    :attr:`verdict` to decide between re-resolving and waiting.

    Typed rather than carried inside ``detail``, because ``to_response_dict``
    INLINES a structured detail at the top level — an ``error_code`` key in
    there would overwrite the envelope's own and make the response describe a
    different error than the one raised. It also keeps the verdict out of the
    generic ``ServiceError`` surface the binders catch.
    """

    def __init__(
        self, verdict: str, message: str, *, status_code: int, retry_after: str
    ) -> None:
        super().__init__(
            code=ErrorCode.RUNTIME_UNAVAILABLE,
            message=message,
            status_code=status_code,
            headers={"Retry-After": retry_after},
        )
        self.verdict = verdict


@dataclass(frozen=True)
class RuntimeTarget:
    """Which working copy (or workspace) a call acts on.

    Logical identifiers, never a path: the control plane derives paths only to
    put them in request bodies the runtime re-derives anyway, and routing on a
    path would mean parsing one back into identifiers at every call site.

    ``user_id`` and ``environment`` are required for ``TargetKey.COPY``
    endpoints and ignored by the workspace-scoped ones.
    """

    tenant_id: str
    workspace_id: str
    user_id: str = ""
    environment: str = ""

    def lease_key(self) -> str:
        """The lease key of this copy.

        Raises:
            RuntimeUnavailableError: the target does not name a single copy.
                Surfaced as a 503 rather than an assertion because it can only
                come from a dispatch bug, and the request has not run.
        """
        try:
            return lease_key(
                self.tenant_id, self.workspace_id, self.user_id, self.environment
            )
        except LeaseKeyError as exc:
            raise RuntimeUnavailableError(
                "env-control dispatch",
                f"a working-copy endpoint needs a full target, got {self!r}: {exc}",
            ) from exc


@dataclass(frozen=True)
class RuntimeReply:
    """One replica's answer during a fan-out.

    ``error`` is set instead of ``body`` when that replica failed. Both are
    reported because a fan-out is COMPLETE or it is not, and a caller merging
    the results must be able to tell "this replica had nothing" from "this
    replica could not be asked".
    """

    addr: str
    body: t.Optional[dict] = None
    error: t.Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.body is not None


class RuntimeClient:
    """Typed, lease-aware wrapper over the env-control REST surface."""

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: float | None = None,
        transport: t.Optional[httpx.AsyncBaseTransport] = None,
        router: t.Optional[LeaseRouter] = None,
        fleet: t.Optional[RuntimeFleet] = None,
    ) -> None:
        self._base_url = (base_url or settings.runtime_rest_url).rstrip("/")
        self._timeout = timeout if timeout is not None else settings.runtime_request_timeout
        # Injected by tests; ``None`` means real network.
        self._transport = transport
        # Placement is what turns "nobody holds this copy" into a routable
        # answer. It needs the internal token; without it the endpoint would
        # reject us anyway, so misses stay on the degraded path.
        self._router = router or LeaseRouter(
            fallback_url=self._base_url,
            placement=LeasePlacement(self._base_url, settings.runtime_internal_token),
        )
        self._fleet = fleet if fleet is not None else get_runtime_fleet()

    @property
    def base_url(self) -> str:
        return self._base_url

    # ── single-replica surface ────────────────────────────────────────

    async def post(
        self, path: str, payload: dict, *, target: t.Optional[RuntimeTarget] = None
    ) -> dict:
        """POST ``payload`` to the replica this endpoint must reach."""
        return await self._dispatch_first("POST", path, target, json=payload)

    async def get(
        self,
        path: str,
        params: dict | None = None,
        *,
        target: t.Optional[RuntimeTarget] = None,
    ) -> dict:
        """GET with query params from the replica this endpoint must reach."""
        return await self._dispatch_first("GET", path, target, params=params)

    # ── fan-out surface ───────────────────────────────────────────────

    async def post_all(
        self,
        path: str,
        payload: dict,
        *,
        target: RuntimeTarget,
        also: t.Sequence[str] = (),
    ) -> list[RuntimeReply]:
        """POST to EVERY candidate replica, one reply each.

        For operations that are only correct when complete: a worktree left on
        a replica the release skipped resurrects the environment on that
        member's next push.

        ``also`` adds addresses the fleet view cannot see — a DRAINING replica
        is excluded from candidates (it must not receive new work) yet still
        holds files a teardown has to remove. Callers pass the holders they
        observed before releasing the leases.
        """
        return await self._dispatch_all("POST", path, target, json=payload, also=also)

    async def get_all(
        self,
        path: str,
        params: dict | None = None,
        *,
        target: t.Optional[RuntimeTarget] = None,
    ) -> list[RuntimeReply]:
        """GET from EVERY candidate replica, one reply each (union answers).

        ``target`` is required for workspace-addressed endpoints and must be
        omitted for fleet-wide ones (see :mod:`src.runtime.routing`).
        """
        return await self._dispatch_all("GET", path, target, params=params)

    async def delete_all(self, path: str) -> list[RuntimeReply]:
        """DELETE on every live replica (fleet-wide teardown of in-process state)."""
        return await self._dispatch_all("DELETE", path, None)

    # ── dispatch ──────────────────────────────────────────────────────

    async def _dispatch_first(
        self,
        method: str,
        path: str,
        target: t.Optional[RuntimeTarget],
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Send to the one replica that should answer, repairing the route once."""
        delivery = self._delivery_for(path, Fanout.FIRST, target)

        if target is None:
            # Not routed: _delivery_for has already refused every other
            # combination, so this is the "any replica" case.
            return await self._request(
                method, self._base_url, path, json=json, params=params
            )
        if delivery.key is TargetKey.COPY:
            return await self._request_copy(
                method, path, target, delivery, json=json, params=params
            )
        return await self._request_in_workspace(
            method, path, target, json=json, params=params
        )

    async def _dispatch_all(
        self,
        method: str,
        path: str,
        target: t.Optional[RuntimeTarget],
        *,
        json: dict | None = None,
        params: dict | None = None,
        also: t.Sequence[str] = (),
    ) -> list[RuntimeReply]:
        """Send to every candidate replica, collecting one reply each.

        Sequential rather than concurrent: a fan-out runs on lifecycle paths
        (release, purge, member listing), not on the hot path, and replicas
        share the coordination backend and the remotes these calls touch.
        Bounded parallelism would buy milliseconds and cost a thundering herd
        against those.
        """
        delivery = self._delivery_for(path, Fanout.ALL, target)
        if delivery.key is TargetKey.FLEET:
            candidates = list(await self._fleet_addrs())
        else:
            assert target is not None  # guaranteed by _delivery_for
            candidates = list(await self._candidates(target))
        for extra in also:
            addr = normalize_runtime_addr(extra)
            if addr and addr not in candidates:
                candidates.append(addr)

        replies: list[RuntimeReply] = []
        for addr in candidates:
            try:
                body = await self._request(method, addr, path, json=json, params=params)
            except ServiceError as exc:
                logger.warning(
                    "env_control_fanout_replica_failed",
                    path=path,
                    target=addr,
                    error=str(exc),
                )
                replies.append(RuntimeReply(addr=addr, error=exc))
            else:
                replies.append(RuntimeReply(addr=addr, body=body))
        return replies

    def _delivery_for(
        self, path: str, expected: Fanout, target: t.Optional[RuntimeTarget]
    ) -> Delivery:
        """Resolve and validate the delivery contract of ``path``.

        Refuses three dispatch bugs outright, all of which would otherwise
        produce a plausible-looking wrong answer:

        * an endpoint whose fan-out differs from the method used (a union read
          through the single-replica surface returns one replica's slice);
        * a routed endpoint called without a target (nothing to resolve);
        * a non-routed endpoint called with one (the target would be ignored,
          which hides the fact that the call is not addressed).
        """
        delivery = delivery_of(path)
        if delivery.fanout is not expected:
            raise RuntimeUnavailableError(
                f"{path}",
                f"endpoint declares fanout={delivery.fanout.value}; it must be "
                f"dispatched through the {delivery.fanout.value} surface",
            )
        if delivery.routed and target is None:
            raise RuntimeUnavailableError(
                f"{path}",
                "endpoint is addressed at a "
                f"{delivery.key.value} and was dispatched without a target",
            )
        if not delivery.routed and target is not None:
            raise RuntimeUnavailableError(
                f"{path}",
                "endpoint is served identically by every replica; passing a "
                "target suggests the caller expects it to be routed",
            )
        return delivery

    async def _request_copy(
        self,
        method: str,
        path: str,
        target: RuntimeTarget,
        delivery: Delivery,
        *,
        json: dict | None,
        params: dict | None,
    ) -> dict:
        """Send to the replica that owns one working copy.

        When the endpoint CREATES the copy the lease is placed first and the
        winner is the only correct recipient: a second candidate would be a
        second writer. Everything else is addressed at a copy that must already
        exist, and for those the lease holder is the best candidate rather than
        the only one — a working copy can outlive its lease (released, expired,
        left by a previous holder), and asking only the holder would report
        files as gone while they are still on disk.

        Two independent budgets on the preferred candidate, because the two
        conditions are different: a stale route is REPAIRED by re-resolving
        once, while a copy that is not ready yet needs WAITING and must not
        consume the repair budget.
        """
        key = target.lease_key()
        repairs = _MAX_ROUTE_REPAIRS
        waits = _MAX_WAIT_ATTEMPTS

        while True:
            preferred = await self._resolve_copy(key, delivery)
            try:
                return await self._request(
                    method, preferred, path, json=json, params=params
                )
            except ServiceError as exc:
                code = _verdict_of(exc)
                if code in _WAIT_CODES and waits > 0:
                    waits -= 1
                    await self._wait_for_copy(exc, path, key, code)
                    # Re-resolve deliberately: RELOCATING means the holder is
                    # CHANGING, so the address we just used is the one least
                    # likely to serve the retry.
                    self._forget_route(key)
                    continue
                if code == PlatformErrorCode.LEASE_NOT_HELD.value and repairs > 0:
                    repairs -= 1
                    self._forget_route(key)
                    logger.info("env_control_route_stale", path=path, lease_key=key)
                    continue
                if delivery.place_lease:
                    # The lease was just placed for this very call, so the
                    # winner is the single legitimate writer. Trying another
                    # replica would mount the copy twice.
                    raise _surfaced(exc, path) from exc
                return await self._request_remaining_candidates(
                    method, path, target, exclude=preferred, first_error=exc,
                    json=json, params=params,
                )

    def _forget_route(self, key: str) -> None:
        """Drop both cached routing views for one copy.

        They are cached separately (one lease record, one fleet snapshot) but
        they answer the same question, so refreshing one while the other still
        points at the old holder would just re-derive the stale route.
        """
        self._router.invalidate_lease_key(key)
        self._fleet.invalidate()

    async def _resolve_copy(self, key: str, delivery: Delivery) -> str:
        """Resolve one copy's holder, placing a lease only when allowed.

        ``place_lease`` is per-endpoint (see :mod:`src.runtime.routing`):
        placement makes the winner CLONE the repository, which is right before
        writing to a copy and wrong for reading about one — a probe would
        otherwise materialize files in order to report that they were absent.

        A capacity refusal propagates as a 503 instead of degrading: the fleet
        just decided there is no room, and the router's stable-hash fallback
        would pick one of the very instances placement refused.
        """
        try:
            base_url = await self._router.resolve_lease_key(
                key, allow_placement=delivery.place_lease
            )
        except LeaseCapacityRefused as exc:
            raise RuntimeUnavailableError(
                "env-control dispatch",
                f"no runtime capacity for working copy {key!r}: {exc}",
            ) from exc
        except LeaseUnavailable as exc:
            raise RuntimeUnavailableError(
                "env-control dispatch",
                f"no runtime holds working copy {key!r}: {exc}",
            ) from exc
        if not base_url:
            raise RuntimeUnavailableError(
                "env-control dispatch",
                f"lease resolution produced no address for {key!r}",
            )
        logger.debug("env_control_routed", lease_key=key, target=base_url)
        return base_url

    async def _request_remaining_candidates(
        self,
        method: str,
        path: str,
        target: RuntimeTarget,
        *,
        exclude: str,
        first_error: ServiceError,
        json: dict | None,
        params: dict | None,
    ) -> dict:
        """Try the other replicas that could hold files for this workspace.

        Reached when the preferred replica declined and the endpoint does not
        create the copy. The first error is re-raised if none of the others can
        answer either: it came from the replica most likely to be right, so it
        is the most informative one to report.
        """
        for addr in await self._candidates(target):
            if addr == exclude:
                continue
            try:
                return await self._request(
                    method, addr, path, json=json, params=params
                )
            except ServiceError as exc:
                logger.debug(
                    "env_control_candidate_declined",
                    path=path,
                    target=addr,
                    error=str(exc),
                )
        raise _surfaced(first_error, path)

    async def _request_in_workspace(
        self,
        method: str,
        path: str,
        target: RuntimeTarget,
        *,
        json: dict | None,
        params: dict | None,
    ) -> dict:
        """Ask the replicas holding copies of a workspace, holders first.

        The runtime answers these by scanning its own disk, so a replica with
        no copy of the workspace reports "absent" — indistinguishable, to the
        caller, from "does not exist anywhere". Candidates are therefore tried
        in order until one answers.

        "Answers" means "did not fail", not "found something": the response
        shapes differ per endpoint and only the binder knows what an empty one
        means. That is sound because the ordering puts lease holders first, so
        the replica most likely to hold files is asked first and the rest cover
        a copy that outlived its lease.
        """
        last_error: t.Optional[ServiceError] = None
        for addr in await self._candidates(target):
            try:
                return await self._request(
                    method, addr, path, json=json, params=params
                )
            except ServiceError as exc:
                # A replica that cannot answer must not end the search: the
                # next candidate may hold the copy this one lacks.
                last_error = exc
                logger.debug(
                    "env_control_candidate_declined",
                    path=path,
                    target=addr,
                    error=str(exc),
                )
        if last_error is not None:
            raise _surfaced(last_error, path)
        raise RuntimeUnavailableError(
            f"{method} {path}",
            f"no runtime replica could serve workspace {target.workspace_id!r}",
        )

    async def _fleet_addrs(self) -> tuple[str, ...]:
        """Every live replica, or the Service address when the fleet is unknown."""
        view = await self._fleet.view()
        if view.known and view.addrs:
            return view.addrs
        return (self._base_url,) if self._base_url else ()

    async def _candidates(self, target: RuntimeTarget) -> tuple[str, ...]:
        """Base URLs that may hold files for a workspace, best first.

        Falls back to the configured Service address when the fleet is unknown
        (Redis disabled or unreachable): that is the correct and only answer
        for a single-replica deployment, and reporting an empty candidate list
        would turn a coordination outage into "this workspace has nothing".
        """
        view = await self._fleet.view()
        if not view.known:
            return (self._base_url,) if self._base_url else ()
        candidates = view.candidates_for_workspace(
            target.tenant_id, target.workspace_id
        )
        return candidates or ((self._base_url,) if self._base_url else ())

    async def _wait_for_copy(
        self, error: ServiceError, path: str, key: str, code: str
    ) -> None:
        """Sleep out a MATERIALIZING / RELOCATING verdict.

        Both mean the request was VALID and will succeed shortly, so failing
        the orchestration on them would turn normal platform behaviour (a first
        clone, a rolling restart) into a lifecycle error. The runtime's own
        Retry-After is honoured, capped so one stuck hand-over cannot hold a
        lifecycle operation open indefinitely.
        """
        delay = min(_retry_after_seconds(error), _MAX_WAIT_SECONDS)
        logger.info(
            "env_control_copy_not_ready",
            path=path,
            lease_key=key,
            error_code=code,
            delay_seconds=delay,
        )
        await asyncio.sleep(delay)

    async def _request(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if not base_url:
            raise RuntimeUnavailableError(
                f"{method} {path}",
                "services.runtime is not configured in platform.yaml",
            )

        url = f"{base_url}{path}"
        headers = {INTERNAL_TOKEN_HEADER: settings.runtime_internal_token}
        try:
            async with self._client() as client:
                resp = await client.request(
                    method, url, json=json, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "runtime_request_unreachable",
                method=method,
                path=path,
                target=base_url,
                error=str(exc),
            )
            raise RuntimeUnavailableError(f"{method} {path}", str(exc)) from exc

        if resp.status_code >= 400:
            raise self._translate_error(method, path, resp)
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeUnavailableError(
                f"{method} {path}", f"malformed response body: {exc}"
            ) from exc

        # A 202 is a SUCCESS status carrying a routing verdict, so it never
        # reaches _translate_error. Raised here as the verdict the copy-dispatch
        # loop waits on; relaying the body would hand the caller an empty
        # result for a request that has not run.
        if isinstance(body, dict):
            code = str(body.get("error_code", ""))
            if code in _WAIT_CODES:
                raise RoutingVerdictError(
                    code,
                    str(body.get("message") or code),
                    status_code=resp.status_code,
                    retry_after=resp.headers.get("Retry-After", "2"),
                )
        return body

    def _translate_error(
        self, method: str, path: str, resp: httpx.Response
    ) -> ServiceError:
        """Map the runtime's structured error onto the local hierarchy."""
        body: dict = {}
        try:
            decoded = resp.json()
            if isinstance(decoded, dict):
                body = decoded
        except ValueError:
            pass

        code = str(body.get("error_code", ""))
        if not body.get("message"):
            body = dict(body, message=f"runtime {resp.status_code} on {path}")

        factory = _ERROR_MAP.get(code)
        if factory is not None:
            return factory(body)

        detail = body.get("detail")
        # Routing verdicts stay distinguishable: the copy-dispatch loop reads
        # them to decide between repairing the route and waiting, and a generic
        # 409 would make LEASE_NOT_HELD indistinguishable from a domain
        # conflict the caller must surface.
        if code in _ROUTING_VERDICTS:
            return RoutingVerdictError(
                code,
                str(body["message"]),
                status_code=resp.status_code,
                retry_after=resp.headers.get("Retry-After", "2"),
            )

        return ServiceError(
            ErrorCode(code) if code in ErrorCode._value2member_map_ else ErrorCode.INTERNAL_ERROR,
            str(body["message"]),
            detail=detail if isinstance(detail, dict) else (
                str(detail) if detail is not None else None
            ),
            status_code=502 if resp.status_code >= 500 else resp.status_code,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)


def _verdict_of(error: ServiceError) -> str:
    """The routing verdict an error carries, "" when it is an ordinary failure."""
    return getattr(error, "verdict", "")


def _surfaced(error: ServiceError, path: str) -> ServiceError:
    """The error a caller should see once every routing budget is spent.

    A routing verdict is addressed to the router; relayed as-is it would read
    as a domain 409 ("conflict") to the binders and to the API. Out of budget,
    it means the copy is not reachable right now — a retryable 503.
    """
    verdict = _verdict_of(error)
    if not verdict:
        return error
    return RuntimeUnavailableError(
        path, f"working copy not reachable after routing retries ({verdict})"
    )


def _retry_after_seconds(error: ServiceError) -> float:
    """The advertised wait, defaulting to a short one when absent or unusable."""
    headers = getattr(error, "headers", None) or {}
    try:
        return max(float(headers.get("Retry-After", "")), 0.0)
    except (TypeError, ValueError):
        return 1.0


_client: RuntimeClient | None = None


def get_runtime_client() -> RuntimeClient:
    """Get or create the global :class:`RuntimeClient`.

    Shared on purpose: the lease route cache and the fleet view live on it, and
    a client per call would re-resolve every request — putting Redis back on
    the path these caches exist to keep it off.
    """
    global _client
    if _client is None:
        _client = RuntimeClient()
    return _client


def set_runtime_client(client: RuntimeClient | None) -> None:
    """Replace the global client (tests wire a fake here)."""
    global _client
    _client = client
