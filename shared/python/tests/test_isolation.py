"""Tests for the isolation profile policy (design 04 §5, P6a).

This module decides where user code runs, so the tests focus on the two
properties that make it safe rather than on parsing mechanics: an unclaimed
task kind must land in a strong profile, and reducing the set of available
profiles must never widen anybody's sandbox.
"""

from __future__ import annotations

import pytest

from datacode_common.isolation import (
    FAILSAFE_ORDER,
    IsolationConfigError,
    IsolationLevel,
    IsolationPolicy,
    NoIsolationAvailable,
    build_isolation_policy,
    load_isolation_policy,
)

ALL_LEVELS = frozenset(IsolationLevel)
NO_CONTAINERS = frozenset({IsolationLevel.IN_PROCESS, IsolationLevel.PROCESS})


def _document(**profiles) -> dict:
    """Build a minimal configuration document around the given profiles."""
    return {"profiles": profiles, "default_profile": "strongest_enabled"}


# A compact stand-in for the real file: same shape, fewer fields.
def _realistic_document() -> dict:
    return _document(
        L1_inprocess={
            "isolation": "in_process",
            "allowed_task_kinds": ["file.read", "ontology.load"],
        },
        L3_proc={
            "isolation": "process",
            "persistent": True,
            "allowed_task_kinds": ["agent.shell", "sqlmesh.plan", "sqlmesh.apply"],
        },
        L2_container={
            "isolation": "container",
            "runtime_class": "runc",
            "timeout_seconds": 3600,
            "credentials": {"warehouse": "scoped_readwrite", "platform": "none"},
            "allowed_task_kinds": ["sqlmesh.plan", "sqlmesh.apply", "git.push"],
        },
        L3_base={
            "isolation": "container",
            "runtime_class": "runc",
            "timeout_seconds": 1800,
            "credentials": {"warehouse": "scoped_readonly", "platform": "none"},
            "allowed_task_kinds": ["agent.shell", "dagster.run"],
        },
        L3_strong={
            "enabled": False,
            "inherits": "L3_base",
            "runtime_class": "gvisor",
        },
    )


class TestRealConfiguration:
    """The shipped conf/isolation-profiles.yaml must load and be sane."""

    def test_loads_from_conf(self):
        policy = load_isolation_policy(refresh=True)

        assert "L1_inprocess" in policy.profile_names
        assert "L3_proc" in policy.profile_names
        assert "L3_base" in policy.profile_names

    def test_deferred_strong_profile_is_disabled(self):
        """Q2 decision: L3_strong ships disabled, not absent."""
        policy = load_isolation_policy(refresh=True)

        assert not policy.profile("L3_strong").enabled
        # Disabled but still ranked above L3_base, so enabling it later needs
        # no code change.
        assert policy.profile("L3_strong").strength > policy.profile("L3_base").strength

    def test_default_is_a_strong_profile(self):
        """The fallback must never be an L1/L2 profile."""
        policy = load_isolation_policy(refresh=True)

        assert policy.default_profile is not None
        assert policy.default_profile.name in FAILSAFE_ORDER

    def test_only_l2_may_write_to_the_warehouse(self):
        """Q5 constraint: a profile running user code gets read-only access."""
        policy = load_isolation_policy(refresh=True)

        assert policy.profile("L2_container").grants_writable_warehouse
        assert not policy.profile("L3_base").grants_writable_warehouse
        assert not policy.profile("L3_proc").grants_writable_warehouse


