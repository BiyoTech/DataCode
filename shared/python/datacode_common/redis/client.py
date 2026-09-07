"""Shared async Redis client.

Every Redis-backed coordination primitive in the platform (rate limiting,
SQLMesh environment locks, the SSE event bus) goes through this module rather
than opening its own connection. Two reasons:

1. **One pool per process.** A connection pool per domain would multiply
   sockets against the same instance for no benefit, and would make the "is
   Redis reachable" question have several different answers at once.
2. **One availability decision.** Redis is a coordination backend, not a
   datastore: when it is unreachable the services must keep serving requests
   with their in-process fallbacks. That degraded mode is only safe to reason
   about if every caller observes the same verdict, which
   :func:`redis_available` provides.

Usage::

    from datacode_common.redis.client import get_redis, redis_available

    if await redis_available():
        client = get_redis()
        await client.set(key, value)
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

from datacode_common.config.platform import RedisConfig, platform_config

logger = logging.getLogger(__name__)

# One client PER EVENT LOOP, keyed weakly: redis-py's connection pool binds to
# the loop that created it, so a client reused on a second loop fails with
# "Event loop is closed". A service has one loop for its lifetime (the cache
# then behaves as a singleton); test suites, which run one loop per test, get
# a fresh client each time and never touch a dead pool.
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Redis]" = (
    weakref.WeakKeyDictionary()
)


def redis_config() -> RedisConfig:
    """Return the platform-level Redis configuration."""
    return platform_config().redis


def get_redis() -> Optional[Redis]:
    """Return the async Redis client for the RUNNING event loop.

    Returns ``None`` when Redis is disabled in ``platform.yaml``, which callers
    must read as "run the in-process fallback". Creating the client does not
    open a socket (redis-py connects lazily), so this never blocks; use
    :func:`redis_available` when reachability actually matters.

    Must be called from a running loop; outside one (sync startup code probing
    availability) there is nothing sensible to return, so ``None`` is returned
    and the caller falls back.
    """
    cfg = redis_config()
    if not cfg.enabled:
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    client = _clients.get(loop)
    if client is None:
        client = Redis.from_url(
            cfg.url(),
            # Responses are decoded to str: every value the platform stores is
            # textual (counters, lock owners, JSON events), and decoding once
            # here keeps call sites from repeating .decode() everywhere.
            decode_responses=True,
            socket_connect_timeout=cfg.connect_timeout_seconds,
            socket_timeout=cfg.operation_timeout_seconds,
            max_connections=cfg.max_connections,
            health_check_interval=30,
        )
        _clients[loop] = client
        logger.info(
            "redis_client_created url=%s key_prefix=%s max_connections=%d",
            cfg.safe_url(),
            cfg.key_prefix,
            cfg.max_connections,
        )
    return client


async def redis_available() -> bool:
    """Report whether Redis answers a PING right now.

    Deliberately not cached: a cached "unavailable" would keep a recovered
    instance out of service, and a cached "available" would hide an outage
    until the next failing command. The call is a single round trip bounded by
    ``operation_timeout_seconds``.
    """
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except (RedisError, OSError) as exc:
        logger.warning("redis_ping_failed url=%s error=%s", redis_config().safe_url(), exc)
        return False


def redis_key(*segments: str) -> str:
    """Build a fully namespaced key from *segments*."""
    return redis_config().key(*segments)


async def close_redis() -> None:
    """Close the current loop's client and drop it from the cache.

    Called from service shutdown hooks so a reload does not leak the pool.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    client = _clients.pop(loop, None)
    if client is None:
        return
    try:
        await client.aclose()
    except (RedisError, OSError) as exc:
        logger.warning("redis_close_failed error=%s", exc)
    else:
        logger.info("redis_client_closed")
