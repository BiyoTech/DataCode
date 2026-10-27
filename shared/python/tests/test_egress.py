"""Egress policy tests — the deny rules that carry the sandbox's security.

Focused on the properties that would be a platform compromise if broken, rather
than on exhaustive branch coverage: deny-by-default, resolved-address checking
(DNS rebinding), and the baseline ranges nobody may configure away.
"""

from __future__ import annotations

import pytest

from datacode_common.egress import (
    ALLOW_REASON,
    DENY_INTERNAL_ADDRESS,
    DENY_MALFORMED,
    DENY_PORT,
    DENY_UNKNOWN_HOST,
    DENY_UNRESOLVABLE,
    EgressConfigError,
    EgressDestination,
    EgressPolicy,
    EgressResolutionError,
    host_matches,
    normalize_host,
)

WAREHOUSE = "warehouse.acme.example"
GIT = "git.acme.example"


def _resolver(mapping: dict[str, list[str]]):
    """A resolver serving a fixed table; unknown names fail like a real one."""

    def resolve(host: str):
        try:
            return mapping[host]
        except KeyError:
            raise EgressResolutionError(f"NXDOMAIN for {host}") from None

    return resolve


def _policy(**kwargs) -> EgressPolicy:
    """A realistic tenant policy: one warehouse, one Git server."""
    resolver = kwargs.pop(
        "resolver",
        _resolver({WAREHOUSE: ["93.184.216.34"], GIT: ["8.8.4.4"]}),
    )
    destinations = kwargs.pop(
        "destinations",
        [
            EgressDestination(host=WAREHOUSE, ports=(5432,), kind="warehouse"),
            EgressDestination(host=GIT, ports=(443,), kind="git"),
        ],
    )
    return EgressPolicy.build(destinations, resolver=resolver, **kwargs)


class TestDenyByDefault:
    """An undeclared destination must be unreachable."""

    def test_declared_host_and_port_is_allowed(self):
        decision = _policy().evaluate(WAREHOUSE, 5432)

        assert decision.allowed
        assert decision.reason == ALLOW_REASON
        assert decision.resolved == ("93.184.216.34",)

    def test_undeclared_host_is_denied(self):
        decision = _policy().evaluate("pypi.org", 443)

        assert not decision.allowed
        assert decision.reason == DENY_UNKNOWN_HOST

    def test_empty_allowlist_denies_everything(self):
        """The classic misconfiguration must fail closed, not open."""
        policy = EgressPolicy.build([], resolver=_resolver({WAREHOUSE: ["93.184.216.34"]}))

        assert not policy.evaluate(WAREHOUSE, 443).allowed

    def test_declared_host_on_another_port_is_denied_distinctly(self):
        """A tenant needs to know it was the port, not the host."""
        decision = _policy().evaluate(WAREHOUSE, 22)

        assert not decision.allowed
        assert decision.reason == DENY_PORT


class TestResolvedAddressChecking:
    """The property that closes DNS rebinding (T3/T6/T7)."""

    def test_allowlisted_name_pointing_inside_the_cluster_is_denied(self):
        policy = _policy(resolver=_resolver({WAREHOUSE: ["10.0.0.5"]}))

        decision = policy.evaluate(WAREHOUSE, 5432)

        assert not decision.allowed
        assert decision.reason == DENY_INTERNAL_ADDRESS
        assert "10.0.0.5" in decision.detail

    def test_one_bad_address_among_several_denies_the_whole_name(self):
        """Partial success is the attack, not a partial success."""
        policy = _policy(
            resolver=_resolver({WAREHOUSE: ["93.184.216.34", "192.168.1.7"]})
        )

        decision = policy.evaluate(WAREHOUSE, 5432)

        assert not decision.allowed
        assert decision.reason == DENY_INTERNAL_ADDRESS

    def test_ipv6_mapped_ipv4_cannot_bypass_the_v4_rules(self):
        policy = _policy(resolver=_resolver({WAREHOUSE: ["::ffff:10.0.0.5"]}))

        assert not policy.evaluate(WAREHOUSE, 5432).allowed

    def test_unresolvable_host_is_denied_not_allowed(self):
        policy = _policy(resolver=_resolver({}))

        decision = policy.evaluate(WAREHOUSE, 5432)

        assert not decision.allowed
        assert decision.reason == DENY_UNRESOLVABLE