class TestSelection:
    """profile_for() is the whole point of the module."""

    def test_claimed_kind_resolves_to_its_profile(self):
        policy = build_isolation_policy(_realistic_document())

        assert policy.profile_for("file.read").name == "L1_inprocess"
        assert policy.profile_for("dagster.run").name == "L3_base"
        assert policy.profile_for("git.push").name == "L2_container"

    def test_overlap_resolves_to_the_strongest_claimant(self):
        """sqlmesh.plan is claimed by L3_proc and L2; the container wins."""
        policy = build_isolation_policy(_realistic_document())

        assert policy.profile_for("sqlmesh.plan").name == "L2_container"
        assert policy.profile_for("agent.shell").name == "L3_base"

    def test_unclaimed_kind_falls_back_to_the_default(self):
        policy = build_isolation_policy(_realistic_document())

        selected = policy.profile_for("something.nobody.declared")

        assert policy.default_profile is not None
        assert selected.name == policy.default_profile.name
        assert selected.name in FAILSAFE_ORDER

    def test_unclaimed_kind_never_falls_back_into_l1_or_l2(self):
        """The core fail-safe property, asserted directly."""
        policy = build_isolation_policy(_realistic_document())

        selected = policy.profile_for("unknown.kind")

        assert selected.name not in {"L1_inprocess", "L2_container"}

    def test_ambiguous_overlap_is_deterministic(self):
        """Two equally strong claimants must not depend on dict ordering."""
        document = _document(
            L3_proc={"isolation": "process", "allowed_task_kinds": []},
            A_container={
                "isolation": "container",
                "runtime_class": "runc",
                "allowed_task_kinds": ["shared.kind"],
            },
            B_container={
                "isolation": "container",
                "runtime_class": "runc",
                "allowed_task_kinds": ["shared.kind"],
            },
        )

        first = build_isolation_policy(document).profile_for("shared.kind").name
        second = build_isolation_policy(document).profile_for("shared.kind").name

        assert first == second == "A_container"

    def test_disabled_profile_is_never_selected(self):
        policy = build_isolation_policy(_realistic_document())

        # L3_strong claims dagster.run through inheritance but is disabled.
        assert policy.profile_for("dagster.run").name == "L3_base"


class TestStrengthLadder:
    def test_sandboxed_kernel_outranks_shared_kernel(self):
        policy = build_isolation_policy(_realistic_document())

        assert (
            policy.profile("L3_strong").strength
            > policy.profile("L3_base").strength
            > policy.profile("L3_proc").strength
            > policy.profile("L1_inprocess").strength
        )

    def test_enabled_profiles_are_ordered_strongest_first(self):
        policy = build_isolation_policy(_realistic_document())

        strengths = [p.strength for p in policy.enabled_profiles]

        assert strengths == sorted(strengths, reverse=True)
        assert "L3_strong" not in [p.name for p in policy.enabled_profiles]


class TestRestrictedTo:
    """Availability is separate from configuration."""

    def test_without_a_container_backend_work_lands_in_l3_proc(self):
        policy = build_isolation_policy(_realistic_document())

        restricted = policy.restricted_to(NO_CONTAINERS, reason="no kubernetes")

        assert restricted.profile_for("agent.shell").name == "L3_proc"
        assert restricted.profile_for("sqlmesh.plan").name == "L3_proc"
        assert restricted.default_profile.name == "L3_proc"

    def test_restriction_disables_rather_than_removes(self):
        """Keeping the profile visible makes the deployment's intent readable."""
        policy = build_isolation_policy(_realistic_document())

        restricted = policy.restricted_to(NO_CONTAINERS)

        assert "L3_base" in restricted.profile_names
        assert not restricted.profile("L3_base").enabled

    def test_restriction_does_not_mutate_the_original(self):
        policy = build_isolation_policy(_realistic_document())

        policy.restricted_to(NO_CONTAINERS)

        assert policy.profile("L3_base").enabled
        assert policy.profile_for("agent.shell").name == "L3_base"

    def test_unclaimed_kind_still_fails_safe_after_restriction(self):
        policy = build_isolation_policy(_realistic_document()).restricted_to(
            NO_CONTAINERS
        )

        assert policy.profile_for("unknown.kind").name == "L3_proc"

    def test_restricting_away_every_profile_is_fatal(self):
        """Better to refuse to start than to run user code unisolated."""
        policy = build_isolation_policy(_realistic_document())

        with pytest.raises(IsolationConfigError, match="refusing to start"):
            policy.restricted_to(frozenset())

    def test_in_process_only_keeps_serving_in_process_work(self):
        """A deployment with the task plane off still does file IO correctly."""
        policy = build_isolation_policy(_realistic_document()).restricted_to(
            frozenset({IsolationLevel.IN_PROCESS})
        )

        assert policy.profile_for("file.read").name == "L1_inprocess"
        # No fail-safe profile survives, so there is no default to fall back on.
        assert policy.default_profile is None

    def test_in_process_only_refuses_gated_work(self):
        """The core property: refuse, never downgrade into L1."""
        policy = build_isolation_policy(_realistic_document()).restricted_to(
            frozenset({IsolationLevel.IN_PROCESS})
        )

        # agent.shell is claimed only by process/container profiles, all disabled.
        with pytest.raises(NoIsolationAvailable, match="no fail-safe profile"):
            policy.profile_for("agent.shell")

        with pytest.raises(NoIsolationAvailable):
            policy.profile_for("unknown.kind")


