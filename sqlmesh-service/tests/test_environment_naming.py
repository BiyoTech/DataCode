"""Tests for the environment name algebra (pure functions).

The mapping between an environment, its Git branch and its Dagster deployment
must be total and bidirectional: every other service derives names from these
functions, so a regression here silently detaches code from data.
"""

import pytest

from src.environments.naming import (
    MAX_DEPLOYMENT_NAME_LENGTH,
    PRODUCTION_BRANCH,
    PRODUCTION_ENV,
    branch_of,
    deployment_name,
    env_of,
    environment_id,
    is_valid_env_name,
    split_environment_id,
    suggest_sandbox_name,
)


@pytest.mark.parametrize("name", ["prod", "dev_alice", "feature_orders", "a1_b2"])
def test_valid_environment_names(name):
    assert is_valid_env_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "p",             # too short
        "Prod",          # uppercase
        "my-env",        # "-" collides with SQL identifiers
        "env/x",         # "/" collides with Git refs
        "1env",          # must start with a letter
        "x" * 41,        # too long
    ],
)
def test_invalid_environment_names(name):
    assert not is_valid_env_name(name)


def test_layout_reserved_names_are_rejected():
    """``envs`` is the directory holding every environment; an environment of
    the same name would make ``.../u1/envs/envs`` ambiguous for any consumer
    that locates the environment segment by value instead of by position."""
    assert not is_valid_env_name("envs")


def test_production_maps_to_the_main_branch():
    assert branch_of(PRODUCTION_ENV) == PRODUCTION_BRANCH
    assert env_of(PRODUCTION_BRANCH) == PRODUCTION_ENV


def test_non_production_environments_use_the_branch_namespace():
    assert branch_of("dev_alice") == "env/dev_alice"
    assert env_of("env/dev_alice") == "dev_alice"


@pytest.mark.parametrize("branch", ["release/1.0", "feature/x", "env/Bad-Name", "dev"])
def test_platform_external_branches_have_no_environment(branch):
    assert env_of(branch) is None


def test_environment_id_round_trips():
    key = environment_id("ws-t1-sales", "dev_alice")
    assert key == "ws-t1-sales:dev_alice"
    assert split_environment_id(key) == ("ws-t1-sales", "dev_alice")


@pytest.mark.parametrize("value", ["", "no-separator", "ws-t1-sales:", ":prod"])
def test_malformed_environment_ids_are_rejected(value):
    assert split_environment_id(value) == ("", "")


def test_deployment_name_is_a_safe_identifier():
    assert deployment_name("ws-t1-sales", "dev_alice") == "ws_t1_sales__dev_alice"


def test_deployment_name_stays_within_the_resource_name_limit():
    """The name doubles as a K8s resource name, capped at 63 characters."""
    name = deployment_name("w" * 200, "dev_alice")
    assert len(name) <= MAX_DEPLOYMENT_NAME_LENGTH
    # The environment must survive intact: it is what identifies the deployment.
    assert name.endswith("__dev_alice")


def test_truncated_deployment_names_stay_unique_and_reproducible():
    """Truncation must not merge two workspaces into one deployment."""
    first = deployment_name("w" * 120 + "_a", "dev_alice")
    second = deployment_name("w" * 120 + "_b", "dev_alice")
    assert first != second
    assert first == deployment_name("w" * 120 + "_a", "dev_alice")


def test_sandbox_name_suggestion_is_always_valid():
    assert suggest_sandbox_name("user-42") == "dev_user_42"
    assert is_valid_env_name(suggest_sandbox_name("user-42"))
    assert is_valid_env_name(suggest_sandbox_name(""))
