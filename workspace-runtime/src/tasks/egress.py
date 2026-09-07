"""Egress destinations of one working copy (design 04 §3.4, P6b).

A sandbox's allow-list is derived from what the WORKING COPY already declares, not
from a separate table an operator must maintain in parallel:

* the SQLMesh ``config.yaml`` gateways name the warehouses the project connects to;
* the Git remote names the server it pushes to.

Deriving rather than declaring is the point. A hand-maintained list would drift
from the project the moment a user added a gateway, and the drift would surface as
"my plan cannot reach the warehouse" — which an operator fixes by widening the
list, in the worst case to something permissive. Reading the project means the
allow-list is exactly as wide as the project needs, and narrows again when it does.

What is deliberately NOT here
-----------------------------
Credentials. The gateway block contains them (the user put them there, which Q5
accepted), and this module reads it — so every extracted value is passed through
the shared masking helper before it can reach a log, and only HOSTS leave this
module. Nothing that could authenticate travels into an :class:`EgressGrant`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from datacode_common.egress import (
    DEFAULT_ALLOWED_PORTS,
    WAREHOUSE_PORTS,
    EgressDestination,
    normalize_host,
)

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Attribute names SQLMesh connection configs use for the network location. Which
# one exists depends on the dialect (``host`` for Postgres/MySQL, ``account`` for
# Snowflake, ``server_hostname`` for Databricks, ...), so all are probed.
_HOST_ATTRIBUTES = (
    "host",
    "server_hostname",
    "account",
    "hostname",
    "endpoint",
)

# Suffix appended to a Snowflake account identifier, which is not a hostname on
# its own. Without this the destination would never match the real connection.
_SNOWFLAKE_SUFFIX = ".snowflakecomputing.com"

# Dialects whose connection is entirely local: no destination to allow, and
# emitting one would put a meaningless entry in the audit trail.
_LOCAL_DIALECTS = frozenset({"duckdb", "sqlite", "spark", ""})


@dataclass(frozen=True)
class WorkingCopyEgress:
    """Destinations derived from one working copy.

    Attributes:
        destinations: What the sandbox may reach.
        gateway: Gateway the warehouse entries came from, empty when the project
            default was used. Recorded so an operator reading a denial knows which
            gateway was consulted.
        notes: Human-readable provenance, one line per finding. Surfaced in logs
            rather than raised: a project with one unreadable gateway should still
            get the destinations of the others.
    """

    destinations: tuple[EgressDestination, ...] = ()
    gateway: str = ""
    notes: tuple[str, ...] = ()


def _host_of(connection: Any) -> str:
    """Extract the network host from a SQLMesh connection config.

    Returns an empty string for a local engine (DuckDB, SQLite) — those need no
    egress at all, and inventing a destination for them would be noise.
    """
    dialect = str(getattr(type(connection), "DIALECT", "") or "").lower()
    if dialect in _LOCAL_DIALECTS:
        return ""

    for attribute in _HOST_ATTRIBUTES:
        value = getattr(connection, attribute, None)
        if not value:
            continue
        host = normalize_host(str(value))
        if not host:
            continue
        if dialect == "snowflake" and not host.endswith(_SNOWFLAKE_SUFFIX):
            # An account identifier, not a hostname: allowing it verbatim would
            # never match the connection the driver actually opens.
            return f"{host}{_SNOWFLAKE_SUFFIX}"
        return host
    return ""


def _port_of(connection: Any) -> tuple[int, ...]:
    """Ports to allow for a connection.

    A configured port is used alone: narrowing to the one port a project actually
    uses is strictly better than allowing every port a dialect might use. Only when
    the config leaves it implicit do the dialect's common ports apply.
    """
    port = getattr(connection, "port", None)
    try:
        numeric = int(port) if port else 0
    except (TypeError, ValueError):
        numeric = 0
    if 0 < numeric <= 65535:
        return (numeric,)
    return WAREHOUSE_PORTS


def warehouse_destinations_of(
    config: Any, gateway: str = ""
) -> tuple[tuple[EgressDestination, ...], tuple[str, ...]]:
    """Derive warehouse destinations from a parsed SQLMesh config.

    Args:
        config: A parsed SQLMesh ``Config``.
        gateway: Restrict to this gateway. Empty means the project default —
            which is what a task without an explicit gateway will use, so the
            allow-list matches the connection that will actually be opened.

    Returns:
        The destinations, plus provenance notes for the log.

    Only ONE gateway's warehouse is allowed per task (Q5): a task that connects to
    the default gateway has no reason to reach a second warehouse, and permitting
    every configured gateway would widen the sandbox for no benefit.
    """
    notes: list[str] = []
    gateways = getattr(config, "gateways", None) or {}
    default_name = str(getattr(config, "default_gateway_name", "") or "")
    selected = gateway or default_name

    candidate = gateways.get(selected) if selected else None
    if candidate is None and not gateway:
        # A single unnamed gateway is a valid project shape.
        candidate = next(iter(gateways.values()), None)
    if candidate is None:
        notes.append(
            f"no gateway {selected!r} in the project; no warehouse destination"
        )
        return (), tuple(notes)

    connection = getattr(candidate, "connection", None)
    if connection is None:
        notes.append(f"gateway {selected!r} declares no connection")
        return (), tuple(notes)

    host = _host_of(connection)
    if not host:
        dialect = str(getattr(type(connection), "DIALECT", "") or "").lower()
        notes.append(
            f"gateway {selected!r} needs no egress"
            if dialect in _LOCAL_DIALECTS
            else f"gateway {selected!r} declares no reachable host"
        )
        return (), tuple(notes)

    ports = _port_of(connection)
    notes.append(f"gateway {selected!r} -> {host}:{','.join(str(p) for p in ports)}")
    return (
        (
            EgressDestination(
                host=host, ports=ports, kind="warehouse", note=f"gateway {selected}"
            ),
        ),
        tuple(notes),
    )


def git_destination(remote_url: str) -> Optional[EgressDestination]:
    """Derive the Git server destination from a remote URL.

    Handles both URL forms a remote can take: ``https://host/path`` and the SCP-like
    ``git@host:path``. The credential half of a URL is discarded here rather than
    masked, because only the host leaves this function at all.
    """
    candidate = (remote_url or "").strip()
    if not candidate:
        return None

    host = ""
    port: tuple[int, ...] = ()
    if "://" in candidate:
        parsed = urlparse(candidate)
        host = normalize_host(parsed.hostname or "")
        if parsed.port:
            port = (int(parsed.port),)
        elif parsed.scheme == "ssh":
            port = (22,)
    elif "@" in candidate and ":" in candidate:
        # git@github.com:org/repo.git
        _, _, remainder = candidate.partition("@")
        host = normalize_host(remainder.split(":", 1)[0])
        port = (22,)

    if not host:
        return None
    return EgressDestination(
        host=host, ports=port or DEFAULT_ALLOWED_PORTS, kind="git", note="git remote"
    )


async def resolve_working_copy_egress(
    workspace_path: str, *, gateway: str = "", include_git: bool = True
) -> WorkingCopyEgress:
    """Derive every destination one working copy legitimately needs.

    Never raises: a project whose config cannot be parsed yields an EMPTY list, and
    an empty list means "no egress". That is the correct direction — a task that
    then cannot reach its warehouse fails with a clear network error, whereas
    defaulting to a permissive list on a parse failure would silently open the gate
    exactly when the platform understands the project least.
    """
    notes: list[str] = []
    destinations: list[EgressDestination] = []
    selected_gateway = gateway

    try:
        from src.sqlmesh.config_access import load_workspace_config

        config = await asyncio.to_thread(load_workspace_config, workspace_path)
        warehouse, warehouse_notes = warehouse_destinations_of(config, gateway)
        destinations.extend(warehouse)
        notes.extend(warehouse_notes)
        selected_gateway = gateway or str(
            getattr(config, "default_gateway_name", "") or ""
        )
    except Exception as exc:  # noqa: BLE001 — a user project may fail any way
        # Deliberately not re-raised; see the docstring. Logged so the cause is
        # visible when a task reports "cannot reach the warehouse".
        logger.warning(
            "egress_warehouse_discovery_failed",
            workspace_path=workspace_path,
            gateway=gateway,
            error=str(exc),
        )
        notes.append(f"warehouse discovery failed: {exc}")

    if include_git:
        git = await _git_destination(workspace_path)
        if git is not None:
            destinations.append(git)
            notes.append(f"git remote -> {git.host}")

    logger.info(
        "egress_destinations_resolved",
        workspace_path=workspace_path,
        gateway=selected_gateway,
        # Hosts only. The gateway block this was read from contains credentials.
        hosts=[d.host for d in destinations],
        notes=list(notes),
    )
    return WorkingCopyEgress(
        destinations=_deduplicate(destinations),
        gateway=selected_gateway,
        notes=tuple(notes),
    )


async def _git_destination(workspace_path: str) -> Optional[EgressDestination]:
    """Read the working copy's ``origin`` and turn it into a destination.

    A working copy with no remote is entirely normal (a project that has not been
    pushed yet), so a failure here is a missing destination rather than an error.
    """
    try:
        from src.git.ops import run_git_async

        result = await run_git_async(
            workspace_path, ["remote", "get-url", "origin"]
        )
        url = result.stdout.strip() if result.returncode == 0 else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "egress_git_discovery_failed",
            workspace_path=workspace_path,
            error=str(exc),
        )
        return None
    return git_destination(url)


def _deduplicate(
    destinations: Iterable[EgressDestination],
) -> tuple[EgressDestination, ...]:
    """Collapse duplicates, merging the ports of same-host entries."""
    merged: dict[tuple[str, str], EgressDestination] = {}
    for destination in destinations:
        key = (destination.host, destination.kind)
        existing = merged.get(key)
        if existing is None:
            merged[key] = destination
            continue
        merged[key] = EgressDestination(
            host=existing.host,
            ports=tuple(sorted(set(existing.ports) | set(destination.ports))),
            kind=existing.kind,
            note=existing.note,
        )
    return tuple(merged.values())


__all__ = [
    "WorkingCopyEgress",
    "git_destination",
    "resolve_working_copy_egress",
    "warehouse_destinations_of",
]
