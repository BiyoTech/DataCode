"""Lease-aware env-control dispatch (architecture review P0-1).

In Kubernetes ``services.runtime`` is a headless Service and every replica has
its OWN volume. A call that lands on the wrong replica either fails the lease
gate or — worse — answers 200 with an honest report about an empty disk. These
tests pin the addressing contract of :class:`RuntimeClient` with REAL routing
code (``LeaseRouter`` over a fake Redis, real delivery table) and an ``httpx``
mock transport that plays several replicas:

* working-copy endpoints reach the lease holder, not the Service address;
* only endpoints that CREATE a copy may place a lease (a probe must never make
  a replica clone a repository just to report it absent);
* ``LEASE_NOT_HELD`` repairs the route exactly once;
* a capacity refusal propagates instead of degrading to stable-hash;
* fan-out endpoints visit every live replica;
* an unclassified path is refused before any request is sent.

Plus the structural guards that keep the discipline from eroding: the delivery
table must cover every env-control route the runtime defines and every literal
the control plane references, and nothing outside ``src/runtime/client.py`` may
dial the runtime directly.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.lease import lease_redis_key  # noqa: E402
from datacode_common.lease_routing import (  # noqa: E402
    LeaseCapacityRefused,
    LeaseRouter,
)

from src.runtime.client import RuntimeClient, RuntimeTarget  # noqa: E402
from src.runtime.fleet import FleetView  # noqa: E402
from src.runtime.routing import (  # noqa: E402
    ENV_CONTROL_DELIVERY,
    UnclassifiedEndpointError,
    delivery_of,
)
from src.utils.errors import RuntimeUnavailableError  # noqa: E402

_SERVICE = "http://runtime:8002"
_A = "http://replica-a:8002"
_B = "http://replica-b:8002"
_COPY = RuntimeTarget(
    tenant_id="t1", workspace_id="ws-t1-shop", user_id="u1", environment="prod"
)
_KEY = _COPY.lease_key()
_WS = RuntimeTarget(tenant_id="t1", workspace_id="ws-t1-shop")

_CONTROL_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_ENV_CONTROL = (
    _CONTROL_ROOT.parent / "workspace-runtime" / "src" / "api" / "rest" / "env_control.py"
)


# ── doubles ────────────────────────────────────────────────────────────────


class _FakeRedis:
    """Only what LeaseRouter reads: GET lease:{key} and the replica index."""

    def __init__(self, records: dict[str, str] | None = None):
        self.records = dict(records or {})

    async def get(self, key: str):
        return self.records.get(key)

    async def smembers(self, key: str):
        return set()


def _holder(addr: str) -> str:
    host = addr.removeprefix("http://")
    return json.dumps(
        {"runtime_id": "rt", "runtime_addr": host, "token": "tok", "acquired_at": 0}
    )


class _Placement:
    """Records placements; answers with a fixed winner or a capacity refusal."""

    def __init__(self, winner: str = _A, refuse: bool = False):
        self.winner = winner
        self.refuse = refuse
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def acquire(self, key: str) -> str:
        self.calls.append(key)
        if self.refuse:
            raise LeaseCapacityRefused("full", retry_after="5")
        return self.winner


class _Fleet:
    def __init__(self, addrs=(), leases=None, known=True):
        self._view = FleetView(
            known=known, addrs=tuple(addrs), leases_by_addr=dict(leases or {})
        )
        self.invalidations = 0

    async def view(self) -> FleetView:
        return self._view

    def invalidate(self) -> None:
        self.invalidations += 1


class _Replicas:
    """Plays several runtime replicas behind one mock transport."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
        self.answers: dict[str, httpx.Response] = {}
        self.queue: dict[str, list[httpx.Response]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        base = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
        self.calls.append((request.method, base, request.url.path))
        queued = self.queue.get(base)
        if queued:
            return queued.pop(0)
        return self.answers.get(base, httpx.Response(200, json={"ok": True}))

    def hosts(self) -> list[str]:
        return [base for _, base, _ in self.calls]


def _client(replicas, *, redis=None, placement=None, fleet=None) -> RuntimeClient:
    async def _redis():
        return redis

    router = LeaseRouter(
        fallback_url=_SERVICE, placement=placement, redis_factory=_redis
    )
    return RuntimeClient(
        base_url=_SERVICE,
        transport=httpx.MockTransport(replicas.handler),
        router=router,
        fleet=fleet or _Fleet(known=False),
    )


# ── working copies reach their holder ──────────────────────────────────────


async def test_copy_endpoint_reaches_the_lease_holder_not_the_service():
    """The failure this whole change exists for: DNS picks a replica at random."""
    replicas = _Replicas()
    redis = _FakeRedis({lease_redis_key(_KEY): _holder(_B)})
    client = _client(replicas, redis=redis)

    await client.post("/internal/env-control/workspace/scaffold", {}, target=_COPY)

    assert replicas.hosts() == [_B]


async def test_creating_endpoint_places_a_lease_on_a_clean_miss():
    """Scaffold must land where the copy will be served afterwards."""
    replicas = _Replicas()
    placement = _Placement(winner=_A)
    client = _client(replicas, redis=_FakeRedis(), placement=placement)

    await client.post("/internal/env-control/git/ensure-repository", {}, target=_COPY)
    await client.post("/internal/env-control/workspace/scaffold", {}, target=_COPY)

    assert placement.calls == [_KEY]
    # The scaffold followed the placement through the shared route cache.
    assert replicas.hosts() == [_A, _A]


async def test_probe_never_places_a_lease():
    """A probe placing a lease would clone a repo to report it was absent."""
    replicas = _Replicas()
    placement = _Placement(winner=_A)
    client = _client(replicas, redis=_FakeRedis(), placement=placement)

    await client.get_all(
        "/internal/env-control/git/pending-work", {"branch": "main"}, target=_WS
    )

    assert placement.calls == []


async def test_stale_route_is_repaired_once():
    """Ownership moves between the refusal and the retry, as in a hand-over."""
    replicas = _Replicas()
    redis = _FakeRedis({lease_redis_key(_KEY): _holder(_A)})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "replica-a":
            redis.records[lease_redis_key(_KEY)] = _holder(_B)
            replicas.calls.append((request.method, _A, request.url.path))
            return httpx.Response(
                409, json={"error_code": "LEASE_NOT_HELD", "message": "moved"}
            )
        return replicas.handler(request)

    client = _client(replicas, redis=redis)
    client._transport = httpx.MockTransport(handler)

    body = await client.post(
        "/internal/env-control/workspace/scaffold", {}, target=_COPY
    )

    assert body == {"ok": True}
    assert replicas.hosts() == [_A, _B]


async def test_route_repair_budget_is_one():
    """A second refusal means the lease subsystem itself is degraded.

    The holder is asked twice (initial + one repair); after that the request
    falls through to the remaining candidates, whose own lease gate decides.
    """
    replicas = _Replicas()
    redis = _FakeRedis({lease_redis_key(_KEY): _holder(_A)})
    replicas.answers[_A] = httpx.Response(
        409, json={"error_code": "LEASE_NOT_HELD", "message": "not here"}
    )
    replicas.answers[_SERVICE] = httpx.Response(
        409, json={"error_code": "LEASE_NOT_HELD", "message": "not here either"}
    )
    client = _client(replicas, redis=redis)

    with pytest.raises(RuntimeUnavailableError):
        await client.post(
            "/internal/env-control/workspace/scaffold", {}, target=_COPY
        )
    assert replicas.hosts() == [_A, _A, _SERVICE]


async def test_capacity_refusal_propagates_instead_of_degrading():
    """Degrading would route around the capacity decision placement just made."""
    replicas = _Replicas()
    client = _client(
        replicas, redis=_FakeRedis(), placement=_Placement(refuse=True)
    )

    with pytest.raises(RuntimeUnavailableError) as excinfo:
        await client.post(
            "/internal/env-control/git/ensure-worktree", {}, target=_COPY
        )

    assert "capacity" in str(excinfo.value.detail or excinfo.value.message)
    assert replicas.calls == []


async def test_probe_asks_every_replica_not_just_the_first_answer():
    """A replica without the copy answers 200 with zeros, not an error.

    A "first answer wins" walk stops there; when nobody holds the lease that
    first replica is chosen by stable hash, not by where the files are.
    """
    replicas = _Replicas()
    replicas.answers[_A] = httpx.Response(200, json={"uncommitted": 0, "unpushed": 0})
    replicas.answers[_B] = httpx.Response(200, json={"uncommitted": 3, "unpushed": 1})
    client = _client(replicas, fleet=_Fleet(addrs=(_A, _B)))

    replies = await client.get_all(
        "/internal/env-control/git/pending-work", {}, target=_COPY
    )

    assert sorted(r.addr for r in replies) == [_A, _B]
    assert sum(r.body["uncommitted"] for r in replies) == 3


async def test_mutation_walk_moves_past_a_replica_without_the_copy():
    """Mutations answer 404 where there is no copy, so a walk is correct."""
    replicas = _Replicas()
    replicas.answers[_A] = httpx.Response(
        404, json={"error_code": "WORKSPACE_NOT_FOUND", "message": "no copy"}
    )
    replicas.answers[_B] = httpx.Response(200, json={"ok": True})
    client = _client(replicas, fleet=_Fleet(addrs=(_A, _B)))

    body = await client.post(
        "/internal/env-control/sqlmesh/invalidate-virtual-environment",
        {},
        target=_WS,
    )

    assert body == {"ok": True}
    assert replicas.hosts() == [_A, _B]


async def test_creating_endpoint_never_tries_a_second_writer():
    """After placement the winner is the single legitimate writer."""
    replicas = _Replicas()
    replicas.answers[_A] = httpx.Response(
        502, json={"error_code": "GIT_OPERATION_FAILED", "message": "boom"}
    )
    client = _client(
        replicas,
        redis=_FakeRedis(),
        placement=_Placement(winner=_A),
        fleet=_Fleet(addrs=(_A, _B)),
    )

    with pytest.raises(Exception):
        await client.post(
            "/internal/env-control/git/ensure-worktree", {}, target=_COPY
        )

    assert replicas.hosts() == [_A]


# ── fan-out and non-routed endpoints ───────────────────────────────────────


async def test_union_endpoint_visits_every_replica_holders_first():
    replicas = _Replicas()
    fleet = _Fleet(addrs=(_A, _B), leases={_B: (_KEY,)})
    client = _client(replicas, fleet=fleet)

    replies = await client.get_all(
        "/internal/env-control/git/members", {}, target=_WS
    )

    assert [reply.addr for reply in replies] == [_B, _A]
    assert all(reply.ok for reply in replies)


async def test_fleet_wide_endpoint_needs_no_target():
    replicas = _Replicas()
    client = _client(replicas, fleet=_Fleet(addrs=(_A, _B)))

    await client.delete_all("/api/v1/dagster/deployments/ws-t1-shop__prod")

    assert sorted(replicas.hosts()) == [_A, _B]


async def test_remote_only_endpoint_uses_the_service_address():
    replicas = _Replicas()
    redis = _FakeRedis({lease_redis_key(_KEY): _holder(_B)})
    client = _client(replicas, redis=redis)

    await client.post("/internal/env-control/workspace/provision-remote", {})

    assert replicas.hosts() == [_SERVICE]


async def test_routed_endpoint_without_target_is_refused():
    replicas = _Replicas()
    client = _client(replicas)

    with pytest.raises(RuntimeUnavailableError):
        await client.post("/internal/env-control/workspace/scaffold", {})
    assert replicas.calls == []


async def test_union_endpoint_through_single_surface_is_refused():
    """A single-replica read of a union returns one replica's slice."""
    replicas = _Replicas()
    client = _client(replicas)

    with pytest.raises(RuntimeUnavailableError):
        await client.get("/internal/env-control/git/members", target=_WS)
    assert replicas.calls == []


async def test_unclassified_endpoint_is_refused_before_any_request():
    replicas = _Replicas()
    client = _client(replicas)

    with pytest.raises(UnclassifiedEndpointError):
        await client.post("/internal/env-control/git/brand-new", {}, target=_COPY)
    assert replicas.calls == []


def test_delivery_ignores_query_strings():
    assert delivery_of("/internal/env-control/git/pending-work?x=1") is (
        ENV_CONTROL_DELIVERY["/internal/env-control/git/pending-work"]
    )


def test_only_creating_endpoints_may_place_a_lease():
    """Placement clones a repository; only two endpoints are entitled to that."""
    placing = sorted(
        path for path, delivery in ENV_CONTROL_DELIVERY.items() if delivery.place_lease
    )
    assert placing == [
        "/internal/env-control/git/ensure-repository",
        "/internal/env-control/git/ensure-worktree",
    ]


# ── structural guards ──────────────────────────────────────────────────────


def _runtime_env_control_routes() -> set[str]:
    source = _RUNTIME_ENV_CONTROL.read_text(encoding="utf-8")
    prefix = re.search(r'APIRouter\(prefix="([^"]+)"', source).group(1)
    return {
        prefix + match
        for match in re.findall(r'@router\.(?:get|post|put|delete)\("([^"]+)"', source)
    }


def test_delivery_table_covers_every_runtime_env_control_route():
    """A new runtime endpoint cannot be dispatched before it is classified."""
    routes = _runtime_env_control_routes()
    declared = {p for p in ENV_CONTROL_DELIVERY if p.startswith("/internal/env-control/")}
    assert routes, "failed to parse the runtime env-control router"
    assert routes - declared == set(), "unclassified runtime env-control routes"
    assert declared - routes == set(), "delivery table names routes that do not exist"


def _string_constants(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _control_modules():
    for path in sorted((_CONTROL_ROOT / "src").rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_every_env_control_literal_in_control_is_classified():
    for path, tree in _control_modules():
        for value in _string_constants(tree):
            # The bare prefix is a label, not a dispatchable path.
            if value.startswith("/internal/env-control/") and not value.endswith("/"):
                delivery_of(value)  # raises UnclassifiedEndpointError


# Modules allowed to open their own HTTP clients. The runtime client is the
# single hop to workspace-runtime; the lease controller's drain call is
# addressed at ONE named replica by design (it is the victim being drained).
_HTTP_ALLOWED = {
    "src/runtime/client.py",
    "src/capacity/lease_controller.py",
}


def test_nothing_outside_the_runtime_client_dials_the_runtime():
    """The discipline that lets the delivery table be the only routing policy."""
    offenders: list[str] = []
    for path, tree in _control_modules():
        rel = path.relative_to(_CONTROL_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "runtime_rest_url":
                if rel not in {"src/runtime/client.py", "src/config.py"}:
                    offenders.append(f"{rel}: reads runtime_rest_url")
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "AsyncClient"
                and isinstance(node.value, ast.Name)
                and node.value.id == "httpx"
                and rel not in _HTTP_ALLOWED
            ):
                offenders.append(f"{rel}: constructs httpx.AsyncClient")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "RuntimeClient"
                and rel != "src/runtime/client.py"
            ):
                offenders.append(f"{rel}: constructs its own RuntimeClient")
    assert offenders == []
