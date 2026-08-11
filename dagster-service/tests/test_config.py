"""Tests for local/remote mode configuration resolution."""

from src.config import Settings


def test_local_mode_is_not_remote_and_uses_local_graphql():
    cfg = Settings(dagster_mode="local", dagster_graphql_url="http://localhost:3001/graphql")
    assert cfg.is_remote is False
    assert cfg.active_graphql_url == "http://localhost:3001/graphql"


def test_remote_mode_is_remote_and_uses_remote_graphql():
    cfg = Settings(
        dagster_mode="remote",
        dagster_remote_graphql_url="http://dagster-prod:3000/graphql",
    )
    assert cfg.is_remote is True
    assert cfg.active_graphql_url == "http://dagster-prod:3000/graphql"


def test_mode_matching_is_case_insensitive():
    assert Settings(dagster_mode="REMOTE").is_remote is True
    assert Settings(dagster_mode="Local").is_remote is False


def test_code_location_config_is_present_in_both_modes():
    """Code Location is ALWAYS local, so its config must exist regardless."""
    for mode in ("local", "remote"):
        cfg = Settings(dagster_mode=mode, dagster_remote_graphql_url="http://x/graphql")
        assert cfg.code_location_port > 0
        assert cfg.code_location_host
