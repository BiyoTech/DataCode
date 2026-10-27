"""agent-service must reach workspace-runtime through the lease (design 06 P4a).

Why this file exists
--------------------
``services.runtime`` is a HEADLESS Service in Kubernetes: its DNS returns every
pod, so dialing it lands on an arbitrary replica. Exactly one replica holds a
working copy's lease and the others answer ``409 LEASE_NOT_HELD``, so a direct
dial works perfectly with one replica and fails for (N-1)/N of requests with N.

That asymmetry is the trap: the bug is invisible in development and near-total in
production, and its symptom is an infrastructure error code delivered to an LLM,
which cannot act on it and may fabricate an answer instead. The tests below pin
both halves of the fix — that routing happens at all, and that the three contract
verdicts are handled here rather than relayed.

The last test is the anti-regression one: it asserts no tool module dials the
configured runtime URL itself. Without it the next tool added would reintroduce
exactly this defect, since the direct call reads perfectly well.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.lease_routing import LeaseRouter  # noqa: E402

from src.tools.runtime_client import (  # noqa: E402
    RuntimeClient,
    RuntimeUnavailable,
)

WORKSPACE = "/base/t1/ws-t1-shop/u1/envs/dev"

HOLDER = "http://10.0.0.2:8002"
NON_HOLDER = "http://10.0.0.1:8002"

LEASE_NOT_HELD_BODY = {
    "error_code": "LEASE_NOT_HELD",
    "message": "This runtime does not hold the lease for the requested working copy",
}


class StubRouter(LeaseRouter):
    """A router whose answers the test controls, recording invalidations.

    Subclassed rather than mocked so the client is exercised against the real
    interface it depends on: a mock would keep passing if that interface changed.
    """

    def __init__(self, addresses: list[str]) -> None:
        super().__init__(fallback_url=addresses[0])
        self._addresses = addresses
        self.resolutions = 0
        self.invalidations = 0

    async def resolve(self, workspace_path: str) -> str:
        index = min(self.resolutions, len(self._addresses) - 1)
        self.resolutions += 1
        return self._addresses[index]

    def invalidate(self, workspace_path: str) -> None:
        self.invalidations += 1


def _client(router: StubRouter, handler) -> RuntimeClient:
    """Build a client whose transport is driven by ``handler``."""
    client = RuntimeClient(base_url=router.fallback_url, router=router)
    transport = httpx.MockTransport(handler)

    async def send(method, url, *, json=None, params=None, timeout):
        async with httpx.AsyncClient(transport=transport) as http:
            return await http.request(method, url, json=json, params=params)

    client._send = send  # noqa: SLF001 — the seam this test exists to drive
    return client


class TestLeaseAwareRouting:
    async def test_requests_go_to_the_lease_holder(self):
        router = StubRouter([HOLDER])
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        result = await _client(router, handler).post(
            "/api/v1/query", workspace_path=WORKSPACE, json={"sql": "SELECT 1"}
        )

        assert result == {"ok": True}
        # The pod address, not the load-balanced Service.
        assert seen == [f"{HOLDER}/api/v1/query"]

    async def test_lease_not_held_is_repaired_not_relayed(self):
        # The lease moved inside the route cache's window. Only the caller can
        # fix that; relaying the 409 would show a platform hand-over to the LLM
        # as a failure it cannot understand.
        router = StubRouter([NON_HOLDER, HOLDER])
        hits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            base = f"{request.url.scheme}://{request.url.netloc.decode()}"
            hits.append(base)
            if base == NON_HOLDER:
                return httpx.Response(409, json=LEASE_NOT_HELD_BODY)
            return httpx.Response(200, json={"rows": []})

        result = await _client(router, handler).get(
            "/api/v1/models", workspace_path=WORKSPACE
        )

        assert result == {"rows": []}
        assert hits == [NON_HOLDER, HOLDER]
        # The stale route must be dropped, or the next request repeats the miss.
        assert router.invalidations == 1

    async def test_repeated_lease_not_held_is_surfaced(self):
        # A second verdict is a genuine fault, not a hand-over in progress: more
        # attempts would turn a routing hiccup into a storm against Redis.
        router = StubRouter([NON_HOLDER])
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(409, json=LEASE_NOT_HELD_BODY)

        with pytest.raises(RuntimeUnavailable, match="409"):
            await _client(router, handler).get(
                "/api/v1/models", workspace_path=WORKSPACE
            )

        assert calls == 2  # the original plus exactly one re-resolution

    async def test_other_conflicts_are_not_retried(self):
        # 409 also covers conflicts that are not routing verdicts (a busy
        # environment). Retrying those would repeat a rejected operation.
        router = StubRouter([HOLDER])
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                409, json={"error_code": "ENVIRONMENT_BUSY", "message": "busy"}
            )

        with pytest.raises(RuntimeUnavailable):
            await _client(router, handler).post(
                "/api/v1/plan", workspace_path=WORKSPACE, json={}
            )

        assert calls == 1
        assert router.invalidations == 0


class TestPendingCopyVerdicts:
    @pytest.mark.parametrize(
        ("status", "code"),
        [(202, "WORKSPACE_MATERIALIZING"), (503, "WORKSPACE_RELOCATING")],
    )
    async def test_pending_copy_is_waited_out(self, status, code, monkeypatch):
        # Both mean the request was VALID and will succeed shortly, so surfacing
        # them would turn normal platform behaviour (a first clone, a rolling
        # restart) into an apparent error.
        monkeypatch.setattr(
            "src.tools.runtime_client.asyncio.sleep", _no_sleep
        )
        router = StubRouter([HOLDER])
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    status, json={"error_code": code}, headers={"Retry-After": "2"}
                )
            return httpx.Response(200, json={"content": "x"})

        result = await _client(router, handler).post(
            "/rt/v1/tools/read", workspace_path=WORKSPACE, json={}
        )

        assert result == {"content": "x"}
        assert calls == 2

    async def test_a_copy_that_never_becomes_ready_is_an_error(
        self, monkeypatch
    ):
        # Budget exhausted on a 202: the request never ran, so reporting success
        # with an empty body would make the agent act on data it does not have.
        monkeypatch.setattr(
            "src.tools.runtime_client.asyncio.sleep", _no_sleep
        )
        router = StubRouter([HOLDER])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202, json={"error_code": "WORKSPACE_MATERIALIZING"}
            )

        with pytest.raises(RuntimeUnavailable, match="still not ready"):
            await _client(router, handler).post(
                "/rt/v1/tools/read", workspace_path=WORKSPACE, json={}
            )


class TestUnconfiguredService:
    async def test_missing_runtime_url_fails_loudly(self):
        client = RuntimeClient(base_url="", router=StubRouter([""]))
        assert client.configured is False
        with pytest.raises(RuntimeUnavailable, match="not configured"):
            await client.get("/api/v1/models", workspace_path=WORKSPACE)


class TestNoToolBypassesTheLease:
    """Anti-regression: no tool may dial the runtime URL itself.

    This is the defect this whole module exists to fix. It reads perfectly well
    at the call site — ``httpx.post(f"{settings.runtime_service_url}/...")`` — and
    is wrong only because of a deployment fact (a headless Service) that is not
    visible in the code. A structural assertion is the only thing that stops the
    next tool from reintroducing it.
    """

    def test_no_tool_module_reads_the_runtime_url_directly(self):
        offenders = []
        for path in (Path(__file__).resolve().parents[1] / "src" / "tools").glob(
            "*.py"
        ):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "runtime_service_url"
                ):
                    offenders.append(path.name)
        # runtime_client.py is exempt: it is the one place that MAY know the
        # address, because it is the place that resolves the lease.
        assert [name for name in offenders if name != "runtime_client.py"] == [], (
            "these tools dial workspace-runtime directly, bypassing lease "
            f"routing: {sorted(set(offenders))}"
        )

    def test_tools_do_not_build_their_own_http_clients(self):
        # A tool with its own client cannot honour a lease verdict: the retry,
        # the invalidation and the wait all live in RuntimeClient.
        offenders = []
        for path in (Path(__file__).resolve().parents[1] / "src" / "tools").glob(
            "*.py"
        ):
            if path.name == "runtime_client.py":
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "AsyncClient"
                ):
                    offenders.append(path.name)
        assert offenders == [], (
            f"these tools construct their own HTTP client: {sorted(set(offenders))}"
        )


async def _no_sleep(_seconds: float) -> None:
    """Skip the contract's wait so the test suite does not pay it."""
    return None
