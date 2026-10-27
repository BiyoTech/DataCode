"""Egress proxy tests — end-to-end through a real socket.

The proxy is exercised over a real TCP connection with a real upstream server,
because the properties that matter (a refused connection never reaches upstream, a
tunnel actually relays bytes, the byte counter advances) are not observable from
unit-testing the decision functions alone.

Name resolution and the grant store are injected; nothing here touches the network
beyond localhost.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from datacode_common.egress_grant import EgressGrant, EgressGrantStore, EgressGrantUnavailable
from datacode_common.security.task_token import (
    TASK_TOKEN_HEADER,
    TaskTokenClaims,
    mint_task_token,
)

from src import server as server_module
from src.config import Settings
from src.server import (
    CODE_FORBIDDEN,
    CODE_QUOTA,
    CODE_UNAUTHENTICATED,
    CODE_UNAVAILABLE,
    EgressProxy,
)

SECRET = "proxy-test-secret"
TASK_ID = "task0001"
TENANT = "tenant-1"


# ── doubles ───────────────────────────────────────────────────────────────


class FakeStore(EgressGrantStore):
    """A grant store backed by a dict, with no Redis and no fallback ambiguity."""

    def __init__(self, grant: EgressGrant | None, *, unavailable: bool = False) -> None:
        super().__init__(allow_local_fallback=True)
        self.grant = grant
        self.unavailable = unavailable
        self.used = 0
        self.recorded: list[int] = []

    async def fetch(self, task_id: str):
        if self.unavailable:
            raise EgressGrantUnavailable("store is down")
        return self.grant if self.grant and self.grant.task_id == task_id else None

    async def usage(self, task_id: str) -> int:
        return self.used

    async def add_usage(self, task_id: str, transferred: int, *, ttl_seconds: int) -> int:
        self.recorded.append(transferred)
        self.used += transferred
        return self.used


async def _upstream_echo() -> tuple[asyncio.AbstractServer, int]:
    """A server that echoes what it receives, so a tunnel can be observed."""

    async def handle(reader, writer):
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class SandboxSide:
    """The sandbox's end of a socket pair, wrapped for readable assertions."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    async def readuntil(self, separator: bytes) -> bytes:
        return await self._reader.readuntil(separator)

    async def readexactly(self, count: int) -> bytes:
        return await self._reader.readexactly(count)

    def write_data(self, payload: bytes) -> None:
        self._writer.write(payload)

    def close(self) -> None:
        self._writer.close()


async def _stream_pair() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, SandboxSide]:
    """A connected pair of asyncio streams.

    Returns the PROXY's view (reader/writer, as its handler would receive) and the
    SANDBOX's view. Built from a real socket pair rather than mocks so the relay is
    exercised over actual transports, including backpressure and EOF.
    """
    server_streams: asyncio.Future = asyncio.get_running_loop().create_future()

    async def accept(reader, writer):
        if not server_streams.done():
            server_streams.set_result((reader, writer))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
    proxy_reader, proxy_writer = await asyncio.wait_for(server_streams, timeout=3)
    server.close()
    return proxy_reader, proxy_writer, SandboxSide(client_reader, client_writer)


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _proxy_settings(monkeypatch):
    """Bind to an ephemeral port with short timeouts and the test secret."""
    monkeypatch.setenv("DATACODE_TASK_TOKEN_SECRET", SECRET)
    monkeypatch.setattr(
        server_module,
        "settings",
        Settings(
            host="127.0.0.1",
            port=0,
            connect_timeout=2.0,
            idle_timeout=1.0,
            tunnel_timeout=5.0,
            allow_local_grant_fallback=True,
        ),
    )


def _token(**overrides) -> str:
    claims = TaskTokenClaims(
        task_id=overrides.pop("task_id", TASK_ID),
        tenant_id=overrides.pop("tenant_id", TENANT),
        workspace_id="ws-1",
        isolation_profile="L2_container",
        **overrides,
    )
    return mint_task_token(claims, secret=SECRET, ttl_seconds=600)


