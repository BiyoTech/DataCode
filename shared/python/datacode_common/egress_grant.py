"""Egress grants — how a task's allow-list reaches the proxy.

The proxy must know which destinations a given task may reach. Three ways to tell
it, and the choice matters:

* Ask workspace-runtime at CONNECT time. Rejected: every outbound connection
  would then depend on the runtime being reachable, so a runtime restart would
  break running tasks' warehouse traffic.
* Put the list in the task token. Rejected: the list is unbounded (a workspace may
  declare many gateways) and a token is a header.
* **Publish the grant to the shared coordination backend when the task is
  submitted, keyed by task id, with the task's own TTL.** Chosen: the writer
  (runtime) already owns the workspace configuration, the reader (proxy) needs one
  cheap lookup, and expiry is automatic — there is no grant to clean up because a
  task's grant dies with the task.

Fail closed, twice over
-----------------------
A missing grant denies. An unreachable store denies. Both are outages of the
control path, and the safe answer to "I cannot tell what this task may reach" is
"nothing" — the opposite would turn a Redis blip into an open egress gate.

The grant deliberately carries NO credentials: it lists destinations, not how to
authenticate to them. Warehouse credentials travel to the sandbox in its own
per-task Secret and never through this store.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from datacode_common.egress import EgressDestination, normalize_host

logger = logging.getLogger(__name__)

# Redis key for one task's grant. Namespaced so it is greppable in a shared
# instance and cannot collide with the lease or event-bus keyspaces.
KEY_PREFIX = "eg:grant:"

# Key holding the bytes a task has already transferred. Separate from the grant
# because it is written on every tunnel close while the grant is immutable.
USAGE_PREFIX = "eg:used:"

# Default byte budget for one task. Generous enough for a real warehouse sync,
# small enough that exfiltrating a database is not a quiet background activity.
DEFAULT_BYTE_QUOTA = 5 * 1024 * 1024 * 1024  # 5 GiB

# Extra lifetime given to a grant beyond the task's timeout, so a task finishing
# its last transfer exactly on the deadline is not denied mid-flush.
GRANT_TTL_MARGIN_SECONDS = 120


def grant_key(task_id: str) -> str:
    return f"{KEY_PREFIX}{task_id}"


def usage_key(task_id: str) -> str:
    return f"{USAGE_PREFIX}{task_id}"


@dataclass(frozen=True)
class EgressGrant:
    """What one task is permitted to reach, and how much.

    Attributes:
        task_id: The task this grant belongs to. Also the store key.
        tenant_id: Owning tenant. Cross-checked against the task token's tenant,
            so a token/grant mismatch (a bug, or a replay) denies.
        destinations: Hosts and ports the task may reach.
        denied_cidrs: Ranges the task's isolation profile refuses, ON TOP of the
            module-level baseline. Carried in the grant because the profile is
            chosen by the runtime and the proxy has no way to know which one
            applied — without this the profile's ``network.denied_cidrs`` would be
            declared and never enforced.
        byte_quota: Total bytes (both directions) the task may transfer. 0 means
            :data:`DEFAULT_BYTE_QUOTA`, never "unlimited": an unlimited default is
            how data exfiltration goes unnoticed.
        expires_at: Unix seconds (UTC) after which the grant is void.
        isolation_profile: Profile applied, for the audit record.
        workspace_id: Owning workspace, for the audit record.
    """

    task_id: str
    tenant_id: str
    destinations: tuple[EgressDestination, ...] = ()
    denied_cidrs: tuple[str, ...] = ()
    byte_quota: int = 0
    expires_at: int = 0
    isolation_profile: str = ""
    workspace_id: str = ""

    @property
    def effective_quota(self) -> int:
        return self.byte_quota if self.byte_quota > 0 else DEFAULT_BYTE_QUOTA

    def is_expired(self, now: Optional[int] = None) -> bool:
        if not self.expires_at:
            return False
        return int(now if now is not None else time.time()) > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "isolation_profile": self.isolation_profile,
            "byte_quota": self.byte_quota,
            "expires_at": self.expires_at,
            "denied_cidrs": list(self.denied_cidrs),
            "destinations": [
                {
                    "host": d.host,
                    "ports": list(d.ports),
                    "kind": d.kind,
                    "note": d.note,
                }
                for d in self.destinations
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EgressGrant":
        raw = data.get("destinations") or ()
        destinations = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            host = normalize_host(str(item.get("host", "")))
            if not host:
                continue
            destinations.append(
                EgressDestination(
                    host=host,
                    ports=tuple(int(p) for p in (item.get("ports") or ())),
                    kind=str(item.get("kind", "")),
                    note=str(item.get("note", "")),
                )
            )
        return cls(
            task_id=str(data.get("task_id", "")),
            tenant_id=str(data.get("tenant_id", "")),
            destinations=tuple(destinations),
            denied_cidrs=tuple(str(c) for c in (data.get("denied_cidrs") or ())),
            byte_quota=int(data.get("byte_quota", 0) or 0),
            expires_at=int(data.get("expires_at", 0) or 0),
            isolation_profile=str(data.get("isolation_profile", "")),
            workspace_id=str(data.get("workspace_id", "")),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)

    @classmethod
    def from_json(cls, payload: str) -> "EgressGrant":
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("egress grant payload must be a JSON object")
        return cls.from_dict(data)


class EgressGrantUnavailable(RuntimeError):
    """The grant store could not be consulted. Callers must DENY, not allow."""


async def _shared_redis():
    """Return the platform's shared Redis client, or None when unusable.

    A missing dependency and an unreachable server both end in None — the caller
    denies either way — but they are logged DIFFERENTLY on purpose. An operator
    seeing "grant store unreachable" while Redis is plainly healthy has no way to
    guess that the real cause is a package the image never installed, and that
    exact confusion cost a debugging session during P6b's smoke test.
    """
    try:
        from datacode_common.redis.client import get_redis, redis_available
    except ImportError as exc:
        logger.error(
            "egress_grant_store_dependency_missing",
            extra={
                "error": str(exc),
                "hint": "install this service with the shared library's full "
                "dependencies (pydantic + pyyaml + redis); egress is DENIED "
                "until the grant store can be reached",
            },
        )
        return None

    try:
        if not await redis_available():
            return None
        return get_redis()
    except Exception as exc:  # noqa: BLE001 — connection errors take many shapes
        logger.warning("egress_grant_store_unavailable", extra={"error": str(exc)})
        return None


@dataclass
class EgressGrantStore:
    """Publishes and reads task grants.

    Backed by the shared Redis when available. An in-memory fallback exists ONLY
    so a single-process test or a single-node dev run works; it is not a
    distributed fallback, because two processes sharing nothing would each believe
    a different allow-list. :attr:`allow_local_fallback` makes that choice
    explicit at the call site rather than implicit in a try/except.

    Attributes:
        allow_local_fallback: Permit the in-process store when no shared one is
            reachable. DEVELOPMENT ONLY.
        client_factory: Async callable returning the backing client, or None when
            there is none. Injectable so a test can exercise BOTH branches
            deterministically — without it, a suite running next to a live Redis
            silently tests the wrong one.
    """

    allow_local_fallback: bool = False
    client_factory: Callable[[], Awaitable[Any]] = _shared_redis
    _local: dict[str, str] = field(default_factory=dict, repr=False)
    _local_usage: dict[str, int] = field(default_factory=dict, repr=False)

    async def _client(self):
        return await self.client_factory()

    # ── writing (workspace-runtime) ────────────────────────────────────

    async def publish(self, grant: EgressGrant, *, ttl_seconds: int) -> None:
        """Store a grant for ``ttl_seconds``.

        Raises:
            EgressGrantUnavailable: The grant could not be stored. The caller must
                treat this as a submission failure: launching a sandbox whose
                egress cannot be authorized would give it no network at all, and
                reporting that as a task failure later would misplace the cause.
        """
        ttl = max(int(ttl_seconds) + GRANT_TTL_MARGIN_SECONDS, 1)
        payload = grant.to_json()

        client = await self._client()
        if client is not None:
            try:
                await client.set(grant_key(grant.task_id), payload, ex=ttl)
                logger.info(
                    "egress_grant_published",
                    extra={
                        "task_id": grant.task_id,
                        "tenant_id": grant.tenant_id,
                        "destinations": len(grant.destinations),
                        "ttl_seconds": ttl,
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "egress_grant_publish_failed",
                    extra={"task_id": grant.task_id, "error": str(exc)},
                )
                if not self.allow_local_fallback:
                    raise EgressGrantUnavailable(str(exc)) from exc

        if not self.allow_local_fallback:
            raise EgressGrantUnavailable(
                "no shared grant store: the egress proxy could not be told what "
                "this task may reach. Enable Redis, or run with the local "
                "fallback in single-process development only."
            )
        self._local[grant_key(grant.task_id)] = payload

    async def revoke(self, task_id: str) -> None:
        """Delete a grant early. Best-effort: the TTL is the real guarantee."""
        client = await self._client()
        if client is not None:
            try:
                await client.delete(grant_key(task_id), usage_key(task_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "egress_grant_revoke_failed",
                    extra={"task_id": task_id, "error": str(exc)},
                )
        self._local.pop(grant_key(task_id), None)
        self._local_usage.pop(usage_key(task_id), None)

    # ── reading (egress-proxy) ─────────────────────────────────────────

    async def fetch(self, task_id: str) -> Optional[EgressGrant]:
        """Return the grant for ``task_id``, or None when there is none.

        Raises:
            EgressGrantUnavailable: The store could not be consulted at all. A
                DISTINCT outcome from "no grant": the first is an outage the
                operator must see, the second is an ordinary denial.
        """
        key = grant_key(task_id)
        client = await self._client()
        if client is not None:
            try:
                raw = await client.get(key)
            except Exception as exc:  # noqa: BLE001
                raise EgressGrantUnavailable(str(exc)) from exc
            if raw is None:
                return None
            return self._parse(task_id, raw)

        if not self.allow_local_fallback:
            raise EgressGrantUnavailable(
                "grant store is unreachable; denying egress rather than guessing"
            )
        raw = self._local.get(key)
        return self._parse(task_id, raw) if raw is not None else None

    @staticmethod
    def _parse(task_id: str, raw: Any) -> Optional[EgressGrant]:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        try:
            return EgressGrant.from_json(text)
        except (ValueError, TypeError) as exc:
            # Corrupt entry: deny by returning None rather than raising, so one
            # bad key cannot look like a store outage.
            logger.error(
                "egress_grant_corrupt", extra={"task_id": task_id, "error": str(exc)}
            )
            return None

    # ── byte accounting ────────────────────────────────────────────────

    async def add_usage(self, task_id: str, transferred: int, *, ttl_seconds: int) -> int:
        """Add ``transferred`` bytes to a task's total and return the new total.

        Counted AFTER a tunnel closes rather than streamed live: the goal is to
        stop the next connection of an abusive task, and per-chunk accounting
        would put a Redis round trip in the data path.

        Failures return the increment itself, so accounting loss degrades the
        quota's precision without failing the transfer that already happened.
        """
        if transferred <= 0:
            return 0
        key = usage_key(task_id)
        ttl = max(int(ttl_seconds) + GRANT_TTL_MARGIN_SECONDS, 1)

        client = await self._client()
        if client is not None:
            try:
                total = int(await client.incrby(key, int(transferred)))
                await client.expire(key, ttl)
                return total
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "egress_usage_update_failed",
                    extra={"task_id": task_id, "error": str(exc)},
                )
                return int(transferred)

        self._local_usage[key] = self._local_usage.get(key, 0) + int(transferred)
        return self._local_usage[key]

    async def usage(self, task_id: str) -> int:
        """Bytes a task has transferred so far. 0 when unknown.

        Unknown counts as zero on purpose: a lost counter must not deny a task
        that never used its budget. The quota bounds abuse; it is not an
        authorization control, and the allow-list is what actually contains a
        compromised sandbox.
        """
        key = usage_key(task_id)
        client = await self._client()
        if client is not None:
            try:
                raw = await client.get(key)
                return int(raw) if raw is not None else 0
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "egress_usage_read_failed",
                    extra={"task_id": task_id, "error": str(exc)},
                )
                return 0
        return self._local_usage.get(key, 0)


def warehouse_destinations(
    hosts: Sequence[str], ports: Sequence[int] = ()
) -> tuple[EgressDestination, ...]:
    """Build warehouse destinations from resolved host names.

    A small helper rather than inline construction because the ``kind`` string is
    what a profile's ``allowed_destinations`` matches on, and a typo there would
    silently make a destination invisible.
    """
    from datacode_common.egress import WAREHOUSE_PORTS

    allowed = tuple(int(p) for p in ports) or WAREHOUSE_PORTS
    return tuple(
        EgressDestination(host=normalize_host(h), ports=allowed, kind="warehouse")
        for h in hosts
        if normalize_host(h)
    )


__all__ = [
    "DEFAULT_BYTE_QUOTA",
    "GRANT_TTL_MARGIN_SECONDS",
    "KEY_PREFIX",
    "USAGE_PREFIX",
    "EgressGrant",
    "EgressGrantStore",
    "EgressGrantUnavailable",
    "grant_key",
    "usage_key",
    "warehouse_destinations",
]
