"""Lease-aware HTTP client for workspace-runtime (design 06 P3d, P4a).

agent-service must be able to scale on LLM concurrency alone, which it cannot do
while it owns working-copy state or spawns user processes. Every gated tool call,
every SQL query and every metadata read therefore crosses this client into
workspace-runtime, the sole owner of working copies and the home of the isolation
boundary.

Split of responsibilities, deliberately NOT collapsed into one place:

  * agent-service keeps the CONVERSATIONAL layer — the command policy and the
    L1-L6 approval flow. "Should this be done" is a dialogue question and needs
    the session to ask the user.
  * workspace-runtime owns the ISOLATION layer — a cleared environment,
    lowered rlimits, a pinned cwd, a container. "Doing it cannot hurt anyone
    else" is an infrastructure property.

Those two are orthogonal, so both apply: a command may be approved and still be
confined.

Why this client resolves a lease
--------------------------------
``services.runtime`` is a HEADLESS Service in Kubernetes: its DNS returns every
pod, so dialing it lands on an arbitrary replica. But exactly one replica holds a
working copy's lease, and only that one can serve requests for it — the others
answer ``409 LEASE_NOT_HELD``. Dialing the Service address directly therefore
fails for (N-1)/N of requests in an N-replica cell while working perfectly with a
single replica, which is why it must not be done from here even though it looks
correct in development.

So every request carrying a ``workspace_path`` is routed through the SAME lease
resolution the gateway uses (``datacode_common.lease_routing``), and the three
contract verdicts a router must understand are handled here rather than being
handed to an LLM that cannot act on them:

* ``409 LEASE_NOT_HELD``       — the cached route is stale: invalidate, re-resolve,
                                 retry once (design 08 §7);
* ``202 WORKSPACE_MATERIALIZING`` — valid request, files still being cloned: wait
                                 out the advertised window;
* ``503 WORKSPACE_RELOCATING`` — the copy is moving to another replica: same.

Requests WITHOUT a working copy (none today, but the surface allows it) go to the
configured base URL, which any replica may serve.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping, Optional

import httpx
from datacode_common.errors import PlatformErrorCode
from datacode_common.lease_routing import (
    LeaseCapacityRefused,
    LeasePlacement,
    LeaseRouter,
    LeaseUnavailable,
)

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Bounded: a stalled runtime must surface as a tool error rather than hang the
# agent's goal loop. Query and shell calls pass their own, longer timeouts.
_DEFAULT_TIMEOUT_S = 30.0

# How long to wait when the runtime says "valid, but not ready yet"
# (MATERIALIZING / RELOCATING). Capped so a stuck hand-over surfaces as a tool
# error instead of consuming the agent's whole turn budget, and the runtime's own
# Retry-After is honoured when it advertises a shorter window.
_MAX_WAIT_ATTEMPTS = 3
_MAX_WAIT_SECONDS = 5.0

# Verdicts that mean "retry the same request shortly"; nothing failed.
_WAIT_CODES = frozenset(
    {
        PlatformErrorCode.WORKSPACE_MATERIALIZING.value,
        PlatformErrorCode.WORKSPACE_RELOCATING.value,
    }
)


class RuntimeUnavailable(RuntimeError):
    """workspace-runtime could not be reached or refused the request.

    Raised instead of returning a partial result so the caller can tell "the tool
    ran and failed" from "the tool never ran" — the agent must not report a
    command as executed when it was not.
    """


class RuntimeClient:
    """Calls workspace-runtime, honouring the working copy's lease."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        router: Optional[LeaseRouter] = None,
    ) -> None:
        self._base_url = (base_url or settings.runtime_service_url).rstrip("/")
        # Placement is what turns "nobody holds this copy" into a routable
        # answer. It needs the internal token; without it the endpoint would
        # reject us anyway, so the router keeps misses on its degraded path.
        self._router = router or LeaseRouter(
            fallback_url=self._base_url,
            placement=LeasePlacement(
                self._base_url, settings.runtime_internal_token
            ),
        )

    @property
    def configured(self) -> bool:
        """Whether a runtime endpoint is known."""
        return bool(self._base_url)

    # NOTE: there is deliberately no public "resolve an address for me" method.
    #
    # One existed (``base_url_for``) for a single caller: the shell shim, which
    # needed the runtime's address as an ENVIRONMENT VARIABLE for a child process
    # so the child could call the platform back. That whole arrangement is gone —
    # governed verbs are routed by workspace-runtime before anything executes
    # (design 04 §3.6) — and re-adding the method would hand the executing side a
    # route into the platform, which the L3-base NetworkPolicy denies and which
    # tests/test_shell_no_platform_route.py asserts against structurally.

    # ── tool surface (``/rt/v1/tools/*``) ─────────────────────────────

    async def shell(
        self,
        workspace_path: str,
        command: str,
        timeout_s: float,
        *,
        user_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Execute one shell command against the working copy.

        The runtime ROUTES it (design 04 §3.6): a governed verb
        (``sqlmesh apply``, ``dc run``, ...) is served by the platform's own
        services, everything else by the sandbox. Either way the result shape is
        the same (``exit_code`` / ``stdout`` / ``stderr`` / ``timed_out``), and
        either way the command never runs in this process.

        ``user_id`` / ``session_id`` are attribution for the apply history of a
        governed verb. They are sent from here because this side knows the
        session — the sandbox does not, and used not to be told, which is exactly
        why routing could not live there.
        """
        # The HTTP timeout must outlast the sandbox's own, otherwise a command
        # the runtime is still bounding would be reported as a transport failure
        # and could be retried while it is still running.
        return await self.post(
            "/rt/v1/tools/shell",
            workspace_path=workspace_path,
            json={
                "workspace_path": workspace_path,
                "command": command,
                "timeout_s": int(timeout_s),
                "user_id": user_id,
                "session_id": session_id,
                "trigger_type": "agent",
            },
            timeout=timeout_s + 15.0,
        )

    async def read(self, workspace_path: str, file_path: str) -> dict[str, Any]:
        """Read a file from the working copy as text (L1: no code execution)."""
        return await self.post(
            "/rt/v1/tools/read",
            workspace_path=workspace_path,
            json={"workspace_path": workspace_path, "file_path": file_path},
        )

    async def read_binary(
        self, workspace_path: str, file_path: str, *, max_bytes: int
    ) -> dict[str, Any]:
        """Read a file as base64 bytes (L1) — office documents and the like."""
        return await self.post(
            "/rt/v1/tools/read-bytes",
            workspace_path=workspace_path,
            json={
                "workspace_path": workspace_path,
                "file_path": file_path,
                "max_bytes": max_bytes,
            },
        )

    async def delete(self, workspace_path: str, file_path: str) -> dict[str, Any]:
        """Delete a path inside the working copy (L1)."""
        return await self.post(
            "/rt/v1/tools/delete",
            workspace_path=workspace_path,
            json={"workspace_path": workspace_path, "file_path": file_path},
        )

    async def write(
        self, workspace_path: str, file_path: str, content: str
    ) -> dict[str, Any]:
        """Write a file into the working copy (L1)."""
        return await self.post(
            "/rt/v1/tools/write",
            workspace_path=workspace_path,
            json={
                "workspace_path": workspace_path,
                "file_path": file_path,
                "content": content,
            },
        )

    async def list_dir(
        self,
        workspace_path: str,
        path: str = "",
        pattern: str = "*",
        recursive: bool = False,
        include_dirs: bool = False,
    ) -> dict[str, Any]:
        """List the working copy's files (L1)."""
        return await self.post(
            "/rt/v1/tools/list",
            workspace_path=workspace_path,
            json={
                "workspace_path": workspace_path,
                "path": path,
                "pattern": pattern,
                "recursive": recursive,
                "include_dirs": include_dirs,
            },
        )

    async def search(
        self,
        workspace_path: str,
        query: str,
        *,
        pattern: str = "*",
        regex: bool = False,
        context_before: int = 0,
        context_after: int = 0,
        max_results: int = 50,
        include_matches: bool = True,
    ) -> dict[str, Any]:
        """Search file contents inside the working copy (L1).

        The walk happens on the runtime side; what comes back is structured
        (matches + per-file counts) and the caller renders it.
        """
        return await self.post(
            "/rt/v1/tools/search",
            workspace_path=workspace_path,
            json={
                "workspace_path": workspace_path,
                "query": query,
                "pattern": pattern,
                "regex": regex,
                "context_before": context_before,
                "context_after": context_after,
                "max_results": max_results,
                "include_matches": include_matches,
            },
            # A full-tree grep is not a metadata call: the default budget is
            # sized for those, and a large repository legitimately exceeds it.
            timeout=60.0,
        )

    # ── generic surface (engine + metadata routes) ────────────────────

    async def get(
        self,
        path: str,
        *,
        workspace_path: str,
        params: Optional[Mapping[str, Any]] = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """GET a runtime endpoint on the replica that owns this working copy."""
        return await self._request(
            "GET", path, workspace_path, params=params, timeout=timeout
        )

    async def post(
        self,
        path: str,
        *,
        workspace_path: str,
        json: Optional[Mapping[str, Any]] = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """POST to a runtime endpoint on the replica that owns this working copy."""
        return await self._request(
            "POST", path, workspace_path, json=json, timeout=timeout
        )

    # ── internals ─────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        workspace_path: str,
        *,
        json: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send one request, resolving and repairing the route as the contract says.

        Raises:
            RuntimeUnavailable: the request could not be completed. Every failure
                mode collapses here on purpose: the caller's job is to report a
                tool failure, and it has no way to act on the difference between
                a stale route and a dead pod.
        """
        if not self.configured:
            raise RuntimeUnavailable(
                "workspace-runtime URL is not configured "
                "(conf/platform.yaml services.runtime.rest_url)"
            )

        # Two independent budgets, because the two conditions are different:
        #
        # * a stale route (LEASE_NOT_HELD) is repaired by ONE re-resolution. That
        #   bound is a contract decision, not a tuning knob: either the
        #   re-resolution finds the new holder, or the lease subsystem itself is
        #   degraded and more attempts would turn a routing hiccup into a request
        #   storm against Redis.
        # * a copy that is not ready yet (MATERIALIZING / RELOCATING) needs
        #   WAITING, and it is bounded separately so a stuck hand-over surfaces
        #   as a tool error instead of consuming the agent's whole turn.
        route_repairs = 1
        waits = _MAX_WAIT_ATTEMPTS

        while True:
            base_url = await self._resolve(workspace_path)
            response = await self._send(
                method, base_url + path, json=json, params=params, timeout=timeout
            )
            code = _error_code(response)

            if (
                response.status_code == 409
                and code == PlatformErrorCode.LEASE_NOT_HELD.value
                and route_repairs > 0
            ):
                # The lease moved inside our cache window. Only we can fix this;
                # relaying it would show a platform hand-over to the LLM as a
                # failure it cannot understand.
                route_repairs -= 1
                self._router.invalidate(workspace_path)
                logger.info(
                    "runtime_route_stale", path=path, workspace_path=workspace_path
                )
                continue

            if code in _WAIT_CODES and waits > 0:
                waits -= 1
                await self._wait_for_copy(response, path, workspace_path, code)
                # Re-resolve deliberately: a relocation means the holder is
                # CHANGING, so the address we just used is the one least likely
                # to serve the retry.
                self._router.invalidate(workspace_path)
                continue

            if response.status_code >= 400:
                detail = _error_detail(response)
                logger.warning(
                    "runtime_request_failed",
                    method=method,
                    path=path,
                    status=response.status_code,
                    detail=detail,
                )
                raise RuntimeUnavailable(
                    f"workspace-runtime returned {response.status_code}: {detail}"
                )

            if code in _WAIT_CODES:
                # Budget exhausted on a 202: the request never actually ran, so
                # reporting success with an empty body would make the agent act
                # on data it does not have.
                raise RuntimeUnavailable(
                    f"working copy is still not ready ({code}) after "
                    f"{_MAX_WAIT_ATTEMPTS} attempts"
                )
            return _decode(response)

    async def _resolve(self, workspace_path: str) -> str:
        """Resolve the replica holding this working copy.

        A capacity refusal is surfaced as-is rather than degraded: the fleet just
        decided there is no room, and routing anyway would defeat the quota.
        """
        try:
            base_url = await self._router.resolve(workspace_path)
        except LeaseCapacityRefused as exc:
            raise RuntimeUnavailable(
                f"no runtime capacity for this working copy: {exc}"
            ) from exc
        except LeaseUnavailable as exc:
            raise RuntimeUnavailable(
                f"no runtime holds this working copy: {exc}"
            ) from exc
        if not base_url:
            raise RuntimeUnavailable(
                "lease resolution produced no runtime address for "
                f"{workspace_path!r}"
            )
        return base_url

    async def _send(
        self,
        method: str,
        url: str,
        *,
        json: Optional[Mapping[str, Any]],
        params: Optional[Mapping[str, Any]],
        timeout: float,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.request(
                    method, url, json=json, params=params
                )
        except httpx.HTTPError as exc:
            logger.warning("runtime_unreachable", url=url, error=str(exc))
            raise RuntimeUnavailable(
                f"cannot reach workspace-runtime: {exc}"
            ) from exc

    async def _wait_for_copy(
        self,
        response: httpx.Response,
        path: str,
        workspace_path: str,
        code: str,
    ) -> None:
        """Sleep out a MATERIALIZING / RELOCATING verdict.

        Both codes mean the request was VALID and will succeed shortly, so
        surfacing them to the LLM would turn normal platform behaviour (a first
        clone, a rolling restart) into an apparent error. The runtime's own
        Retry-After is honoured, capped so one stuck hand-over cannot hold the
        agent's turn open.
        """
        delay = min(_retry_after_seconds(response), _MAX_WAIT_SECONDS)
        logger.info(
            "runtime_copy_not_ready",
            path=path,
            workspace_path=workspace_path,
            error_code=code,
            delay_seconds=delay,
        )
        await asyncio.sleep(delay)


def _error_code(response: httpx.Response) -> str:
    """The platform error code a runtime response carries, "" when none.

    Read from the BODY rather than inferred from the status: 409 also covers
    conflicts that are not routing verdicts, and 202/503 have meanings unrelated
    to leases. Acting on the status alone would retry the wrong things.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return ""
    return str(body.get("error_code", "")) if isinstance(body, dict) else ""


def _retry_after_seconds(response: httpx.Response) -> float:
    """The advertised wait, defaulting to a short one when absent or unusable."""
    raw = response.headers.get("Retry-After", "")
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return 1.0


def _decode(response: httpx.Response) -> dict[str, Any]:
    """Decode a success body, tolerating an empty one."""
    if not response.content:
        return {}
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeUnavailable(
            f"workspace-runtime returned a malformed body: {exc}"
        ) from exc
    return body if isinstance(body, dict) else {"result": body}


def _error_detail(response: httpx.Response) -> str:
    """Extract a readable message from a runtime error response."""
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("detail") or body)[:200]
    return str(body)[:200]


_client: Optional[RuntimeClient] = None


def get_runtime_client() -> RuntimeClient:
    """Process-wide runtime client singleton.

    Shared on purpose: the lease route cache lives on it, and a client per call
    would re-resolve every request — putting Redis back on the hot path this
    cache exists to keep it off.
    """
    global _client
    if _client is None:
        _client = RuntimeClient()
    return _client