def _grant(port: int, **overrides) -> EgressGrant:
    """A grant permitting one host on ``port``.

    The host is a name, not an address: the policy judges the RESOLVED address, so
    tests inject a resolver to decide what that name maps to.
    """
    from datacode_common.egress import EgressDestination

    overrides.setdefault("expires_at", int(time.time()) + 600)
    return EgressGrant(
        task_id=overrides.pop("task_id", TASK_ID),
        tenant_id=overrides.pop("tenant_id", TENANT),
        destinations=(
            EgressDestination(host="warehouse.test", ports=(port,), kind="warehouse"),
        ),
        **overrides,
    )


async def _serve(
    store: EgressGrantStore, resolver=None
) -> tuple[EgressProxy, int]:
    proxy = EgressProxy(store=store, resolver=resolver)
    server = await proxy.start()
    return proxy, server.sockets[0].getsockname()[1]


async def _request(
    port: int, target: str, *, token: str | None = None, method: str = "CONNECT"
) -> tuple[str, dict, asyncio.StreamReader, asyncio.StreamWriter]:
    """Send a CONNECT and return (status line, parsed body, streams)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"{method} {target} HTTP/1.1\r\nHost: {target}\r\n"
    if token:
        head += f"{TASK_TOKEN_HEADER}: {token}\r\n"
    writer.write((head + "\r\n").encode())
    await writer.drain()

    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
    text = raw.decode("latin-1")
    status = text.split("\r\n", 1)[0]
    body: dict = {}
    length = 0
    for line in text.split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    if length:
        body = json.loads(await asyncio.wait_for(reader.readexactly(length), timeout=3))
    return status, body, reader, writer


# ── tests ─────────────────────────────────────────────────────────────────


class TestAuthentication:
    async def test_no_token_is_refused_with_407(self):
        proxy, port = await _serve(FakeStore(None))
        try:
            status, body, _, writer = await _request(port, "warehouse.test:443")
            writer.close()
        finally:
            await proxy.close()

        assert "407" in status
        assert body["code"] == CODE_UNAUTHENTICATED

    async def test_forged_token_is_refused(self):
        forged = mint_task_token(
            TaskTokenClaims(task_id=TASK_ID, tenant_id="attacker"),
            secret="a-different-secret",
            ttl_seconds=600,
        )
        proxy, port = await _serve(FakeStore(None))
        try:
            status, body, _, writer = await _request(
                port, "warehouse.test:443", token=forged
            )
            writer.close()
        finally:
            await proxy.close()

        assert "407" in status
        assert body["code"] == CODE_UNAUTHENTICATED
        # The reason must not leak which half failed.
        assert "signature" not in body["message"].lower()

    async def test_expired_token_is_refused(self):
        expired = mint_task_token(
            TaskTokenClaims(
                task_id=TASK_ID, tenant_id=TENANT, expires_at=int(time.time()) - 3600
            ),
            secret=SECRET,
        )
        proxy, port = await _serve(FakeStore(None))
        try:
            status, _, _, writer = await _request(
                port, "warehouse.test:443", token=expired
            )
            writer.close()
        finally:
            await proxy.close()

        assert "407" in status

    async def test_proxy_refuses_to_start_without_a_secret(self, monkeypatch):
        """A proxy that cannot verify tokens must fail loudly, not deny silently."""
        monkeypatch.delenv("DATACODE_TASK_TOKEN_SECRET", raising=False)

        with pytest.raises(RuntimeError, match="not set"):
            await EgressProxy(store=FakeStore(None)).start()


class TestGrants:
    async def test_missing_grant_denies(self):
        proxy, port = await _serve(FakeStore(None))
        try:
            status, body, _, writer = await _request(
                port, "warehouse.test:443", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()

        assert "403" in status
        assert body["code"] == CODE_FORBIDDEN

    async def test_unreachable_store_denies_with_503(self):
        """Fail closed: an outage must not open the gate."""
        proxy, port = await _serve(FakeStore(None, unavailable=True))
        try:
            status, body, _, writer = await _request(
                port, "warehouse.test:443", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()

        assert "503" in status
        assert body["code"] == CODE_UNAVAILABLE

    async def test_tenant_mismatch_between_token_and_grant_denies(self):
        upstream, upstream_port = await _upstream_echo()
        store = FakeStore(_grant(upstream_port, tenant_id="tenant-other"))
        proxy, port = await _serve(store)
        try:
            status, _, _, writer = await _request(
                port, f"warehouse.test:{upstream_port}", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()
            upstream.close()

        assert "403" in status

    async def test_expired_grant_denies(self):
        store = FakeStore(_grant(443, expires_at=int(time.time()) - 10))
        proxy, port = await _serve(store)
        try:
            status, _, _, writer = await _request(
                port, "warehouse.test:443", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()

        assert "403" in status


class TestDestinationPolicy:
    async def test_undeclared_host_is_denied(self):
        upstream, upstream_port = await _upstream_echo()
        proxy, port = await _serve(FakeStore(_grant(upstream_port)))
        try:
            status, body, _, writer = await _request(
                port, f"evil.test:{upstream_port}", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()
            upstream.close()

        assert "403" in status
        assert body["code"] == CODE_FORBIDDEN

    async def test_denial_carries_the_contract_code_and_destination(self):
        """The 08 §7 contract: an EGRESS_DENIED refusal names its destination.

        "Your egress was denied" without the host is an unsolvable riddle for
        the user who declared that host — the destination is the one piece of
        information that makes the refusal actionable.
        """
        upstream, upstream_port = await _upstream_echo()
        proxy, port = await _serve(FakeStore(_grant(upstream_port)))
        try:
            status, body, _, writer = await _request(
                port, f"evil.test:{upstream_port}", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()
            upstream.close()

        assert "403" in status
        assert body["code"] == "EGRESS_DENIED"
        assert body["destination"] == f"evil.test:{upstream_port}"

    async def test_internal_address_is_denied_even_when_declared(self):
        """The T3/T6/T7 property, asserted through the real socket path."""
        from datacode_common.egress import EgressDestination

        grant = EgressGrant(
            task_id=TASK_ID,
            tenant_id=TENANT,
            destinations=(
                EgressDestination(host="169.254.169.254", ports=(80,), kind="warehouse"),
            ),
            expires_at=int(time.time()) + 600,
        )
        proxy, port = await _serve(FakeStore(grant))
        try:
            status, _, _, writer = await _request(
                port, "169.254.169.254:80", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()

        assert "403" in status

    async def test_only_connect_is_supported(self):
        proxy, port = await _serve(FakeStore(None))
        try:
            status, body, _, writer = await _request(
                port, "http://example.com/x", token=_token(), method="GET"
            )
            writer.close()
        finally:
            await proxy.close()

        assert "400" in status
        assert "CONNECT" in body["message"]

    async def test_declared_host_resolving_publicly_is_authorized(self):
        """The ALLOW decision: everything up to and including the policy passed.

        Asserted on the decision rather than on a 200 through a socket, because an
        allowed destination must resolve to a PUBLIC address (loopback is refused
        by design, and rightly so), and dialling a real public address from a test
        would make it depend on the network. The relay that follows an allow is
        covered end-to-end in TestTunnel.
        """
        grant = _grant(5432)
        proxy = EgressProxy(
            store=FakeStore(grant), resolver=lambda host: ["93.184.216.34"]
        )
        claims = TaskTokenClaims(task_id=TASK_ID, tenant_id=TENANT)
        request = server_module.ConnectRequest(host="warehouse.test", port=5432)

        decision, used = await proxy._authorize(claims, grant, request, peer="test")

        assert decision.allowed, decision.detail
        assert decision.resolved == ("93.184.216.34",)
        assert used == 0

    async def test_dns_rebinding_is_denied_through_the_real_path(self):
        """A declared host resolving inside the cluster must not be tunnelled."""
        proxy, port = await _serve(
            FakeStore(_grant(443)), resolver=lambda host: ["10.0.0.5"]
        )
        try:
            status, body, _, writer = await _request(
                port, "warehouse.test:443", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()

        assert "403" in status
        assert body["code"] == CODE_FORBIDDEN

    async def test_profile_denied_cidrs_from_the_grant_are_enforced(self):
        """A range only the profile named must still be refused.

        The proxy cannot know which profile a task ran under, so the ranges travel
        in the grant. Without this the profile's ``network.denied_cidrs`` would be
        declared and never enforced.
        """
        grant = _grant(443, denied_cidrs=("93.184.216.0/24",))
        proxy, port = await _serve(
            FakeStore(grant), resolver=lambda host: ["93.184.216.34"]
        )
        try:
            status, _, _, writer = await _request(
                port, "warehouse.test:443", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()

        assert "403" in status


class TestQuota:
    async def test_exhausted_quota_returns_429(self):
        store = FakeStore(_grant(443, byte_quota=1000))
        store.used = 1000
        proxy, port = await _serve(store)
        try:
            status, body, _, writer = await _request(
                port, "warehouse.test:443", token=_token()
            )
            writer.close()
        finally:
            await proxy.close()

        assert "429" in status
        assert body["code"] == CODE_QUOTA


class TestTunnel:
    """The relay layer, given a decision the policy already made.

    Driven through :meth:`EgressProxy._tunnel` rather than through a socket,
    because an end-to-end tunnel needs an upstream the policy would ACCEPT — and
    the only addresses available in a test are loopback ones, which the policy
    refuses by design (correctly: that is the T3 rule). Patching the policy to
    make them pass would test a proxy that does not exist, so the two halves are
    verified separately: the decision path through real sockets above, and the
    relay path here with a decision handed to it.
    """

    async def test_relays_both_directions_and_accounts_bytes(self):
        upstream, upstream_port = await _upstream_echo()
        store = FakeStore(_grant(upstream_port))
        proxy = EgressProxy(store=store)
        claims = TaskTokenClaims(task_id=TASK_ID, tenant_id=TENANT)
        decision = server_module.EgressDecision(
            True,
            "allowed",
            "test",
            host="warehouse.test",
            port=upstream_port,
            resolved=("127.0.0.1",),
        )

        # A socket pair stands in for the sandbox's side of the connection.
        client_reader, client_writer, sandbox = await _stream_pair()
        try:
            tunnel = asyncio.create_task(
                proxy._tunnel(
                    client_reader,
                    client_writer,
                    claims,
                    store.grant,
                    decision,
                    used=0,
                    peer="test",
                )
            )
            # The 200 response, then a round trip through the echo upstream.
            head = await asyncio.wait_for(sandbox.readuntil(b"\r\n\r\n"), timeout=3)
            assert b"200" in head

            sandbox.write_data(b"hello-warehouse")
            echoed = await asyncio.wait_for(sandbox.readexactly(15), timeout=3)
            assert echoed == b"hello-warehouse"

            sandbox.close()
            await asyncio.wait_for(tunnel, timeout=5)
        finally:
            upstream.close()
            await upstream.wait_closed()

        # Both directions counted, and the cumulative counter advanced.
        assert store.recorded, "byte usage was never recorded"
        assert store.used == 30  # 15 sent + 15 received

    async def test_unreachable_upstream_returns_502(self):
        """A closed port must be reported, not left as a silent hang."""
        # Bind and immediately release a port, so nothing listens on it.
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()

        store = FakeStore(_grant(dead_port))
        proxy = EgressProxy(store=store)
        claims = TaskTokenClaims(task_id=TASK_ID, tenant_id=TENANT)
        decision = server_module.EgressDecision(
            True, "allowed", "test", host="warehouse.test",
            port=dead_port, resolved=("127.0.0.1",),
        )

        client_reader, client_writer, sandbox = await _stream_pair()
        await proxy._tunnel(
            client_reader, client_writer, claims, store.grant, decision,
            used=0, peer="test",
        )
        head = await asyncio.wait_for(sandbox.readuntil(b"\r\n\r\n"), timeout=3)
        sandbox.close()

        assert b"502" in head
        assert not store.recorded
