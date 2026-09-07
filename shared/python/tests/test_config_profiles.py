"""Configuration profile contract tests (base + profile overlay model).

The deployment story of this platform rests on ONE claim: the same resolution
mechanism serves host dev, Docker and Kubernetes, and an environment differs
only by which profile is selected. These tests pin that claim, because the ways
it can break are all silent:

* ``conf/platform.yaml`` is deliberately address-free. If someone re-adds a
  usable ``database.host`` to it, every profile keeps working AND a deployment
  that forgot to select one silently connects to that address instead of failing.
* A profile is an INCREMENT. If a profile stops declaring an address it looks
  fine locally (the base default fills in) and connects to localhost in
  production.
* A container/cluster profile that mentions ``localhost`` resolves to the
  container itself — a connection refused at best, the wrong process at worst.
* ``cells.expect_global_db`` and ``database.services.identity`` must agree in
  both directions: the flag without the override is refused at startup, and the
  override without the flag disables the very guard that makes the split safe.

The values are read from the real files through the real loader, so these tests
fail when the shipped configuration drifts, not when a copy of it does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import datacode_common.config.platform as platform_module
from datacode_common.config.platform import PlatformConfig, platform_config

CONF_DIR = Path(__file__).resolve().parents[3] / "conf"

# Every profile the platform ships, and whether it describes a full platform or
# only the global tier. `identity_only` profiles legitimately declare no gateway
# or runtime URL: publishing an address nothing in that tier can reach would be
# worse than omitting it.
ALL_PROFILES = ("dev", "docker", "cell", "global", "k8s-cell", "k8s-global")
IDENTITY_ONLY_PROFILES = ("global", "k8s-global")
FULL_PLATFORM_PROFILES = ("dev", "docker", "cell", "k8s-cell")
# Profiles whose addresses are resolved from INSIDE a container.
CONTAINERIZED_PROFILES = ("docker", "cell", "global", "k8s-cell", "k8s-global")

# The frozen platform port assignment. Identical in every environment on
# purpose: only the host part of a URL is environment-specific, so a port that
# differs between profiles is a mistake rather than a choice.
EXPECTED_PORTS = {
    "gateway": 8000,
    "agent": 8001,
    "runtime": 8002,
    "identity": 8003,
    "control": 8004,
}


def load_profile(profile: str, monkeypatch: pytest.MonkeyPatch) -> PlatformConfig:
    """Resolve *profile* through the production loader.

    The module-level cache is reset so each profile is loaded fresh; without
    that, the first profile touched in a session would answer for all of them.
    """
    monkeypatch.setenv("DATACODE_ENV", profile)
    monkeypatch.setattr(platform_module, "_platform", None)
    return platform_config()


def raw_yaml(name: str) -> dict:
    """Parse a config file WITHOUT merging, to assert on what it itself says."""
    return yaml.safe_load((CONF_DIR / name).read_text(encoding="utf-8")) or {}


class TestBaseIsAddressFree:
    """The base carries structure and invariants — never a reachable address.

    This is the property that lets profiles be increments instead of complete
    copies. The previous design (dev values in the base) forced every container
    deployment to ship a full replacement file, and those replacements drifted.
    """

    def test_no_profile_file_is_missing(self):
        for profile in ALL_PROFILES:
            path = CONF_DIR / f"platform.{profile}.yaml"
            assert path.exists(), f"profile {profile} is referenced but has no file"

    @pytest.mark.parametrize(
        "dotted_key",
        [
            "database.host",
            "database.port",
            "database.name",
            "database.username",
            "redis.host",
            "redis.port",
            "workspace.base_path",
            "services",
        ],
    )
    def test_base_declares_no_addresses(self, dotted_key: str):
        node = raw_yaml("platform.yaml")
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return  # absent: exactly what this test wants
            node = node[part]
        pytest.fail(
            f"conf/platform.yaml must not declare {dotted_key!r} (found {node!r}): "
            "an address in the base silently serves deployments that forgot to "
            "select a profile"
        )

    def test_base_keeps_the_cross_environment_invariants(self):
        """The mirror image: stripping addresses must not strip everything.

        Pool sizing, key prefixes, timeouts, the log mirroring policy and the
        env-var registry are properties of the WORKLOAD, and duplicating them
        into six profiles is how they start disagreeing.
        """
        base = raw_yaml("platform.yaml")

        assert base["database"]["pool_size"] > 0
        assert base["redis"]["key_prefix"] == "datacode"
        assert base["redis"]["operation_timeout_seconds"] > 0
        assert base["cells"]["default_cell"]
        assert base["logging"]["workspace_file"]["enabled"] is True
        assert base["security"], "the env-var registry is the base's job"


class TestCodeDefaultsArePostgresAligned:
    """Code defaults must not smuggle in a second database dialect.

    These defaults only surface when NO profile was selected — every shipped
    profile addresses the database explicitly. They were once the MySQL-era
    ``port=3306``/``username=root`` pair, which combined with the base file's
    ``type: postgresql`` into a chimera URL (postgres scheme, MySQL port and
    user) that connected to whatever MySQL happened to listen locally and
    failed with a protocol error far from the actual misconfiguration.
    Aligned defaults keep the forgotten-profile failure legible: a plain
    connection refused against localhost:5432.
    """

    def test_database_defaults_describe_local_postgres(self):
        from datacode_common.config.platform import (
            DatabaseConfig,
            DatabaseServiceConfig,
        )

        for cfg in (DatabaseConfig(), DatabaseServiceConfig()):
            assert cfg.type == "postgresql"
            assert cfg.host == "localhost"
            assert cfg.port == 5432
            assert cfg.username == "datacode"


class TestProfilesResolve:
    """Each profile must produce a config a service can actually run on."""

    @pytest.mark.parametrize("profile", ALL_PROFILES)
    def test_database_is_fully_addressed(self, profile, monkeypatch):
        cfg = load_profile(profile, monkeypatch)

        assert cfg.database.type == "postgresql", (
            "one engine across dev/Docker/K8s: a dialect-specific defect must "
            "appear locally before it can reach production"
        )
        assert cfg.database.host
        assert cfg.database.port > 0
        assert cfg.database.name
        assert cfg.database.username

    @pytest.mark.parametrize("profile", ALL_PROFILES)
    def test_database_url_is_buildable(self, profile, monkeypatch):
        cfg = load_profile(profile, monkeypatch)

        url = cfg.database.url()
        assert url.startswith("postgresql+"), url
        assert cfg.database.host in url

    @pytest.mark.parametrize("profile", ALL_PROFILES)
    def test_redis_is_addressed_when_enabled(self, profile, monkeypatch):
        cfg = load_profile(profile, monkeypatch)

        if cfg.redis.enabled:
            assert cfg.redis.host
            assert cfg.redis.port > 0
            assert cfg.redis.safe_url().startswith("redis://")

    @pytest.mark.parametrize("profile", FULL_PLATFORM_PROFILES)
    def test_full_platform_profiles_publish_every_service_url(
        self, profile, monkeypatch
    ):
        cfg = load_profile(profile, monkeypatch)

        assert cfg.services.url("gateway", "http_url")
        assert cfg.services.url("agent", "rest_url")
        assert cfg.services.url("agent", "ws_url")
        assert cfg.services.url("agent", "grpc_url")
        assert cfg.services.url("runtime")
        assert cfg.services.url("control")
        assert cfg.services.url("identity")

    @pytest.mark.parametrize("profile", IDENTITY_ONLY_PROFILES)
    def test_global_profiles_publish_only_identity(self, profile, monkeypatch):
        """The global tier runs identity and nothing else."""
        cfg = load_profile(profile, monkeypatch)

        assert cfg.services.url("identity")
        for service in ("gateway", "agent", "runtime", "control"):
            assert not cfg.services.url(service, "rest_url"), (
                f"{profile} publishes a {service} URL, but nothing in the "
                "global tier can reach one"
            )

    @pytest.mark.parametrize("profile", FULL_PLATFORM_PROFILES)
    def test_workspace_base_path_is_declared(self, profile, monkeypatch):
        cfg = load_profile(profile, monkeypatch)
        assert cfg.workspace_base_path

    @pytest.mark.parametrize("profile", ALL_PROFILES)
    def test_ports_are_frozen_across_profiles(self, profile, monkeypatch):
        """Only the HOST part of a service URL may differ between profiles."""
        cfg = load_profile(profile, monkeypatch)

        checks = {
            "gateway": cfg.services.url("gateway", "http_url"),
            "agent": cfg.services.url("agent", "rest_url"),
            "runtime": cfg.services.url("runtime"),
            "identity": cfg.services.url("identity"),
            "control": cfg.services.url("control"),
        }
        for service, url in checks.items():
            if not url:
                continue  # legitimately absent (see the global tier test)
            assert url.endswith(f":{EXPECTED_PORTS[service]}"), (
                f"{profile}: {service} is published as {url}, but the platform "
                f"port for it is {EXPECTED_PORTS[service]}"
            )


class TestContainerProfilesAvoidLocalhost:
    """`localhost` inside a container is the container — never the peer."""

    @pytest.mark.parametrize("profile", CONTAINERIZED_PROFILES)
    def test_no_localhost_anywhere(self, profile, monkeypatch):
        cfg = load_profile(profile, monkeypatch)

        offenders: list[str] = []
        if "localhost" in cfg.database.host or "127.0.0.1" in cfg.database.host:
            offenders.append(f"database.host={cfg.database.host}")
        if cfg.redis.enabled and (
            "localhost" in cfg.redis.host or "127.0.0.1" in cfg.redis.host
        ):
            offenders.append(f"redis.host={cfg.redis.host}")
        for service in ("gateway", "agent", "runtime", "control", "identity"):
            for key in ("rest_url", "http_url", "ws_url", "grpc_url"):
                url = cfg.services.url(service, key)
                if url and ("localhost" in url or "127.0.0.1" in url):
                    offenders.append(f"services.{service}.{key}={url}")

        assert not offenders, (
            f"{profile} resolves inside a container, so these point at the "
            f"container itself: {offenders}"
        )


class TestGlobalCellSplitIsCoherent:
    """The flag and the override must agree — in both directions."""

    @pytest.mark.parametrize("profile", ALL_PROFILES)
    def test_flag_and_override_agree(self, profile, monkeypatch):
        cfg = load_profile(profile, monkeypatch)
        identity_override = cfg.database.services.get("identity")

        if cfg.cells.expect_global_db:
            assert identity_override is not None, (
                f"{profile} declares expect_global_db but ships no "
                "database.services.identity override: control-service refuses "
                "to start in exactly this state"
            )
        elif identity_override is not None:
            pytest.fail(
                f"{profile} overrides database.services.identity without "
                "declaring expect_global_db, which disables the startup guard "
                "that makes the split safe"
            )

    @pytest.mark.parametrize("profile", ("cell", "k8s-cell"))
    def test_cell_identity_points_off_the_cell(self, profile, monkeypatch):
        """A cell's identity data must not live in that cell's database.

        Otherwise one cell's database failure is a platform-wide login outage,
        and every cell ends up sharing one cell's users.
        """
        cfg = load_profile(profile, monkeypatch)
        identity = cfg.database.services["identity"]

        assert identity.host != cfg.database.host, (
            f"{profile}: identity resolves to the CELL database "
            f"({identity.host}), defeating the Global/Cell split"
        )
        assert identity.name != cfg.database.name


class TestSandboxEgressIsOnlyClaimedWhereEnforced:
    """An egress proxy without a NetworkPolicy is an illusion of control.

    Only the Kubernetes profiles ship the NetworkPolicies that make the proxy a
    sandbox's ONLY outbound path, so only they may advertise its address. A dev
    or compose profile pointing at a proxy would mediate SOME traffic while the
    rest flowed freely — which reads as "egress is controlled" in the config and
    is not true on the wire.
    """

    @pytest.mark.parametrize("profile", ("dev", "docker", "cell", "global"))
    def test_unenforced_profiles_declare_no_proxy(self, profile, monkeypatch):
        load_profile(profile, monkeypatch)
        raw = raw_yaml(f"platform.{profile}.yaml")
        services = raw.get("services") or {}

        assert "egress_proxy" not in services, (
            f"{profile} has no NetworkPolicy, so a configured egress proxy "
            "would only mediate part of a sandbox's traffic"
        )

    def test_k8s_cell_declares_the_full_egress_plane(self):
        services = raw_yaml("platform.k8s-cell.yaml")["services"]

        assert services["egress_proxy"]["url"].endswith(":3128")
        # A Pod with dns_policy None has NO resolver without this address.
        assert services["egress_dns"]["address"]
