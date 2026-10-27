"""Lease contract tests (design 03 §3.3, design 05 §5.2, P4a).

Pins the vocabulary the three parties share (control grants, runtime holds,
gateway routes). The cases worth having here are the ones where a plausible
alternative reading of the contract would silently break the single-writer
invariant or abort running work:

* a draining holder must stay routable, or graceful shutdown aborts applies;
* TTL is a crash-recovery bound, so renewal must be strictly more frequent;
* keys must be rejected rather than coerced, or two working copies can
  collapse onto one lease;
* timestamps must be tz-aware, since a naive one compared against UTC shifts
  expiry by the deployment's offset.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from datacode_common.lease import (
    LEASE_RENEW_DIVISOR,
    LEASE_TTL_SECONDS,
    Lease,
    LeaseState,
    RuntimeHeartbeat,
    decode_runtime_heartbeat,
    encode_runtime_heartbeat,
    renew_interval_seconds,
)
from datacode_common.naming import (
    LeaseKeyError,
    lease_key,
    parse_working_copy,
    split_lease_key,
)

_KEY = "t1/ws-t1-shop/u1/dev"

# Shared with shared/go/lease/key_test.go: both languages assert against the
# same expectations rather than against each other's source.
_VECTORS_PATH = Path(__file__).resolve().parents[2] / "lease-vectors.json"


def _vectors() -> dict:
    return json.loads(_VECTORS_PATH.read_text())


class TestCrossLanguageVectors:
    """The Go gateway and the Python runtime must agree byte for byte.

    A divergence does not raise anywhere: it produces a Redis miss, the gateway
    requests a new lease, and a working copy already mounted elsewhere gets
    mounted a second time -- precisely the corruption the lease prevents.
    """

    def test_vectors_file_is_present_and_populated(self):
        vectors = _vectors()
        assert vectors["valid_keys"], "the cross-language check would be vacuous"
        assert vectors["invalid_keys"]

    def test_valid_keys_render_and_parse_identically(self):
        for case in _vectors()["valid_keys"]:
            built = lease_key(
                case["tenant_id"],
                case["workspace_id"],
                case["user_id"],
                case["environment"],
            )
            assert built == case["key"]
            assert split_lease_key(case["key"]) == (
                case["tenant_id"],
                case["workspace_id"],
                case["user_id"],
                case["environment"],
            )

    def test_invalid_keys_are_rejected(self):
        for case in _vectors()["invalid_keys"]:
            with pytest.raises(LeaseKeyError):
                split_lease_key(case["key"])

    def test_working_copy_paths_derive_the_same_key(self, tmp_path):
        for case in _vectors()["working_copy_paths"]:
            # parse_working_copy resolves against a base, so reconstruct the
            # layout under tmp_path while keeping the vector's tail segments.
            tenant, workspace, user, environment = case["key"].split("/")
            path = tmp_path / tenant / workspace / user / "envs" / environment
            path.mkdir(parents=True, exist_ok=True)
            assert parse_working_copy(str(tmp_path), str(path)).lease_key == case["key"]

    def test_invalid_working_copy_paths_are_rejected(self, tmp_path):
        from datacode_common.naming import WorkingCopyLayoutError

        for case in _vectors()["invalid_working_copy_paths"]:
            if not case["path"]:
                continue
            # Re-root the vector's path under tmp_path so the base check is not
            # what rejects it -- the LAYOUT must be what fails.
            relative = case["path"].lstrip("/")
            with pytest.raises((WorkingCopyLayoutError, LeaseKeyError, ValueError)):
                parse_working_copy(str(tmp_path), str(tmp_path / relative))


def _lease(**overrides) -> Lease:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    defaults = {
        "lease_key": _KEY,
        "runtime_id": "rt-0",
        "runtime_addr": "10.1.2.3:8002",
        "state": LeaseState.HELD,
        "acquired_at": now,
        "expires_at": now + timedelta(seconds=LEASE_TTL_SECONDS),
    }
    return Lease(**{**defaults, **overrides})


class TestLeaseKeyAlgebra:
    def test_round_trip(self):
        key = lease_key("t1", "ws-t1-shop", "u1", "dev")
        assert key == _KEY
        assert split_lease_key(key) == ("t1", "ws-t1-shop", "u1", "dev")

    def test_environment_is_part_of_the_key(self):
        # Two environments of one user are independent working copies and must
        # not serialize onto a single lease.
        a = lease_key("t1", "ws-t1-shop", "u1", "dev")
        b = lease_key("t1", "ws-t1-shop", "u1", "staging")
        assert a != b

    @pytest.mark.parametrize(
        "args",
        [
            ("", "ws-t1-shop", "u1", "dev"),
            ("t1", "", "u1", "dev"),
            ("t1", "ws-t1-shop", "", "dev"),
            ("t1", "ws-t1-shop", "u1", ""),
        ],
    )
    def test_empty_segment_is_rejected(self, args):
        with pytest.raises(LeaseKeyError):
            lease_key(*args)

    def test_separator_in_segment_is_rejected(self):
        # Otherwise "t1/ws/u1" + "/dev" could be re-parsed as a different tuple.
        with pytest.raises(LeaseKeyError, match="must not contain"):
            lease_key("t1", "ws-t1-shop", "u1/x", "dev")

    def test_invalid_environment_is_rejected(self):
        with pytest.raises(LeaseKeyError, match="invalid environment"):
            lease_key("t1", "ws-t1-shop", "u1", "Dev")

    @pytest.mark.parametrize(
        "value", ["", "t1/ws/u1", "t1/ws/u1/dev/extra", "t1//u1/dev"]
    )
    def test_split_rejects_malformed_keys(self, value):
        # Coercing instead of raising would let two working copies share one
        # lease, which is exactly the invariant the lease exists to hold.
        with pytest.raises(LeaseKeyError):
            split_lease_key(value)

    def test_working_copy_exposes_its_lease_key(self, tmp_path):
        from datacode_common.naming import parse_working_copy

        path = tmp_path / "t1" / "ws-t1-shop" / "u1" / "envs" / "dev"
        path.mkdir(parents=True)
        copy = parse_working_copy(str(tmp_path), str(path))
        assert copy.lease_key == _KEY


class TestTTLSemantics:
    def test_renewal_is_more_frequent_than_expiry(self):
        # A holder renewing once per TTL would lose its lease to one lost
        # packet; the divisor buys two consecutive failures.
        assert renew_interval_seconds(120) == 40
        assert LEASE_RENEW_DIVISOR >= 3

    def test_renew_pushes_expiry_without_moving_acquisition(self):
        lease = _lease()
        later = lease.acquired_at + timedelta(seconds=100)
        renewed = lease.renewed(now=later)
        assert renewed.acquired_at == lease.acquired_at
        assert renewed.expires_at == later + timedelta(seconds=LEASE_TTL_SECONDS)

    def test_seconds_until_expiry_goes_negative_when_stale(self):
        lease = _lease()
        past_due = lease.expires_at + timedelta(seconds=5)
        assert lease.seconds_until_expiry(now=past_due) == -5

    def test_non_positive_ttl_is_rejected(self):
        with pytest.raises(ValueError, match="must be positive"):
            renew_interval_seconds(0)


class TestRoutability:
    def test_held_is_routable(self):
        assert _lease(state=LeaseState.HELD).is_routable is True

    def test_draining_stays_routable(self):
        # The draining holder still OWNS the working copy: routing elsewhere
        # would create a second writer, and refusing to route would abort the
        # in-flight apply that draining is meant to let finish.
        assert _lease(state=LeaseState.DRAINING).is_routable is True

    def test_released_is_not_routable(self):
        assert _lease(state=LeaseState.RELEASED).is_routable is False


class TestNaiveTimestampsRejected:
    @pytest.mark.parametrize("field_name", ["acquired_at", "expires_at"])
    def test_lease_rejects_naive_datetime(self, field_name):
        with pytest.raises(ValueError, match="timezone-aware"):
            _lease(**{field_name: datetime(2026, 8, 27, 12, 0)})  # noqa: DTZ001

    def test_heartbeat_rejects_naive_datetime(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            RuntimeHeartbeat(
                runtime_id="rt-0",
                addr="10.1.2.3:8002",
                lease_count=0,
                max_leases=16,
                memory_used_ratio=0.1,
                reported_at=datetime(2026, 8, 27, 12, 0),  # noqa: DTZ001
            )

    def test_malformed_lease_key_is_rejected_at_construction(self):
        with pytest.raises(LeaseKeyError):
            _lease(lease_key="not-a-key")


class TestCapacity:
    def _hb(self, **overrides) -> RuntimeHeartbeat:
        defaults = {
            "runtime_id": "rt-0",
            "addr": "10.1.2.3:8002",
            "lease_count": 4,
            "max_leases": 16,
            "memory_used_ratio": 0.4,
            "reported_at": datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        }
        return RuntimeHeartbeat(**{**defaults, **overrides})

    def test_has_capacity_when_under_both_limits(self):
        assert self._hb().has_capacity() is True

    def test_count_limit_blocks_placement(self):
        assert self._hb(lease_count=16).has_capacity() is False

    def test_memory_watermark_blocks_placement_below_the_count_limit(self):
        # The whole point of Q3: an underestimated per-lease cost must surface
        # as a refused lease, not as an OOM.
        assert self._hb(lease_count=2, memory_used_ratio=0.85).has_capacity() is False

    def test_draining_instance_never_accepts_new_leases(self):
        assert self._hb(draining=True).has_capacity() is False

    def test_zero_max_leases_means_memory_only_admission(self):
        # `max_per_instance: 0` documents "derive capacity from the memory
        # budget alone"; treating it as "capacity zero" would idle the pod.
        assert self._hb(max_leases=0, lease_count=99).has_capacity() is True


class TestHeartbeatWireFormat:
    """The JSON shape is a contract with independent readers (P8).

    Placement (runtime, request path) and the LeaseController (control,
    capacity loop) both decode it; a decoder that accepted a slightly wrong
    shape would silently blank instances out of placement or undercount
    capacity rather than raise.
    """

    def _hb(self, **overrides) -> RuntimeHeartbeat:
        defaults = {
            "runtime_id": "rt-0",
            "addr": "10.1.2.3:8002",
            "lease_count": 4,
            "max_leases": 16,
            "memory_used_ratio": 0.4,
            "reported_at": datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
            "draining": True,
            "leases": ("t1/ws-a/u1/dev", "t1/ws-b/u2/prod"),
        }
        return RuntimeHeartbeat(**{**defaults, **overrides})

    def test_round_trip_preserves_every_field(self):
        original = self._hb()
        decoded = decode_runtime_heartbeat(encode_runtime_heartbeat(original))
        assert decoded == original

    def test_decode_ignores_unknown_fields_from_newer_writers(self):
        payload = json.loads(encode_runtime_heartbeat(self._hb()))
        payload["future_field"] = {"anything": True}
        assert decode_runtime_heartbeat(json.dumps(payload)) == self._hb()

    def test_decode_defaults_optional_fields(self):
        # draining/leases arrived after the first deployment of the format;
        # records without them must decode, not raise.
        raw = json.dumps(
            {
                "runtime_id": "rt-0",
                "addr": "10.1.2.3:8002",
                "lease_count": 1,
                "max_leases": 8,
                "memory_used_ratio": 0.2,
                "reported_at": "2026-08-27T12:00:00+00:00",
            }
        )
        hb = decode_runtime_heartbeat(raw)
        assert hb is not None
        assert hb.draining is False
        assert hb.leases == ()

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not json",
            "{}",
            json.dumps({"runtime_id": "rt-0"}),  # required fields missing
            json.dumps(
                {
                    "runtime_id": "rt-0",
                    "addr": "a",
                    "lease_count": "many",  # wrong type
                    "max_leases": 8,
                    "memory_used_ratio": 0.2,
                    "reported_at": "2026-08-27T12:00:00+00:00",
                }
            ),
        ],
    )
    def test_undecodable_input_returns_none(self, raw):
        assert decode_runtime_heartbeat(raw) is None


class TestRedisKeyLayout:
    def test_keys_are_cell_scoped(self, monkeypatch):
        from datacode_common.lease import (
            lease_redis_key,
            runtime_index_redis_key,
            runtime_redis_key,
        )

        monkeypatch.setenv("DATACODE_CELL_ID", "cell-7")
        # One Redis may back several cells; without the cell segment a lease in
        # cell-0 and one in cell-7 would collide on the same key.
        assert ":cell-7:" in lease_redis_key(_KEY)
        assert lease_redis_key(_KEY).endswith(f"lease:{_KEY}")
        assert runtime_redis_key("rt-0").endswith("rt:rt-0")
        assert runtime_index_redis_key().endswith("rt:index")

    def test_lease_redis_key_validates_its_input(self):
        from datacode_common.lease import lease_redis_key

        with pytest.raises(LeaseKeyError):
            lease_redis_key("garbage")

    def test_runtime_key_requires_an_id(self):
        from datacode_common.lease import runtime_redis_key

        with pytest.raises(ValueError, match="must not be empty"):
            runtime_redis_key("")
