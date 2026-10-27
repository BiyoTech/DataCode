"""Lease wiring — builds the LeaseManager from configuration and the database.

Kept separate from :mod:`src.lease.manager` on purpose: the manager takes its
repository source as a callback so the lease pipeline can be tested without a
database, and that property disappears the moment the manager itself imports a
repository. This module is where the two meet, and the only place that knows
both.

The lease subsystem is OPTIONAL at this stage (``lease.enabled``). When it is
off, :func:`get_lease_manager` returns ``None`` and the service behaves exactly
as it did before P4a — requests reach any replica and read the working copy from
the shared volume. The storage contract (design 07 ADR-003 Q7) is what keeps
that combination honest: a replica-private medium with leases disabled is
refused at startup.
"""

from __future__ import annotations

import typing as t

from datacode_common.naming import split_lease_key

from src.config import settings
from src.git.ops import GitAuth
from src.lease.audit import get_lease_auditor
from src.lease.heartbeat import HeartbeatReporter
from src.lease.holder import LeaseHolder, runtime_id
from src.lease.manager import LeaseManager, RepositorySource
from src.lease.reclaimer import DiskReclaimer
from src.lease.server import LeaseServer, build_lease_server
from src.utils.logging import get_logger

logger = get_logger(__name__)

_manager: t.Optional[LeaseManager] = None
_server: t.Optional[LeaseServer] = None
_reclaimer: t.Optional[DiskReclaimer] = None


async def resolve_repository_source(lease_key: str) -> RepositorySource:
    """Look up where a workspace's code lives, and how to authenticate.

    Failures are returned as an EMPTY source rather than raised: an already
    materialized working copy does not need a remote, so a database hiccup must
    not make a warm workspace unservable. Materialization itself reports the
    missing URL if it turns out one was required.
    """
    _tenant_id, workspace_id, user_id, _environment = split_lease_key(lease_key)

    try:
        from src.workspace.repository import (
            UserGitCredentialRepository,
            WorkspaceRepository,
        )

        workspace = await WorkspaceRepository().get_by_id(workspace_id)
        repo_url = getattr(workspace, "git_repo_url", "") or ""

        auth: t.Optional[GitAuth] = None
        credential = await UserGitCredentialRepository().get_default(user_id)
        if credential is not None and getattr(credential, "token", ""):
            auth = GitAuth(
                token=credential.token,
                provider=getattr(credential, "provider", "") or "",
                username=getattr(credential, "username", "") or "",
            )
    except Exception as exc:  # noqa: BLE001 - a warm copy must stay servable
        logger.warning(
            "lease_repository_source_unavailable",
            lease_key=lease_key,
            error=str(exc),
        )
        return RepositorySource(repo_url="")

    return RepositorySource(repo_url=repo_url, auth=auth)


def _advertised_addr() -> str:
    """Address the gateway should dial to reach this replica directly.

    Routing to a specific pod is the whole point of a lease, so a wildcard bind
    address is useless here; the deployment must provide a concrete one.
    """
    host = settings.code_location_advertise_host or runtime_id()
    return f"{host}:{settings.rest_port}"


