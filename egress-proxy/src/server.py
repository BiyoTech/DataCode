"""The proxy server — identify, authorize, tunnel, account (design 04 §3.4).

One coroutine per client connection, with a fixed order that is the whole security
argument:

1.  **Identify.** No valid task token, no connection. The token is signed and
    self-scoping, so this step also determines WHICH tenant's allow-list applies —
    a sandbox cannot select another tenant's rules by claiming to be it.
2.  **Fetch the grant.** What this specific task may reach, published by the
    runtime when the task was submitted. Missing grant or unreachable store both
    deny; the safe answer to "I cannot tell" is "nothing".
3.  **Authorize.** The shared policy decides, including resolving the destination
    and refusing every internal address — which is what closes DNS rebinding.
4.  **Connect to the RESOLVED address**, not to the hostname again. Re-resolving
    would reopen the very TOCTOU gap step 3 closed: the second lookup could
    return a different, internal address.
5.  **Tunnel and account.** Bytes are counted and recorded when the tunnel closes.

The proxy never inspects payloads. It cannot — the tunnel is TLS — and it should
not: this is an authorization boundary, not a content filter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from datacode_common.egress import (
    DENY_QUOTA,
    EgressDecision,
    EgressPolicy,
    ResolverFn,
)
from datacode_common.egress_grant import (
    EgressGrant,
    EgressGrantStore,
    EgressGrantUnavailable,
)
from datacode_common.errors import PlatformErrorCode
from datacode_common.security.task_token import (
    TaskTokenClaims,
    TaskTokenError,
    extract_task_token,
    task_token_secret,
    verify_task_token,
)

from src import audit
from src.config import settings
from src.connect import (
    ConnectRequest,
    ProtocolError,
    established,
    parse_connect,
    refusal,
)

logger = logging.getLogger(__name__)

# Chunk size for relaying. Large enough that a bulk transfer is not syscall-bound,
# small enough that a slow reader does not pin megabytes per tunnel.
_RELAY_CHUNK = 64 * 1024

# Stable refusal codes, mirroring the policy's own reasons where they overlap.
# CODE_FORBIDDEN is the platform contract literal (design 08 §7): the sandbox
# that receives the refusal surfaces it to the user, and the frontend can only
# localize what it can match — so the code is aliased from the shared contract
# module rather than restated here.
CODE_UNAUTHENTICATED = "unauthenticated"
CODE_FORBIDDEN = PlatformErrorCode.EGRESS_DENIED.value
CODE_QUOTA = "egress_quota_exceeded"
CODE_UNAVAILABLE = "policy_unavailable"
CODE_PROTOCOL = "bad_request"
CODE_UPSTREAM = "upstream_unreachable"


class EgressProxy:
    """Accepts CONNECT requests from sandboxes and tunnels the allowed ones."""

    def __init__(
        self,
        store: Optional[EgressGrantStore] = None,
        resolver: Optional[ResolverFn] = None,
    ) -> None:
        """Build the proxy.

        Args:
            store: Grant store. Injected in tests; defaults to the shared one.
            resolver: Name resolution used by the policy. Injected so tests can
                exercise the real decision path without touching DNS, and so a
                future deployment can point it at the controlled resolver
                explicitly rather than relying on the Pod's ``dnsConfig``.
        """
        self._store = store or EgressGrantStore(
            allow_local_fallback=settings.allow_local_grant_fallback
        )
        self._resolver = resolver
        self._server: Optional[asyncio.AbstractServer] = None
        # Guards the "is the signing secret configured" check so the failure is
        # reported once at startup instead of on every connection.
        self._secret = task_token_secret()

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> asyncio.AbstractServer:
        """Bind and start serving.

        Refuses to start without a signing secret: a proxy that cannot verify
        tokens would deny every request, and failing loudly at startup is far
        better than a fleet of sandboxes with mysteriously broken networking.
        """
        if not self._secret:
            raise RuntimeError(
                "DATACODE_TASK_TOKEN_SECRET is not set: the proxy could not verify "
                "any task token and would deny every request"
            )

        self._server = await asyncio.start_server(
            self._handle_client, settings.host, settings.port
        )
        logger.info(
            "egress_proxy_listening",
            extra={
                "host": settings.host,
                "port": settings.port,
                "local_grant_fallback": settings.allow_local_grant_fallback,
            },
        )
        return self._server

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ── per-connection flow ────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = _peer_name(writer)
        try:
            request = await self._read_request(reader, writer, peer)
            if request is None:
                return

            claims = self._authenticate(request, writer, peer)
            if claims is None:
                return

            grant = await self._fetch_grant(claims, writer, peer)
            if grant is None:
                return

            decision, used = await self._authorize(claims, grant, request, peer)
            if not decision.allowed:
                # The contract's EGRESS_DENIED body names the destination
                # (design 08 §7): the caller must see WHICH host was refused.
                await self._refuse(
                    writer,
                    *_status_for(decision),
                    detail=decision.detail,
                    destination=_destination_of(decision, request),
                )
                return

            await self._tunnel(reader, writer, claims, grant, decision, used, peer)
        except (ConnectionResetError, BrokenPipeError):
            # A sandbox that vanished mid-handshake is unremarkable.
            pass
        except Exception as exc:  # noqa: BLE001 — one bad client must not kill the proxy
            logger.error("egress_proxy_handler_failed", extra={"peer": peer, "error": str(exc)})
        finally:
            await _close_writer(writer)

    async def _read_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str
    ) -> Optional[ConnectRequest]:
        """Read and parse the CONNECT head, bounded in size and time."""
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=settings.connect_timeout
            )
        except asyncio.IncompleteReadError:
            return None  # client closed before finishing the request
        except asyncio.LimitOverrunError:
            audit.auth_failed("request_head_too_large", peer=peer)
            await self._refuse(writer, 400, CODE_PROTOCOL, "request head is too large")
            return None
        except asyncio.TimeoutError:
            audit.auth_failed("request_head_timeout", peer=peer)
            return None

        if len(raw) > settings.max_header_bytes:
            audit.auth_failed("request_head_too_large", peer=peer)
            await self._refuse(writer, 400, CODE_PROTOCOL, "request head is too large")
            return None

        try:
            return parse_connect(raw)
        except ProtocolError as exc:
            audit.auth_failed("protocol_error", peer=peer)
            await self._refuse(writer, 400, CODE_PROTOCOL, str(exc))
            return None

    def _authenticate(
        self, request: ConnectRequest, writer: asyncio.StreamWriter, peer: str
    ) -> Optional[TaskTokenClaims]:
        """Verify the task token. No token, no connection."""
        token = extract_task_token(
            task_header=request.task_token_header,
            proxy_authorization=request.proxy_authorization,
        )
        if not token:
            audit.auth_failed(
                "no_token", peer=peer, target=f"{request.host}:{request.port}"
            )
            self._queue_refusal(
                writer,
                407,
                CODE_UNAUTHENTICATED,
                "a task token is required; present it on "
                "X-Datacode-Task-Token or Proxy-Authorization",
                extra_headers={"Proxy-Authenticate": 'Basic realm="datacode-egress"'},
            )
            return None

        try:
            return verify_task_token(token, secret=self._secret)
        except TaskTokenError as exc:
            # The reason is logged but NOT returned: "expired" versus "bad
            # signature" tells an attacker which half to work on.
            audit.auth_failed(
                f"invalid_token: {exc}",
                peer=peer,
                target=f"{request.host}:{request.port}",
            )
            self._queue_refusal(
                writer, 407, CODE_UNAUTHENTICATED, "task token is not valid"
            )
            return None

    async def _fetch_grant(
        self, claims: TaskTokenClaims, writer: asyncio.StreamWriter, peer: str
    ) -> Optional[EgressGrant]:
        """Load the task's allow-list. Absent or unreadable both deny."""
        try:
            grant = await self._store.fetch(claims.task_id)
        except EgressGrantUnavailable as exc:
            logger.error(
                "egress_grant_unavailable",
                extra={"task_id": claims.task_id, "error": str(exc)},
            )
            audit.decision(
                claims,
                EgressDecision(False, CODE_UNAVAILABLE, "grant store unreachable"),
                peer=peer,
            )
            self._queue_refusal(
                writer,
                503,
                CODE_UNAVAILABLE,
                "egress policy is temporarily unavailable; no traffic is permitted "
                "while it cannot be read",
            )
            return None

        if grant is None:
            audit.decision(
                claims,
                EgressDecision(False, CODE_FORBIDDEN, "no egress grant for this task"),
                peer=peer,
            )
            self._queue_refusal(
                writer,
                403,
                CODE_FORBIDDEN,
                "this task has no egress grant; it may have finished already",
            )
            return None

        if grant.is_expired():
            audit.decision(
                claims,
                EgressDecision(False, CODE_FORBIDDEN, "egress grant expired"),
                peer=peer,
            )
            self._queue_refusal(
                writer, 403, CODE_FORBIDDEN, "this task's egress grant has expired"
            )
            return None

        if grant.tenant_id and grant.tenant_id != claims.tenant_id:
            # Token and grant disagree: a bug or a replay. Either way, deny.
            logger.error(
                "egress_grant_tenant_mismatch",
                extra={
                    "task_id": claims.task_id,
                    "token_tenant": claims.tenant_id,
                    "grant_tenant": grant.tenant_id,
                },
            )
            audit.decision(
                claims,
                EgressDecision(False, CODE_FORBIDDEN, "grant/token tenant mismatch"),
                peer=peer,
            )
            self._queue_refusal(writer, 403, CODE_FORBIDDEN, "task scope mismatch")
            return None

        return grant

    async def _authorize(
        self,
        claims: TaskTokenClaims,
        grant: EgressGrant,
        request: ConnectRequest,
        peer: str,
    ) -> tuple[EgressDecision, int]:
        """Apply the quota, then the destination policy."""
        used = await self._store.usage(claims.task_id)
        quota = grant.effective_quota
        if used >= quota:
            outcome = EgressDecision(
                False,
                DENY_QUOTA,
                f"this task has transferred {used} bytes of its {quota}-byte budget",
                host=request.host,
                port=request.port,
            )
            audit.decision(claims, outcome, peer=peer, bytes_used=used, byte_quota=quota)
            return outcome, used

        policy = EgressPolicy.build(
            grant.destinations,
            # The profile's own ranges, carried in the grant: the runtime picked
            # the profile and the proxy cannot know which one, so without this the
            # profile's denied_cidrs would be declared and never enforced.
            extra_denied_cidrs=grant.denied_cidrs,
            resolver=self._resolver,
        )
        outcome = policy.evaluate(request.host, request.port)
        # Denials are always recorded; allows only when configured, since a busy
        # task opens many connections and the tunnel_closed record already carries
        # the interesting part (the byte counts).
        if not outcome.allowed or settings.audit_allowed:
            audit.decision(claims, outcome, peer=peer, bytes_used=used, byte_quota=quota)
        return outcome, used

    # ── tunnelling ─────────────────────────────────────────────────────

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        claims: TaskTokenClaims,
        grant: EgressGrant,
        decision: EgressDecision,
        used: int,
        peer: str,
    ) -> None:
        """Open the upstream connection and relay until either side finishes."""
        # Connect to the address the policy VERIFIED, never to the name again: a
        # second lookup could return a different, internal address (TOCTOU), which
        # is precisely the hole the resolved-address check just closed.
        target_ip = decision.resolved[0] if decision.resolved else decision.host
        started = time.monotonic()

        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(target_ip, decision.port),
                timeout=settings.connect_timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "egress_upstream_failed",
                extra={
                    "task_id": claims.task_id,
                    "host": decision.host,
                    "address": target_ip,
                    "error": str(exc),
                },
            )
            await self._refuse(
                client_writer,
                502,
                CODE_UPSTREAM,
                f"could not reach {decision.host}:{decision.port}",
            )
            return

        client_writer.write(established())
        try:
            await client_writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            await _close_writer(upstream_writer)
            return

        error: Optional[str] = None
        sent = received = 0
        try:
            sent, received = await asyncio.wait_for(
                self._relay(
                    client_reader, client_writer, upstream_reader, upstream_writer
                ),
                timeout=settings.tunnel_timeout,
            )
        except asyncio.TimeoutError:
            error = f"tunnel exceeded {settings.tunnel_timeout}s"
        except (OSError, ConnectionResetError) as exc:
            error = str(exc)
        finally:
            await _close_writer(upstream_writer)

        transferred = sent + received
        total = used + transferred
        if transferred:
            # Accounted after the fact: the goal is to stop the NEXT connection of
            # an abusive task, and a Redis round trip per chunk would sit in the
            # data path for every legitimate one.
            total = await self._store.add_usage(
                claims.task_id,
                transferred,
                ttl_seconds=max(grant.expires_at - int(time.time()), 60),
            )

        audit.tunnel_closed(
            claims,
            host=decision.host,
            port=decision.port,
            sent=sent,
            received=received,
            duration_ms=int((time.monotonic() - started) * 1000),
            total_used=total,
            error=error,
        )

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> tuple[int, int]:
        """Copy in both directions until one side closes. Returns (sent, received).

        Counted from the SANDBOX's perspective: ``sent`` is data leaving the
        platform, which is the direction that matters when reading these numbers
        for exfiltration.
        """
        counters = {"sent": 0, "received": 0}

        async def pump(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, key: str
        ) -> None:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(_RELAY_CHUNK), timeout=settings.idle_timeout
                    )
                except asyncio.TimeoutError:
                    return  # idle tunnel: cut it rather than hold the resources
                if not chunk:
                    return
                counters[key] += len(chunk)
                writer.write(chunk)
                await writer.drain()

        upload = asyncio.create_task(pump(client_reader, upstream_writer, "sent"))
        download = asyncio.create_task(pump(upstream_reader, client_writer, "received"))
        done, pending = await asyncio.wait(
            {upload, download}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        # Surface a real transport error instead of reporting a clean close.
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
        return counters["sent"], counters["received"]

    # ── refusals ───────────────────────────────────────────────────────

    def _queue_refusal(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        code: str,
        message: str,
        *,
        extra_headers: Optional[dict] = None,
        extra_body: Optional[dict] = None,
    ) -> None:
        """Write a refusal without awaiting, for the synchronous decision paths."""
        try:
            writer.write(
                refusal(
                    status, code, message,
                    extra_headers=extra_headers, extra_body=extra_body,
                )
            )
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def _refuse(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        code: str,
        message: str,
        *,
        detail: str = "",
        destination: str = "",
    ) -> None:
        """Send a refusal and flush it."""
        self._queue_refusal(
            writer,
            status,
            code,
            detail or message,
            extra_body={"destination": destination} if destination else None,
        )
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass


def _status_for(decision: EgressDecision) -> tuple[int, str, str]:
    """Map a policy denial to an HTTP status and a stable code."""
    if decision.reason == DENY_QUOTA:
        return 429, CODE_QUOTA, decision.detail
    return 403, CODE_FORBIDDEN, decision.detail


def _destination_of(decision: EgressDecision, request: ConnectRequest) -> str:
    """The refused destination as the contract's ``detail.destination`` value.

    Prefers the decision's evaluated host (normalized) over the raw request
    line: when a domain resolves to a denied address, the evaluated host is
    the one the user can reason about and add to their allow-list. Only the
    host and port leave this function — never credentials, which never enter
    the CONNECT target anyway.
    """
    host = getattr(decision, "host", "") or request.host
    if not host:
        return ""
    return f"{host}:{decision.port or request.port}"


def _peer_name(writer: asyncio.StreamWriter) -> str:
    try:
        info = writer.get_extra_info("peername")
    except Exception:  # noqa: BLE001
        return ""
    if not info:
        return ""
    return f"{info[0]}:{info[1]}" if isinstance(info, tuple) and len(info) >= 2 else str(info)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    """Close a stream, tolerating a peer that already vanished."""
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass


__all__ = [
    "CODE_FORBIDDEN",
    "CODE_PROTOCOL",
    "CODE_QUOTA",
    "CODE_UNAUTHENTICATED",
    "CODE_UNAVAILABLE",
    "CODE_UPSTREAM",
    "EgressProxy",
]
