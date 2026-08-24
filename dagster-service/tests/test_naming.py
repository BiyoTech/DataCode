"""Tests for the Dagster-side mirror of the platform naming algebra.

Two things must hold: deployment names are IDENTICAL to the ones the environment
registry derives (they meet in workspace.yaml and in the registry's rows), and a
working-copy path is decomposed strictly — a path that does not match the
environment layout must be rejected rather than guessed at.
"""

from pathlib import Path

import pytest

from src.core import naming


# --- Deployment names --------------------------------------------------------


def test_deployment_name_joins_workspace_and_environment():
    # Same expectation as sqlmesh-service's naming test: the two derivations
    # must agree byte for byte.
    assert naming.deployment_name("ws-t1-sales", "dev_alice") == "ws_t1_sales__dev_alice"
    assert naming.deployment_name("ws-t1-sales", "prod") == "ws_t1_sales__prod"


def test_deployment_name_truncates_long_workspace_ids_without_colliding():
    long_a = "ws-t1-" + "a" * 80
    long_b = "ws-t1-" + "a" * 81
    name_a = naming.deployment_name(long_a, "prod")
    name_b = naming.deployment_name(long_b, "prod")

    assert len(name_a) <= naming.MAX_DEPLOYMENT_NAME_LENGTH
    assert name_a.endswith("__prod")  # the environment suffix stays intact
    assert name_a != name_b  # digest keeps distinct ids distinct
    assert name_a == naming.deployment_name(long_a, "prod")  # deterministic


# --- Environment names and types --------------------------------------------


@pytest.mark.parametrize("name", ["prod", "dev_alice", "feature_orders"])
def test_valid_environment_names(name):
    assert naming.is_valid_env_name(name)


@pytest.mark.parametrize("name", ["", "Prod", "dev-alice", "a", "envs", "x" * 41])
def test_invalid_environment_names(name):
    assert not naming.is_valid_env_name(name)


# --- Working copy decomposition ---------------------------------------------


def _make_copy(tmp_path, environment="dev_alice"):
    base = tmp_path / "workspaces"
    path = base / "t1" / "ws-t1-sales" / "u42" / "envs" / environment
    path.mkdir(parents=True)
    return str(base), str(path)


def test_parse_working_copy_extracts_the_full_identity(tmp_path):
    base, path = _make_copy(tmp_path)
    copy = naming.parse_working_copy(base, path)

    assert copy.tenant_id == "t1"
    assert copy.workspace_id == "ws-t1-sales"
    assert copy.user_id == "u42"
    assert copy.environment == "dev_alice"
    assert copy.deployment_name == "ws_t1_sales__dev_alice"


def test_parse_working_copy_defaults_non_production_to_sandbox(tmp_path):
    base, path = _make_copy(tmp_path)
    assert naming.parse_working_copy(base, path).default_env_type == naming.ENV_TYPE_SANDBOX


def test_parse_working_copy_recognizes_production_by_name(tmp_path):
    base, path = _make_copy(tmp_path, environment="prod")
    assert naming.parse_working_copy(base, path).default_env_type == naming.ENV_TYPE_PRODUCTION


def test_parse_working_copy_rejects_path_outside_base(tmp_path):
    base, _ = _make_copy(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(naming.WorkingCopyLayoutError):
        naming.parse_working_copy(base, str(outside))


def test_parse_working_copy_rejects_the_user_root(tmp_path):
    """The user root holds ``.repo`` and ``envs/``; it is not a working copy."""
    base, path = _make_copy(tmp_path)
    user_root = str(Path(path).parent.parent)
    with pytest.raises(naming.WorkingCopyLayoutError):
        naming.parse_working_copy(base, user_root)


def test_parse_working_copy_rejects_a_directory_beside_envs(tmp_path):
    base, _ = _make_copy(tmp_path)
    sibling = Path(base) / "t1" / "ws-t1-sales" / "u42" / "other" / "prod"
    sibling.mkdir(parents=True)
    with pytest.raises(naming.WorkingCopyLayoutError):
        naming.parse_working_copy(base, str(sibling))


def test_parse_working_copy_rejects_an_invalid_environment_segment(tmp_path):
    base, _ = _make_copy(tmp_path)
    bad = Path(base) / "t1" / "ws-t1-sales" / "u42" / "envs" / "Bad-Env"
    bad.mkdir(parents=True)
    with pytest.raises(naming.WorkingCopyLayoutError):
        naming.parse_working_copy(base, str(bad))
