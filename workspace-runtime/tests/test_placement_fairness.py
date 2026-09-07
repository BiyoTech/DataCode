"""Placement fairness tests (design 03 §6 — weighted tenant lease share).

The scenario worth pinning is the one the design names: a big tenant's batch
operation acquires lease after lease, and every "emptiest instance" race is
won by the same burst. Without the fairness term, a small tenant's single
acquire then lands on whatever the burst left behind — usually the most loaded
box. With it, the burst spreads itself first.

Also pinned: the feature's OFF state. A share of 0/absent/unreadable must
reproduce the old spread-only ordering exactly, because enabling fairness can
never make placement worse than that baseline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.lease.placement import NoInstanceAvailable, pick_instance


class FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}

    async def smembers(self, key):
        return set(self._sets.get(key, set()))

    async def get(self, key):
        return self._values.get(key)

    # Test helpers -------------------------------------------------------

    def heartbeat(
        self,
        runtime_id: str,
        *,
        lease_count: int,
        leases: list[str] | None = None,
        memory: float = 0.2,
        max_leases: int = 10,
    ) -> None:
        from datacode_common.lease import (
            runtime_index_redis_key,
            runtime_redis_key,
        )

        self._values[runtime_redis_key(runtime_id)] = json.dumps(
            {
                "runtime_id": runtime_id,
                "addr": f"{runtime_id}:8002",
                "lease_count": lease_count,
                "max_leases": max_leases,
                "memory_used_ratio": memory,
                "reported_at": datetime.now(timezone.utc).isoformat(),
                "leases": leases or [],
            }
        )
        self._sets.setdefault(runtime_index_redis_key(), set()).add(runtime_id)

    def quota(self, share: int | None, *, defaults: bool = False) -> None:
        from datacode_common.lease import (
            quota_defaults_redis_key,
            tenant_quota_redis_key,
        )

        key = (
            quota_defaults_redis_key()
            if defaults
            else tenant_quota_redis_key("t-big")
        )
        self._values[key] = json.dumps(
            {
                "max_concurrent_leases": 64,
                "max_runtime_replicas": 4,
                "tenant_lease_share": share,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )


async def test_a_bursting_tenants_next_lease_avoids_its_own_footprint():
    """The big tenant holds 3 of 4 leases on instance A, 0 on instance B.

    Even though B carries MORE leases in total (4 vs 4 — say 5 vs 4), the
    burst's next acquire must prefer B: A is "full" from THIS tenant's point of
    view, and the small tenants on B are not the bursting tenant's problem to
    displace.
    """
    client = FakeRedis()
    client.heartbeat(
        "rt-a", lease_count=5, leases=["t-big/w/u/e"] * 3 + ["t-other/w/u/e"] * 2
    )
    client.heartbeat("rt-b", lease_count=4, leases=["t-other/w/u/e"] * 4)
    client.quota(share=2)

    placement = await pick_instance(client, tenant_id="t-big")

    assert placement.runtime_id == "rt-b"


async def test_zero_share_reproduces_the_spread_only_ordering():
    """share=0 means no fairness term: plain (lease_count, memory) wins.

    The OFF state is part of the contract: enabling fairness must never make
    placement worse than the baseline, and disabling it must return exactly
    the baseline.
    """
    client = FakeRedis()
    client.heartbeat(
        "rt-a", lease_count=5, leases=["t-big/w/u/e"] * 5, memory=0.2
    )
    client.heartbeat("rt-b", lease_count=6, leases=["t-other/w/u/e"] * 6, memory=0.2)
    client.quota(share=0)

    placement = await pick_instance(client, tenant_id="t-big")

    # rt-a has fewer leases overall; spread-only picks it regardless of who
    # holds them.
    assert placement.runtime_id == "rt-a"


async def test_absent_share_defaults_to_spread_only():
    client = FakeRedis()
    client.heartbeat("rt-a", lease_count=3, leases=["t-big/w/u/e"] * 3)
    client.heartbeat("rt-b", lease_count=4, leases=["t-other/w/u/e"] * 4)
    # No quota keys at all.

    placement = await pick_instance(client, tenant_id="t-big")

    assert placement.runtime_id == "rt-a"


async def test_defaults_key_supplies_the_share_when_no_override_exists():
    client = FakeRedis()
    client.heartbeat("rt-a", lease_count=3, leases=["t-big/w/u/e"] * 3)
    client.heartbeat("rt-b", lease_count=4, leases=["t-other/w/u/e"] * 4)
    client.quota(share=2, defaults=True)  # only the platform defaults key

    placement = await pick_instance(client, tenant_id="t-big")

    assert placement.runtime_id == "rt-b"


async def test_pressure_decays_once_the_share_is_not_saturated():
    """Below the share, the spreading term decides again.

    A share of 4 means the tenant may put up to 4 leases on one instance
    without it starting to look "full"; at 2/4 on A vs 0/4 on B, A is under
    its allowance, but B is strictly freer FOR THIS TENANT, so B still wins —
    while at equal pressure the emptier instance wins, which is what the
    spreading term contributes.
    """
    client = FakeRedis()
    # Both instances carry 2 of the tenant's leases: equal pressure.
    client.heartbeat("rt-a", lease_count=2, leases=["t-big/w/u/e"] * 2)
    client.heartbeat("rt-b", lease_count=6, leases=["t-big/w/u/e"] * 2 + ["t-x/w/u/e"] * 4)
    client.quota(share=4)

    placement = await pick_instance(client, tenant_id="t-big")

    # Equal pressure → lease_count breaks the tie → the emptier instance.
    assert placement.runtime_id == "rt-a"


async def test_unknown_tenant_falls_back_to_spreading():
    client = FakeRedis()
    client.heartbeat("rt-a", lease_count=3, leases=["t-other/w/u/e"] * 3)
    client.heartbeat("rt-b", lease_count=4, leases=["t-x/w/u/e"] * 4)
    client.quota(share=2)

    placement = await pick_instance(client, tenant_id="")

    assert placement.runtime_id == "rt-a"


async def test_unreadable_quota_reads_do_not_break_placement():
    """Fairness is an optimization: its input failing must not 500 the acquire."""

    class BrokenQuotaRedis(FakeRedis):
        async def get(self, key):
            if "quota" in key:
                raise ConnectionError("quota key unreadable")
            return self._values.get(key)

    client = BrokenQuotaRedis()
    client.heartbeat("rt-a", lease_count=3, leases=["t-big/w/u/e"] * 3)
    client.heartbeat("rt-b", lease_count=4, leases=["t-other/w/u/e"] * 4)

    placement = await pick_instance(client, tenant_id="t-big")

    # Spread-only fallback: the emptier instance wins.
    assert placement.runtime_id == "rt-a"


async def test_full_instances_remain_unavailable():
    client = FakeRedis()
    client.heartbeat("rt-a", lease_count=10, max_leases=10)

    with pytest.raises(NoInstanceAvailable):
        await pick_instance(client, tenant_id="t-big")
