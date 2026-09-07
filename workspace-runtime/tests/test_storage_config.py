"""Runtime storage/lease configuration tests (design 07 ADR-003 Q7, P4a).

The unit contract of the validator itself is covered in the shared library.
What is pinned here is the SERVICE wiring, which is where a silent regression
would actually come from: the YAML keys resolving to the right fields, the
declared shipping default staying bootable, and the two fatal misconfigurations
still failing through this service's own settings object rather than only
through a direct call to the validator.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.materialize import MountContractError, MountKind  # noqa: E402

from src.config import Settings  # noqa: E402


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


class TestShippedDefaults:
    def test_declared_default_is_bootable(self):
        # conf/workspace-runtime.yaml describes the CURRENT deployment (shared
        # volume, no leases yet). If this ever fails, the shipped config would
        # crash every runtime on startup.
        contract = _settings().storage_contract()
        assert contract.mount_kind is MountKind.SHARED
        assert contract.lease_enabled is False

    def test_materialize_root_defaults_to_workspace_base_path(self):
        # Until P4b splits them, working copies stay in the tree the platform
        # already declares; an empty root must not resolve to "".
        settings = _settings()
        assert settings.materialize_root == settings.workspace_base_path
        assert settings.materialize_root

    def test_lease_defaults_are_loaded_from_yaml(self):
        settings = _settings()
        assert settings.lease_ttl_seconds == 120
        assert settings.lease_max_per_instance == 16
        assert settings.lease_admission_rss_high_watermark == pytest.approx(0.80)
        assert settings.lease_idle_release_seconds == 1800

    def test_wip_interval_matches_the_published_contract(self):
        """600s is a PROMISE, not a tuning knob (design 07 Q6).

        The product documentation and the frontend both state "at most 10
        minutes". If someone changes this value as a performance tweak, the
        published guarantee silently becomes false — so the number is pinned
        here to force the contract to be revisited deliberately.
        """
        assert _settings().wip_snapshot_interval_seconds == 600
        assert _settings().wip_snapshot_skip_if_clean is True


class TestFatalCombinationsFailThroughSettings:
    def test_shared_medium_with_leases_and_in_process_lock_is_rejected(self):
        # The combination that is worse than 1.x: the lease justifies degrading
        # env locks to in-process, and a shared medium then admits a second
        # writer with nothing serializing it.
        settings = _settings(
            materialize_mount_kind="shared",
            lease_enabled=True,
            env_lock_backend="in_memory",
        )
        with pytest.raises(MountContractError, match="behind the lease"):
            settings.storage_contract()

    def test_shared_medium_with_leases_and_redis_lock_is_the_p4a_transition(self):
        # Design 06 4a: leases route the happy path on the original shared PV
        # while the Redis env lock still serializes the degradation path.
        # Allowed, but only with a warning that names the destination shape.
        contract = _settings(
            materialize_mount_kind="shared",
            lease_enabled=True,
            env_lock_backend="redis",
        ).storage_contract()
        assert contract.lease_enabled is True
        assert any("P4a transitional" in w for w in contract.warnings)

    @pytest.mark.parametrize("kind", ["ephemeral", "instance_local"])
    def test_private_medium_without_leases_is_rejected(self, kind):
        settings = _settings(materialize_mount_kind=kind, lease_enabled=False)
        with pytest.raises(MountContractError, match="requires workspace leases"):
            settings.storage_contract()

    def test_unknown_mount_kind_is_rejected_rather_than_defaulted(self):
        settings = _settings(materialize_mount_kind="local")
        with pytest.raises(MountContractError, match="unknown materialize.mount_kind"):
            settings.storage_contract()


class TestP4bTargetShape:
    def test_instance_local_with_leases_is_valid(self):
        settings = _settings(
            materialize_mount_kind="instance_local",
            lease_enabled=True,
            materialize_root="/var/lib/datacode/workcopies",
        )
        contract = settings.storage_contract()
        assert contract.mount_kind.is_replica_private() is True
        assert contract.warnings == ()

    def test_env_lock_backend_feeds_the_distributed_lock_flag(self):
        # A shared medium's only remaining serialization is the env lock, so the
        # contract must observe the real backend rather than assume one.
        assert (
            _settings(env_lock_backend="in_memory")
            .storage_contract()
            .distributed_env_lock
            is False
        )
        assert (
            _settings(env_lock_backend="redis").storage_contract().distributed_env_lock
            is True
        )

    def test_shared_medium_without_distributed_lock_warns_but_boots(self):
        # This is the pre-P4 status quo. Failing startup here would take down
        # running deployments the moment Redis became unreachable.
        contract = _settings(
            materialize_mount_kind="shared",
            lease_enabled=False,
            env_lock_backend="in_memory",
        ).storage_contract()
        assert len(contract.warnings) == 1
        assert "env_lock_backend" in contract.warnings[0]
