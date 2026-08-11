"""Context Pool - lifecycle management for SQLMesh Context instances.

Strategy:
- One Context per (workspace_path, gateway) key
- Write operations acquire write_lock (exclusive, serialized)
- Read operations run concurrently (Context reads are thread-safe)
- TTL-based eviction for idle contexts
- Dirty flag for lazy refresh after file writes
- Invalidation after Git operations that change working tree
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from sqlmesh.core.config import Config
from sqlmesh.core.context import Context
from sqlmesh.utils.errors import ConfigError

from src.utils.errors import ContextConfigError, ContextLoadError, ContextRefreshError, ServiceError
from src.utils.error_archive import ErrorArchiveContext
from src.utils.logging import get_logger
from src.workspace.config_ops import ensure_state_isolation

logger = get_logger(__name__)


def _archive(
    svc_err: ServiceError,
    *,
    exc: Exception,
    workspace_path: str,
    operation: str,
    gateway: Optional[str] = None,
) -> None:
    """Persist a context error to ``{workspace}/errors/`` (best-effort).

    Thin wrapper around :func:`service_error_from_exception`'s archive hook
    for the manually-constructed ``ContextXxxError`` subclasses raised in
    this module. Sets ``svc_err.error_log_id`` so the API response can
    include it.
    """
    try:
        from src.utils.error_archive import ErrorArchive

        error_id = ErrorArchive.record(
            exc=exc,
            service_error=svc_err,
            archive_context=ErrorArchiveContext(
                workspace_path=workspace_path,
                operation=operation,
                gateway=gateway,
            ),
        )
        svc_err.error_log_id = error_id
    except Exception:
        logger.warning("context_error_archive_failed", exc_info=True)


@dataclass
class ContextEntry:
    """Cached SQLMesh Context with metadata and concurrency controls."""
    context: Context
    workspace_path: str
    gateway: Optional[str]
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dirty: bool = False  # True when workspace files changed since last refresh
    is_valid: bool = True


class ContextPool:
    """Lifecycle pool for SQLMesh Context instances.

    Provides cached Context access with:
    - Per-workspace write serialization via asyncio.Lock
    - Lazy refresh on dirty flag (set by file write, cleared before operations)
    - Explicit invalidation (Git checkout/pull/merge)
    - TTL-based eviction for idle contexts
    - LRU eviction when pool is at capacity
    """

    def __init__(
        self,
        max_size: int = 20,
        idle_ttl_seconds: int = 1800,
        max_age_seconds: int = 7200,
    ):
        self._pool: Dict[str, ContextEntry] = {}
        self._pool_lock = asyncio.Lock()
        self._max_size = max_size
        self._idle_ttl = idle_ttl_seconds
        self._max_age = max_age_seconds
        logger.info(
            "context_pool_initialized",
            max_size=max_size,
            idle_ttl=idle_ttl_seconds,
            max_age=max_age_seconds,
        )

    def _key(self, workspace_path: str, gateway: Optional[str] = None) -> str:
        """Generate pool key. Workspace paths are tenant-scoped by convention."""
        return f"{workspace_path}:{gateway or 'default'}"

    async def get_context(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        config: Optional[Config] = None,
    ) -> ContextEntry:
        """Get or create a Context for the given workspace.

        If the context is dirty (files modified since last refresh),
        it will be refreshed before returning.
        """
        key = self._key(workspace_path, gateway)

        async with self._pool_lock:
            entry = self._pool.get(key)

            if entry and not entry.is_valid:
                # Remove invalid entry
                await self._close_entry(entry)
                del self._pool[key]
                entry = None

            if not entry:
                # Evict LRU if at capacity
                if len(self._pool) >= self._max_size:
                    await self._evict_lru()
                entry = await self._create_entry(workspace_path, gateway, config)
                self._pool[key] = entry

        entry.last_accessed = time.time()
        return entry

    async def refresh_if_dirty(self, entry: ContextEntry) -> None:
        """Ensure the cached context reflects the current on-disk workspace.

        Two independent triggers force the cached context to reload so we never
        plan/lint/query against stale, previously-parsed models:

        1. The ``dirty`` flag (set by API file writes / git ops). These may ADD
           or REMOVE files, which mtime tracking alone cannot detect, so we run
           a full ``context.load()``.
        2. On-disk modifications to already-tracked files, detected by SQLMesh's
           own ``context.refresh()`` (compares each tracked file's ``st_mtime``
           against the value captured at load time). This catches EXTERNAL edits
           made via the native ``sqlmesh`` CLI, editors, or scripts that never
           went through our API and therefore never set the ``dirty`` flag —
           the exact cause of "stale until service restart" behavior.

        On failure the entry is marked invalid AND a ``ContextRefreshError`` is
        raised. Previously this method swallowed the exception and only set
        ``is_valid = False``, which caused subsequent operations to run against
        a context whose underlying DB transaction was already aborted (e.g.
        ``InFailedSqlTransaction``), producing a confusing secondary error
        instead of the actionable root cause. Raising immediately lets the
        caller return a clean, actionable error to the user.
        """
        try:
            if entry.dirty:
                # Full reload: also picks up newly added / removed files that
                # mtime tracking of existing files would miss.
                await asyncio.to_thread(entry.context.load)
                entry.dirty = False
                logger.info(
                    "context_refreshed",
                    workspace_path=entry.workspace_path,
                    reason="dirty_flag",
                )
            else:
                # Cheap mtime check; ``refresh()`` only reloads when a tracked
                # file actually changed on disk, otherwise it is a no-op.
                await asyncio.to_thread(entry.context.refresh)
        except Exception as e:
            # Mark invalid so the pool rebuilds the context on next access,
            # then surface the failure immediately so the caller can return
            # a clean, actionable error instead of triggering a downstream
            # "InFailedSqlTransaction"-style secondary failure.
            entry.is_valid = False
            logger.error(
                "context_refresh_failed",
                workspace_path=entry.workspace_path,
                error=str(e),
                exc_info=True,
            )
            svc_err = ContextRefreshError(entry.workspace_path, detail=str(e))
            _archive(
                svc_err,
                exc=e,
                workspace_path=entry.workspace_path,
                operation="context.refresh",
                gateway=entry.gateway,
            )
            raise svc_err from e

    def mark_dirty(self, workspace_path: str, gateway: Optional[str] = None) -> None:
        """Mark context as dirty (workspace files changed externally)."""
        key = self._key(workspace_path, gateway)
        entry = self._pool.get(key)
        if entry:
            entry.dirty = True

    async def mark_dirty_all_for_workspace(self, workspace_path: str) -> None:
        """Mark every cached context of a workspace dirty (all gateways).

        Used when workspace files change in a way that affects all gateway
        contexts (e.g. newly imported external-model YAML files). A dirty
        context triggers a full `context.load()` on next access, which
        re-globs the external_models directory and picks up new files that
        the incremental `refresh()` would otherwise miss.
        """
        prefix = f"{workspace_path}:"
        async with self._pool_lock:
            marked = 0
            for key, entry in self._pool.items():
                if key.startswith(prefix):
                    entry.dirty = True
                    marked += 1
        if marked:
            logger.info(
                "contexts_marked_dirty_for_workspace",
                workspace_path=workspace_path,
                count=marked,
            )

    def get_entry_if_cached(self, workspace_path: str, gateway: Optional[str] = None) -> Optional[ContextEntry]:
        """Get a cached context entry without creating one.

        Returns None if no context is cached for this workspace.
        Used for non-critical lookups (e.g. dialect detection for format_on_save).
        """
        key = self._key(workspace_path, gateway)
        entry = self._pool.get(key)
        if entry and entry.is_valid:
            return entry
        return None

    async def invalidate(self, workspace_path: str, gateway: Optional[str] = None) -> None:
        """Invalidate and remove cached context (after Git branch switch, etc.)."""
        key = self._key(workspace_path, gateway)
        async with self._pool_lock:
            entry = self._pool.pop(key, None)
            if entry:
                await self._close_entry(entry)
                logger.info("context_invalidated", key=key)

    async def invalidate_all_for_workspace(self, workspace_path: str) -> None:
        """Invalidate all contexts for a workspace (any gateway)."""
        async with self._pool_lock:
            keys_to_remove = [
                k for k in self._pool if k.startswith(f"{workspace_path}:")
            ]
            for key in keys_to_remove:
                entry = self._pool.pop(key)
                await self._close_entry(entry)
            if keys_to_remove:
                logger.info("contexts_invalidated_for_workspace",
                            workspace_path=workspace_path, count=len(keys_to_remove))

    async def _create_entry(
        self,
        workspace_path: str,
        gateway: Optional[str],
        config: Optional[Config],
    ) -> ContextEntry:
        """Create a new Context (expensive, ~1-2s)."""
        logger.info("creating_context", workspace_path=workspace_path, gateway=gateway)

        # Resolve the SQLMesh project root (the sql-warehouse/ sub-directory of
        # the workspace). The pool key and entry stay keyed by the workspace
        # ROOT so dirty/invalidate/archive remain consistent, while the actual
        # SQLMesh Context, state isolation and cache operate on sql-warehouse/.
        from src.workspace.layout import sqlmesh_root

        project_root = sqlmesh_root(workspace_path)

        # Guarantee workspace-scoped SQLMesh state isolation before loading, so
        # workspaces sharing one engine/database don't read each other's state
        # (snapshots / external models). When isolation is newly applied, drop
        # the local .cache so the context re-reads from the now workspace-
        # specific state schema instead of stale cross-workspace snapshots.
        try:
            if ensure_state_isolation(project_root):
                self._clear_workspace_cache(project_root)
        except Exception as e:  # never block context creation on maintenance
            logger.warning(
                "state_isolation_skipped", workspace_path=workspace_path, error=str(e)
            )

        kwargs: dict = {"paths": project_root, "load": True}
        if gateway:
            kwargs["gateway"] = gateway
        if config:
            kwargs["config"] = config

        try:
            context = await asyncio.to_thread(Context, **kwargs)
        except ConfigError as e:
            # Invalid/unloadable config.yaml (e.g. a gateway missing connection
            # credentials). Surface as a clean 4xx instead of a raw 500 that
            # would leak the SQLMesh traceback and config internals.
            logger.error(
                "context_config_invalid",
                workspace_path=workspace_path,
                gateway=gateway,
                error=str(e),
            )
            svc_err = ContextConfigError(workspace_path, detail=str(e))
            _archive(
                svc_err,
                exc=e,
                workspace_path=workspace_path,
                operation="context.config",
                gateway=gateway,
            )
            raise svc_err from e
        except Exception as e:
            # Any other failure during Context construction / load (e.g.
            # state-sync migration hitting a psycopg2 engine error such as
            # ``ObjectNotInPrerequisiteState``, a missing schema, or a
            # connection failure). Translate it into a structured
            # ``ContextLoadError`` so the global exception handler returns a
            # parseable body instead of FastAPI's default 500. The raw error
            # text is preserved as ``detail`` (DB errors do not carry
            # credentials), and ``raise ... from e`` keeps the cause chain
            # intact for server-side tracebacks.
            logger.error(
                "context_load_failed",
                workspace_path=workspace_path,
                gateway=gateway,
                error=str(e),
                exc_info=True,
            )
            svc_err = ContextLoadError(workspace_path, detail=str(e))
            _archive(
                svc_err,
                exc=e,
                workspace_path=workspace_path,
                operation="context.load",
                gateway=gateway,
            )
            raise svc_err from e
        return ContextEntry(
            context=context,
            workspace_path=workspace_path,
            gateway=gateway,
        )

    @staticmethod
    def _clear_workspace_cache(workspace_path: str) -> None:
        """Remove a workspace's local SQLMesh ``.cache`` (regenerable)."""
        import shutil

        cache_dir = Path(workspace_path) / ".cache"
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
            logger.info("workspace_cache_cleared", workspace_path=workspace_path)

    async def _close_entry(self, entry: ContextEntry) -> None:
        """Close a context entry's resources."""
        try:
            if hasattr(entry.context, "engine_adapter") and entry.context.engine_adapter:
                await asyncio.to_thread(entry.context.engine_adapter.close)
        except Exception:
            pass

    async def _evict_lru(self) -> None:
        """Evict the least recently used idle entry."""
        if not self._pool:
            return

        # Prefer entries that are not write-locked
        candidates = [
            (k, e) for k, e in self._pool.items()
            if not e.write_lock.locked()
        ]
        if not candidates:
            candidates = list(self._pool.items())

        # Sort by last accessed, evict oldest
        candidates.sort(key=lambda x: x[1].last_accessed)
        key, entry = candidates[0]
        del self._pool[key]
        await self._close_entry(entry)
        logger.info("context_evicted_lru", key=key)

    async def start_periodic_cleanup(self, interval_seconds: int = 300) -> None:
        """Background task to evict expired entries."""
        while True:
            await asyncio.sleep(interval_seconds)
            await self._evict_expired()

    async def _evict_expired(self) -> None:
        """Evict entries exceeding max age or idle TTL."""
        now = time.time()
        expired_keys = []
        async with self._pool_lock:
            for key, entry in self._pool.items():
                age = now - entry.created_at
                idle = now - entry.last_accessed
                if age > self._max_age or idle > self._idle_ttl:
                    if not entry.write_lock.locked():
                        expired_keys.append(key)
            for key in expired_keys:
                entry = self._pool.pop(key)
                await self._close_entry(entry)
        if expired_keys:
            logger.info("evicted_expired_contexts", count=len(expired_keys))

    def get_metrics(self) -> dict:
        """Return pool metrics for observability."""
        return {
            "size": len(self._pool),
            "max_size": self._max_size,
            "entries": [
                {
                    "key": k,
                    "workspace_path": e.workspace_path,
                    "gateway": e.gateway,
                    "last_accessed_ago_s": int(time.time() - e.last_accessed),
                    "dirty": e.dirty,
                    "write_locked": e.write_lock.locked(),
                }
                for k, e in self._pool.items()
            ],
        }

    async def shutdown(self) -> None:
        """Close all contexts on service shutdown."""
        async with self._pool_lock:
            for key, entry in self._pool.items():
                await self._close_entry(entry)
            self._pool.clear()
            logger.info("context_pool_shutdown")


