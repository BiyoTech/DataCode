"""The sandbox egress plane is configured once — in the platform profile.

The regression: ``conf/platform.k8s-cell.yaml`` declared
``services.egress_proxy.url`` and ``services.egress_dns.address``, but nothing
read them. The runtime only read ``egress_proxy_url`` / ``task_dns_server``,
which no Kubernetes manifest set — so cluster sandboxes ran with no proxy (no
egress at all) and the cluster resolver (internal names resolvable). The
profile also hard-coded a ClusterIP, which the cluster assigns and no file can
predict.

Pinned here:

* the k8s-cell profile's values reach the runtime's settings;
* a Service NAME is resolved to an IP for ``dnsConfig`` (which accepts IPs
  only), an IP passes through, and an unresolvable name degrades loudly
  instead of producing a Pod with a dead resolver.
"""

from __future__ import annotations

import socket

import pytest

from src.tasks.backends.kubernetes import resolve_nameserver


def _settings_under(monkeypatch, profile: str, **env: str):
    """Settings built from a given platform profile, with a fresh platform cache."""
    from datacode_common.config import platform as platform_module

    monkeypatch.setenv("DATACODE_ENV", profile)
    for var in ("WORKSPACE_RUNTIME_EGRESS_PROXY_URL", "WORKSPACE_RUNTIME_TASK_DNS_SERVER"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    # The platform config is a module-level singleton; monkeypatch restores
    # the previous instance afterwards so other tests keep their profile.
    monkeypatch.setattr(platform_module, "_platform", None)
    from src.config import Settings

    return Settings()


class TestProfileReachesTheRuntime:
    def test_k8s_cell_profile_configures_proxy_and_resolver(self, monkeypatch):
        loaded = _settings_under(monkeypatch, "k8s-cell")

        assert loaded.egress_proxy_url.endswith(":3128")
        assert loaded.task_dns_server == "egress-dns.datacode-tasks.svc.cluster.local"

    def test_an_explicit_setting_wins_over_the_profile(self, monkeypatch):
        loaded = _settings_under(
            monkeypatch, "k8s-cell", WORKSPACE_RUNTIME_TASK_DNS_SERVER="10.0.0.10"
        )

        assert loaded.task_dns_server == "10.0.0.10"

    def test_dev_profile_configures_no_egress_plane(self, monkeypatch):
        """No NetworkPolicy in dev, so a proxy would only be a partial control."""
        loaded = _settings_under(monkeypatch, "dev")

        assert loaded.egress_proxy_url == ""
        assert loaded.task_dns_server == ""


class TestResolveNameserver:
    def test_empty_means_unset(self):
        assert resolve_nameserver("") == ""
        assert resolve_nameserver("   ") == ""

    @pytest.mark.parametrize("ip", ["10.96.0.53", "fd00::53"])
    def test_an_ip_passes_through(self, ip, monkeypatch):
        def _no_lookup(*_args, **_kwargs):
            raise AssertionError("an IP must not be looked up")

        monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
        assert resolve_nameserver(ip) == ip

    def test_a_service_name_resolves_to_its_cluster_ip(self, monkeypatch):
        calls = []

        def _lookup(host, port, *args, **kwargs):
            calls.append(host)
            return [
                (socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("fd00::53", 53, 0, 0)),
                (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("10.96.7.7", 53)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", _lookup)

        # IPv4 preferred: it is what a single-stack cluster's Pods can reach.
        assert resolve_nameserver("egress-dns.datacode-tasks.svc.cluster.local") == "10.96.7.7"
        assert calls == ["egress-dns.datacode-tasks.svc.cluster.local"]

    def test_an_unresolvable_name_degrades_instead_of_shipping_a_dead_resolver(
        self, monkeypatch
    ):
        def _fail(*_args, **_kwargs):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        assert resolve_nameserver("egress-dns.nowhere.svc.cluster.local") == ""

    def test_real_resolution_of_localhost(self):
        """No stub: the real resolver path produces an IP literal."""
        import ipaddress

        ipaddress.ip_address(resolve_nameserver("localhost"))
