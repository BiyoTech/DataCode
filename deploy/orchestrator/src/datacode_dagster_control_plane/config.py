"""Configuration of the control-plane workspace syncer (env-only).

The syncer runs beside the Dagster webserver and daemon in the control plane. It
has no YAML file on purpose: it is a sidecar with a handful of knobs, deployed by
the same compose/Helm unit as the webserver, and a second configuration format
between the two planes is a second thing to keep in sync.

Secrets (the internal token) are read from the environment and never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Environment variable names, all prefixed to avoid clashing with Dagster's own.
ENV_REPLICAS = "DATACODE_DATA_PLANE_URLS"
ENV_TOKEN = "DATACODE_INTERNAL_TOKEN"
ENV_WORKSPACE_FILE = "DATACODE_WORKSPACE_FILE"
ENV_GRAPHQL_URL = "DATACODE_DAGSTER_GRAPHQL_URL"
ENV_GRAPHQL_TOKEN = "DATACODE_DAGSTER_GRAPHQL_TOKEN"
ENV_POLL_INTERVAL = "DATACODE_SYNC_INTERVAL_SECONDS"
ENV_REQUEST_TIMEOUT = "DATACODE_SYNC_REQUEST_TIMEOUT_SECONDS"
ENV_CONTROL_PLANE_ID = "DATACODE_CONTROL_PLANE_ID"

# Poll faster than the daemon's own 60s workspace refresh so a new deployment is
# picked up on the daemon's next cycle rather than the one after it.
DEFAULT_POLL_INTERVAL = 15.0
DEFAULT_REQUEST_TIMEOUT = 10.0
DEFAULT_WORKSPACE_FILE = "/opt/dagster/dagster_home/workspace.yaml"


class ConfigError(RuntimeError):
    """Raised when the syncer cannot run with the given environment."""


@dataclass(frozen=True)
class SyncerConfig:
    """Everything the syncer needs, resolved from the environment."""

    replica_urls: tuple[str, ...]
    internal_token: str
    workspace_file: str
    graphql_url: str
    graphql_token: str = ""
    poll_interval: float = DEFAULT_POLL_INTERVAL
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    control_plane_id: str = field(default="dagster-control-plane")

    @classmethod
    def from_env(cls) -> "SyncerConfig":
        """Build the config, failing fast on anything missing or unusable.

        Fails rather than defaults: a syncer that starts with no replicas, or
        without the token that lets it read them, would keep the control plane
        pointed at an empty workspace while looking healthy.
        """
        replicas = tuple(
            url.strip().rstrip("/")
            for url in os.getenv(ENV_REPLICAS, "").split(",")
            if url.strip()
        )
        if not replicas:
            raise ConfigError(
                f"{ENV_REPLICAS} is required: a comma-separated list of "
                "dagster-service base URLs (e.g. http://dagster-service:8004)"
            )
        for url in replicas:
            if not url.startswith(("http://", "https://")):
                raise ConfigError(f"{ENV_REPLICAS} entry is not an HTTP(S) URL: {url}")

        token = os.getenv(ENV_TOKEN, "").strip()
        if not token:
            raise ConfigError(
                f"{ENV_TOKEN} is required: the data plane's /internal endpoints "
                "reject unauthenticated callers"
            )

        graphql_url = os.getenv(ENV_GRAPHQL_URL, "").strip()
        if not graphql_url:
            raise ConfigError(
                f"{ENV_GRAPHQL_URL} is required: the webserver must be told to "
                "reload after the workspace file changes"
            )

        return cls(
            replica_urls=replicas,
            internal_token=token,
            workspace_file=os.getenv(ENV_WORKSPACE_FILE, DEFAULT_WORKSPACE_FILE),
            graphql_url=graphql_url,
            graphql_token=os.getenv(ENV_GRAPHQL_TOKEN, "").strip(),
            poll_interval=_positive_float(ENV_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            request_timeout=_positive_float(
                ENV_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT
            ),
            control_plane_id=os.getenv(ENV_CONTROL_PLANE_ID, "").strip()
            or "dagster-control-plane",
        )


def _positive_float(name: str, default: float) -> float:
    """Read a positive float from the environment, falling back on default."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


__all__ = ["ConfigError", "SyncerConfig"]