# ---------------------------------------------------------------------------
# Lightweight config parsing & connectivity probing (no Context, no DB)
# ---------------------------------------------------------------------------

# All config file names SQLMesh recognizes for a project root.
_CONFIG_FILENAMES = ("config.yaml", "config.yml", "config.py")


def load_workspace_config(workspace_path: str) -> Config:
    """Parse a workspace's SQLMesh ``config.yaml`` WITHOUT creating a Context.

    Returns the fully-parsed :class:`~sqlmesh.core.config.Config`, which
    exposes ``gateways`` and ``default_gateway_name``. Crucially this does NOT
    open any database connection (unlike ``Context(load=True)`` which eagerly
    reads the state DB), so it is safe to call even when the target warehouse
    is unreachable.

    Raises ``ConfigError`` on malformed config.
    """
    from sqlmesh.core.config.loader import load_config_from_paths
    from src.workspace.layout import sqlmesh_root

    project_root = Path(sqlmesh_root(workspace_path))
    project_paths = [project_root / name for name in _CONFIG_FILENAMES]
    return load_config_from_paths(
        Config,
        project_paths=project_paths,
        config_name="config",
    )


def ping_gateway(workspace_path: str, gateway: Optional[str] = None) -> None:
    """Probe connectivity of a gateway's warehouse (raises on failure).

    Builds only the engine adapter for the requested gateway (from parsed
    config) and issues a lightweight ``ping``. No SQLMesh Context / state DB
    load is involved, so this fails fast on an unreachable warehouse instead
    of paying the full context-load cost.

    The caller is expected to wrap this in ``asyncio.to_thread`` +
    ``asyncio.wait_for`` to enforce an overall short timeout, since some
    drivers may not honor their own connect-timeout in every failure mode.
    """
    config = load_workspace_config(workspace_path)

    gw_name = gateway or config.default_gateway_name
    gw_config = config.gateways.get(gw_name) if gw_name else None
    if gw_config is None and config.gateways:
        # Fall back to the first configured gateway when none resolved.
        gw_config = next(iter(config.gateways.values()))
    if gw_config is None or gw_config.connection is None:
        raise ValueError(f"No connection configured for gateway '{gw_name or 'default'}'")

    adapter = gw_config.connection.create_engine_adapter()
    try:
        # ``ping`` runs a trivial round-trip (e.g. SELECT 1) and raises on any
        # connection/auth failure.
        adapter.ping()
    finally:
        try:
            adapter.close()
        except Exception:
            pass


# --- Singleton ---

_pool: Optional[ContextPool] = None


def get_context_pool() -> ContextPool:
    """Get the global ContextPool instance."""
    global _pool
    if _pool is None:
        raise RuntimeError("ContextPool not initialized. Call init_context_pool() first.")
    return _pool


def init_context_pool(**kwargs) -> ContextPool:
    """Initialize the global ContextPool singleton."""
    global _pool
    _pool = ContextPool(**kwargs)
    return _pool