class TestBaselineRanges:
    """Ranges no configuration may open."""

    @pytest.mark.parametrize(
        "address",
        [
            "169.254.169.254",  # T6 cloud metadata
            "10.0.0.1",         # T3 platform data plane
            "172.16.0.1",
            "192.168.1.1",
            "127.0.0.1",
            "9.1.1.1",
            "11.1.1.1",
            "21.1.1.1",
            "30.1.1.1",
            "::1",
            "fd00::1",
        ],
    )
    def test_internal_literals_are_denied_even_if_declared(self, address):
        """Declaring an internal address must not make it reachable."""
        policy = EgressPolicy.build(
            [EgressDestination(host=address, ports=(443, 5432, 80))],
            resolver=_resolver({}),
        )

        decision = policy.evaluate(address, 443)

        assert not decision.allowed
        assert decision.reason == DENY_INTERNAL_ADDRESS

    def test_public_literal_still_needs_a_declaration(self):
        policy = _policy()

        assert not policy.evaluate("93.184.216.34", 443).allowed

    def test_declared_public_literal_is_allowed(self):
        policy = EgressPolicy.build(
            [EgressDestination(host="93.184.216.34", ports=(5432,), kind="warehouse")],
            resolver=_resolver({}),
        )

        assert policy.evaluate("93.184.216.34", 5432).allowed

    def test_extra_cidrs_add_to_the_baseline(self):
        policy = EgressPolicy.build(
            [EgressDestination(host=WAREHOUSE, ports=(5432,))],
            extra_denied_cidrs=["93.184.216.0/24"],
            resolver=_resolver({WAREHOUSE: ["93.184.216.34"]}),
        )

        decision = policy.evaluate(WAREHOUSE, 5432)

        assert not decision.allowed
        assert decision.reason == DENY_INTERNAL_ADDRESS

    @pytest.mark.parametrize(
        "address",
        [
            "203.0.113.10",  # RFC 5737 documentation range
            "198.51.100.7",  # RFC 5737 documentation range
            "224.0.0.1",     # multicast
            "0.0.0.0",       # unspecified
            "240.0.0.1",     # reserved
        ],
    )
    def test_non_global_addresses_are_denied_without_being_enumerated(self, address):
        """Ranges nobody listed are still refused.

        The explicit CIDR list ages badly, so anything the stdlib itself does not
        consider globally routable is refused too. Documentation, multicast and
        reserved ranges are not where a warehouse lives; seeing one in production
        means a misconfiguration, and failing closed is the right answer to that.
        """
        policy = EgressPolicy.build(
            [EgressDestination(host=address, ports=(443,))], resolver=_resolver({})
        )

        decision = policy.evaluate(address, 443)

        assert not decision.allowed
        assert decision.reason == DENY_INTERNAL_ADDRESS

    def test_invalid_cidr_is_fatal_not_skipped(self):
        """A typo must not silently widen the policy."""
        with pytest.raises(EgressConfigError, match="invalid denied CIDR"):
            EgressPolicy.build([], extra_denied_cidrs=["10.0.0.0/33"])


class TestHostMatching:
    def test_exact_match(self):
        assert host_matches("Example.COM", "example.com")

    def test_trailing_dot_is_the_same_host(self):
        assert host_matches("example.com.", "example.com")

    def test_wildcard_matches_subdomains(self):
        assert host_matches("acme.snowflakecomputing.com", "*.snowflakecomputing.com")

    def test_wildcard_does_not_match_the_bare_domain(self):
        """The apex and its sub-domains are often different parties."""
        assert not host_matches("snowflakecomputing.com", "*.snowflakecomputing.com")

    def test_wildcard_is_not_a_suffix_substring(self):
        """The failure mode this rules out: a look-alike domain matching."""
        assert not host_matches(
            "evil-snowflakecomputing.com", "*.snowflakecomputing.com"
        )

    def test_bracketed_ipv6_is_normalized(self):
        assert normalize_host("[::1]") == "::1"


class TestProfileKinds:
    """A profile constrains which KINDS of destination are conceivable."""

    def test_kind_not_permitted_by_the_profile_is_invisible(self):
        policy = EgressPolicy.build(
            [
                EgressDestination(host=WAREHOUSE, ports=(5432,), kind="warehouse"),
                EgressDestination(host="pypi.org", ports=(443,), kind="package_index"),
            ],
            allowed_kinds=["warehouse", "git"],
            resolver=_resolver({WAREHOUSE: ["93.184.216.34"], "pypi.org": ["1.1.1.1"]}),
        )

        assert policy.evaluate(WAREHOUSE, 5432).allowed
        assert not policy.evaluate("pypi.org", 443).allowed


class TestFromProfile:
    def _settings(self, **network) -> dict:
        base = {
            "default": "deny",
            "egress_via_proxy": True,
            "allowed_destinations": ["warehouse", "git"],
            "denied_cidrs": ["10.0.0.0/8"],
        }
        base.update(network)
        return {"network": base}

    def test_builds_from_the_profile_network_block(self):
        policy = EgressPolicy.from_profile(
            self._settings(),
            [EgressDestination(host=WAREHOUSE, ports=(5432,), kind="warehouse")],
            resolver=_resolver({WAREHOUSE: ["93.184.216.34"]}),
        )

        assert policy.evaluate(WAREHOUSE, 5432).allowed
        assert policy.allowed_kinds == frozenset({"warehouse", "git"})

    def test_allow_by_default_profile_is_rejected(self):
        """Such a profile would make every rule below it decorative."""
        with pytest.raises(EgressConfigError, match="must be 'deny'"):
            EgressPolicy.from_profile(self._settings(default="allow"))

    def test_real_profiles_are_deny_by_default(self):
        """Guards the actual conf/isolation-profiles.yaml, not a fixture."""
        from datacode_common.isolation import load_isolation_policy

        policy = load_isolation_policy(refresh=True)
        for name in ("L2_container", "L3_base"):
            profile = policy.profile(name)
            # Must not raise: the profile's network block is a usable policy.
            built = EgressPolicy.from_profile(profile.settings)
            assert built.evaluate("10.0.0.1", 443).reason == DENY_INTERNAL_ADDRESS
            # And with no tenant destinations, nothing public is reachable either.
            assert not built.evaluate("example.com", 443).allowed


class TestMalformedTargets:
    @pytest.mark.parametrize("host", ["", "  ", "http://example.com/x", "a b"])
    def test_malformed_hosts_are_denied(self, host):
        decision = _policy().evaluate(host, 443)

        assert not decision.allowed
        assert decision.reason == DENY_MALFORMED

    @pytest.mark.parametrize("port", [0, -1, 70000])
    def test_out_of_range_ports_are_denied(self, port):
        assert _policy().evaluate(WAREHOUSE, port).reason == DENY_MALFORMED


class TestAuditFields:
    def test_decision_exposes_audit_fields(self):
        decision = _policy().evaluate(WAREHOUSE, 5432)

        fields = decision.audit_fields()

        assert fields["egress_allowed"] is True
        assert fields["egress_host"] == WAREHOUSE
        assert fields["egress_port"] == 5432
        assert fields["egress_resolved"] == ["93.184.216.34"]
