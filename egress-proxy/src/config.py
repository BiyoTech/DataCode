"""Egress proxy configuration.

Everything is a plain setting read from the environment, with no YAML: this
process is deployed as a small, self-contained component and a config file would
be one more thing to mount into a Pod that deliberately mounts almost nothing.
The signing secret is env-only for the usual reason (secrets never in YAML).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Effective proxy settings.

    Attributes:
        host: Bind address. Defaults to all interfaces because the Pod's network
            namespace is the boundary; a NetworkPolicy decides who may connect.
        port: Listen port. 3128 matches the NetworkPolicy in design 04 §3.4.
        connect_timeout: Seconds allowed to establish the upstream connection.
        idle_timeout: Seconds a tunnel may sit with no traffic before being cut.
            Bounds the "open a connection and hold it" resource cost.
        tunnel_timeout: Hard ceiling on one tunnel's lifetime.
        max_header_bytes: Cap on the CONNECT request line plus headers. A client
            that sends more is not speaking the protocol.
        allow_local_grant_fallback: Use the in-process grant store when Redis is
            unavailable. DEVELOPMENT ONLY — with several replicas each would hold
            a different view of what is allowed. Off by default, so a production
            deployment that loses Redis denies rather than diverges.
        audit_allowed: Log successful connections too, not just denials. On by
            default: design 04 §3.4 asks for a full audit trail ("who, when,
            where, how many bytes"), and only recording refusals would leave
            exfiltration through an ALLOWED destination invisible.
    """

    host: str = "0.0.0.0"  # noqa: S104 — the Pod's netns plus NetworkPolicy is the boundary
    port: int = 3128
    connect_timeout: float = 10.0
    idle_timeout: float = 300.0
    tunnel_timeout: float = 3600.0
    max_header_bytes: int = 16 * 1024
    allow_local_grant_fallback: bool = False
    audit_allowed: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("EGRESS_PROXY_HOST", cls.host),
            port=_int_env("EGRESS_PROXY_PORT", cls.port),
            connect_timeout=float(
                _int_env("EGRESS_PROXY_CONNECT_TIMEOUT", int(cls.connect_timeout))
            ),
            idle_timeout=float(
                _int_env("EGRESS_PROXY_IDLE_TIMEOUT", int(cls.idle_timeout))
            ),
            tunnel_timeout=float(
                _int_env("EGRESS_PROXY_TUNNEL_TIMEOUT", int(cls.tunnel_timeout))
            ),
            max_header_bytes=_int_env(
                "EGRESS_PROXY_MAX_HEADER_BYTES", cls.max_header_bytes
            ),
            allow_local_grant_fallback=_bool_env(
                "EGRESS_PROXY_ALLOW_LOCAL_GRANTS", cls.allow_local_grant_fallback
            ),
            audit_allowed=_bool_env("EGRESS_PROXY_AUDIT_ALLOWED", cls.audit_allowed),
            log_level=os.getenv("EGRESS_PROXY_LOG_LEVEL", cls.log_level).upper(),
        )


settings = Settings.from_env()

__all__ = ["Settings", "settings"]