async def init_lease_manager() -> t.Optional[LeaseManager]:
    """Build and start the lease manager, or return ``None`` when disabled."""
    global _manager

    if not settings.lease_enabled:
        logger.info("lease_subsystem_disabled")
        return None
    if _manager is not None:
        return _manager

    from datacode_common.redis import get_redis

    client = get_redis()
    if client is None:
        # Fail loudly: leases were explicitly enabled, and without coordination
        # the single-writer invariant they promise cannot be upheld. Silently
        # degrading here would leave the storage contract's guarantee false.
        raise RuntimeError(
            "lease.enabled is true but Redis is unavailable; workspace leases "
            "require a coordination backend (set redis.enabled or disable leases)"
        )

    holder = LeaseHolder(
        client=client,
        materialize_root=settings.materialize_root,
        runtime_addr=_advertised_addr(),
        ttl_seconds=settings.lease_ttl_seconds,
    )
    manager = LeaseManager(
        holder=holder,
        materialize_root=settings.materialize_root,
        source_provider=resolve_repository_source,
        wip_interval_seconds=settings.wip_snapshot_interval_seconds,
        max_leases=settings.lease_max_per_instance,
        admission_high_watermark=settings.lease_admission_rss_high_watermark,
        memory_ratio_provider=_memory_used_ratio,
        auditor=get_lease_auditor(),
    )
    heartbeat = HeartbeatReporter(
        client=client,
        runtime_id=holder.runtime_id,
        addr=_advertised_addr(),
        max_leases=settings.lease_max_per_instance,
        load_provider=lambda: (
            len(manager.served_keys()),
            _memory_used_ratio(),
            manager.draining,
            manager.served_keys(),
        ),
    )
    manager.attach_heartbeat(heartbeat)
    await manager.start()

    # Disk reclamation (design 06 4b): bounds the working-copy cache on
    # replica-private media. Started with the manager because its victim set
    # is defined by the served leases.
    reclaimer = DiskReclaimer(
        materialize_root=settings.materialize_root,
        served_keys=manager.served_keys,
        last_used=manager.last_used,
        high_watermark_percent=settings.materialize_disk_high_watermark_percent,
        low_watermark_percent=settings.materialize_disk_low_watermark_percent,
        interval_seconds=settings.materialize_reclaim_interval_seconds,
    )
    await reclaimer.start()
    global _reclaimer
    _reclaimer = reclaimer

    _manager = manager
    # The acquire channel for the gateway. Needs the SAME Redis client the
    # holder uses: placement reads the heartbeats the reporter writes.
    global _server
    _server = build_lease_server(manager, client)
    logger.info(
        "lease_subsystem_started",
        runtime_id=holder.runtime_id,
        addr=_advertised_addr(),
        max_leases=settings.lease_max_per_instance,
        acquire_endpoint=_server is not None,
    )
    return manager


def get_lease_manager() -> t.Optional[LeaseManager]:
    """The running lease manager, or ``None`` when the subsystem is off."""
    return _manager


def get_lease_server() -> t.Optional[LeaseServer]:
    """The running acquire server, or ``None`` (subsystem off / no token)."""
    return _server


async def shutdown_lease_manager() -> None:
    """Drain leases gracefully — the zero-loss shutdown path."""
    global _manager, _server, _reclaimer
    # The reclaimer stops first: it must not race the drain by discarding a
    # tree the final snapshot pass is about to read.
    if _reclaimer is not None:
        await _reclaimer.stop()
        _reclaimer = None
    _server = None
    if _manager is None:
        return
    await _manager.drain()
    _manager = None


async def begin_controller_drain() -> dict:
    """Controller-invoked drain (P8 capacity orchestration).

    Announces draining in the heartbeat synchronously (so the answer implies
    "placement already avoids me"), then runs the drain itself in the
    background: a large repository's final snapshots may take minutes, and the
    controller polls the heartbeat rather than holding this request open.
    """
    import asyncio

    manager = get_lease_manager()
    if manager is None:
        return {"draining": False, "reason": "lease subsystem not running"}

    async def _drain_in_background() -> None:
        try:
            await manager.begin_drain()
        except Exception as exc:  # noqa: BLE001 — logged, not propagated
            logger.error("controller_drain_failed", error=str(exc))

    task = asyncio.create_task(_drain_in_background(), name="controller-drain")
    task.add_done_callback(
        lambda t: None if t.cancelled() else (
            logger.error("controller_drain_task_failed", error=str(t.exception()))
            if t.exception() is not None else None
        )
    )
    return {"draining": True, "runtime_id": runtime_id()}


def _memory_used_ratio() -> float:
    """Fraction of this pod's memory budget in use.

    Read from the cgroup rather than from the host: inside a container the
    host's total memory is irrelevant, and using it would let an instance accept
    leases until the kernel OOM-kills it. Returns 0.0 when the cgroup files are
    absent (dev on macOS), where the count limit is the only guard.
    """
    for current, limit in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        (
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ),
    ):
        try:
            with open(current) as handle:
                used = int(handle.read().strip())
            with open(limit) as handle:
                raw = handle.read().strip()
            if raw == "max":
                return 0.0
            total = int(raw)
        except (OSError, ValueError):
            continue
        if total > 0:
            return min(used / total, 1.0)
    return 0.0