class TestInheritance:
    def test_child_inherits_parent_fields(self):
        policy = build_isolation_policy(_realistic_document())
        strong = policy.profile("L3_strong")

        # Inherited from L3_base:
        assert strong.timeout_seconds == 1800
        assert strong.allowed_task_kinds == ("agent.shell", "dagster.run")
        # Overridden by the child:
        assert strong.runtime_class == "gvisor"

    def test_unknown_parent_is_rejected(self):
        document = _document(
            L3_proc={"isolation": "process", "allowed_task_kinds": []},
            Broken={"inherits": "NoSuchProfile", "isolation": "container"},
        )

        with pytest.raises(IsolationConfigError, match="inherits unknown profile"):
            build_isolation_policy(document)

    def test_circular_inheritance_is_rejected(self):
        document = _document(
            A={"inherits": "B", "isolation": "container"},
            B={"inherits": "A", "isolation": "container"},
        )

        with pytest.raises(IsolationConfigError, match="circular inherits"):
            build_isolation_policy(document)


class TestConfigurationErrors:
    """Every one of these would otherwise misplace user code silently."""

    def test_empty_document_is_rejected(self):
        with pytest.raises(IsolationConfigError, match="declares no profiles"):
            build_isolation_policy({})

    def test_missing_isolation_level_is_rejected(self):
        with pytest.raises(IsolationConfigError, match="no 'isolation' level"):
            build_isolation_policy(_document(X={"allowed_task_kinds": []}))

    def test_unknown_isolation_level_is_rejected(self):
        with pytest.raises(IsolationConfigError, match="unknown isolation"):
            build_isolation_policy(_document(X={"isolation": "vm"}))

    def test_non_mapping_profile_is_rejected(self):
        with pytest.raises(IsolationConfigError, match="must be a mapping"):
            build_isolation_policy(_document(X=["isolation: process"]))

    def test_non_list_task_kinds_is_rejected(self):
        with pytest.raises(IsolationConfigError, match="must be a list"):
            build_isolation_policy(
                _document(X={"isolation": "process", "allowed_task_kinds": "shell"})
            )

    def test_default_profile_pointing_at_a_disabled_profile_is_rejected(self):
        document = {
            "profiles": {
                "L3_proc": {"isolation": "process", "allowed_task_kinds": []},
                "Off": {"isolation": "container", "enabled": False},
            },
            "default_profile": "Off",
        }

        with pytest.raises(IsolationConfigError, match="is disabled"):
            build_isolation_policy(document)

    def test_explicit_default_profile_is_honoured(self):
        document = {
            "profiles": {
                "L3_proc": {"isolation": "process", "allowed_task_kinds": []},
                "L3_base": {
                    "isolation": "container",
                    "runtime_class": "runc",
                    "allowed_task_kinds": [],
                },
            },
            "default_profile": "L3_proc",
        }

        policy = build_isolation_policy(document)

        # L3_base is stronger, but the deployment named L3_proc explicitly.
        assert policy.default_profile.name == "L3_proc"

    def test_no_failsafe_candidate_leaves_no_default(self):
        """A file with no L3_* profile can serve only what it claims."""
        document = _document(
            L1_inprocess={"isolation": "in_process", "allowed_task_kinds": ["file.read"]},
            L2_container={
                "isolation": "container",
                "runtime_class": "runc",
                "allowed_task_kinds": ["sqlmesh.apply"],
            },
        )

        policy = build_isolation_policy(document)

        assert policy.default_profile is None
        # Claimed kinds still resolve; unknown ones are refused, not downgraded.
        assert policy.profile_for("sqlmesh.apply").name == "L2_container"
        with pytest.raises(NoIsolationAvailable):
            policy.profile_for("agent.shell")

    def test_unknown_profile_lookup_is_rejected(self):
        policy = build_isolation_policy(_realistic_document())

        with pytest.raises(IsolationConfigError, match="unknown isolation profile"):
            policy.profile("L9_imaginary")


class TestCaching:
    def test_policy_is_cached_between_calls(self):
        first = load_isolation_policy(refresh=True)
        second = load_isolation_policy()

        assert first is second

    def test_refresh_rebuilds(self):
        first = load_isolation_policy(refresh=True)
        second = load_isolation_policy(refresh=True)

        assert first is not second
        assert first.profile_names == second.profile_names


class TestPolicyConstruction:
    def test_policy_accepts_a_prebuilt_profile_mapping(self):
        """Cell-specific overrides construct a policy directly."""
        source = build_isolation_policy(_realistic_document())

        rebuilt = IsolationPolicy(
            {name: source.profile(name) for name in source.profile_names}
        )

        assert rebuilt.default_profile.name == source.default_profile.name
