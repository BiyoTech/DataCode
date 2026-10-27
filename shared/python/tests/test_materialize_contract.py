"""Storage contract tests (design 07 ADR-003 / Q7).

These cover the two combinations whose real-world failure mode is SILENT data
loss, which is the reason the contract is enforced at startup at all:

* a shared medium under lease routing (a second replica writes behind the
  lease's back);
* a replica-private medium without lease routing (replicas serve the same
  workspace from different directories, so writes diverge).

The happy paths are asserted too, so that tightening the rule later cannot
accidentally make a valid production deployment unbootable.
"""

from __future__ import annotations

import pytest

from datacode_common.materialize import (
    MountContractError,
    MountKind,
    resolve_mount_kind,
    validate_storage_contract,
)


class TestResolveMountKind:
    def test_accepts_known_values(self):
        assert resolve_mount_kind("ephemeral") is MountKind.EPHEMERAL
        assert resolve_mount_kind("instance_local") is MountKind.INSTANCE_LOCAL
        assert resolve_mount_kind("shared") is MountKind.SHARED

    def test_normalizes_case_and_padding(self):
        assert resolve_mount_kind("  Instance_Local  ") is MountKind.INSTANCE_LOCAL

    def test_passes_through_enum(self):
        assert resolve_mount_kind(MountKind.EPHEMERAL) is MountKind.EPHEMERAL

    def test_rejects_unknown_value_instead_of_defaulting(self):
        # Guessing on the operator's behalf would reintroduce the very
        # silent-corruption path the declaration exists to prevent.
        with pytest.raises(MountContractError, match="unknown materialize.mount_kind"):
            resolve_mount_kind("local")

    def test_rejects_empty_value(self):
        with pytest.raises(MountContractError, match="is not set"):
            resolve_mount_kind("")


class TestSharedMediumUnderLease:
    """`shared` + lease depends on WHICH env lock backs the degradation path.

    With the Redis lock held this is the P4a transitional shape (design 06 4a:
    leases route the happy path on the original shared PV while the Redis env
    lock still serializes the degradation path) — allowed, with a warning that
    4b moves on. With only in-process locks it is WORSE than 1.x: the lease
    justifies degrading env locks to in-process, and the shared medium then
    admits a second writer with nothing serializing it."""

    def test_shared_with_lease_and_redis_lock_is_the_p4a_transition(self):
        contract = validate_storage_contract(
            root="/var/lib/datacode/workcopies",
            mount_kind="shared",
            lease_enabled=True,
            distributed_env_lock=True,
        )
        assert contract.lease_enabled is True
        # Not silently: the warning names the destination shape.
        assert any("P4a transitional" in w for w in contract.warnings)

    def test_shared_with_lease_and_in_process_lock_is_fatal(self):
        with pytest.raises(MountContractError) as exc:
            validate_storage_contract(
                root="/var/lib/datacode/workcopies",
                mount_kind="shared",
                lease_enabled=True,
                distributed_env_lock=False,
            )
        message = str(exc.value)
        # The error must name the fix, not just the fault.
        assert "instance_local" in message
        assert "behind the lease" in message

    def test_shared_without_lease_is_allowed_with_distributed_lock(self):
        # The pre-P4 status quo: shared volume, no lease, Redis env lock.
        contract = validate_storage_contract(
            root="/workspaces",
            mount_kind="shared",
            lease_enabled=False,
            distributed_env_lock=True,
        )
        assert contract.mount_kind is MountKind.SHARED
        assert contract.mount_kind.is_replica_private() is False
        assert contract.warnings == ()

    def test_shared_without_distributed_lock_warns_but_boots(self):
        # This is the existing 1.x behaviour. It degrades (Git's worktree locks
        # still refuse a double checkout) rather than corrupting silently, so
        # failing startup here would break running deployments instead of
        # protecting them.
        contract = validate_storage_contract(
            root="/workspaces",
            mount_kind="shared",
            lease_enabled=False,
            distributed_env_lock=False,
        )
        assert len(contract.warnings) == 1
        assert "env_lock_backend" in contract.warnings[0]
        assert contract.describe()["warnings"] == list(contract.warnings)


class TestPrivateMediumRequiresLease:
    """A private medium without lease routing loses writes: two replicas each
    materialize their own copy of the same workspace."""

    @pytest.mark.parametrize("kind", ["ephemeral", "instance_local"])
    def test_private_medium_without_lease_is_fatal(self, kind):
        with pytest.raises(MountContractError) as exc:
            validate_storage_contract(
                root="/var/lib/datacode/workcopies",
                mount_kind=kind,
                lease_enabled=False,
                distributed_env_lock=True,
            )
        assert "requires workspace leases" in str(exc.value)

    @pytest.mark.parametrize("kind", ["ephemeral", "instance_local"])
    def test_private_medium_with_lease_is_valid(self, kind):
        contract = validate_storage_contract(
            root="/var/lib/datacode/workcopies",
            mount_kind=kind,
            lease_enabled=True,
            # Leases make the in-process lock sufficient, so this is the
            # expected production shape rather than a degraded one.
            distributed_env_lock=False,
        )
        assert contract.mount_kind.is_replica_private() is True
        assert contract.lease_enabled is True


class TestContractShape:
    def test_root_must_not_be_blank(self):
        with pytest.raises(MountContractError, match="materialize.root"):
            validate_storage_contract(
                root="   ",
                mount_kind="ephemeral",
                lease_enabled=True,
                distributed_env_lock=False,
            )

    def test_resolved_root_expands_user(self):
        contract = validate_storage_contract(
            root="~/.datacode/workcopies",
            mount_kind="instance_local",
            lease_enabled=True,
            distributed_env_lock=False,
        )
        assert contract.resolved_root.is_absolute()
        assert "~" not in str(contract.resolved_root)

    def test_describe_exposes_the_validated_triple(self):
        contract = validate_storage_contract(
            root="/var/lib/datacode/workcopies",
            mount_kind="instance_local",
            lease_enabled=True,
            distributed_env_lock=False,
        )
        described = contract.describe()
        assert described["mount_kind"] == "instance_local"
        assert described["lease_enabled"] is True
        assert described["replica_private"] is True
