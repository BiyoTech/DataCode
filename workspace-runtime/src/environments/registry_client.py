"""The runtime's client for registry writes only the control plane may perform.

``environments`` and ``environment_events`` are control-service's tables. This
runtime authors two facts about them — the watermark a successful apply leaves,
and the audit row a merge produces — because both originate here, on the
replica holding the working copy. It sends them instead of writing them: two
services writing one table is the ownership break every bug of that family in
this platform started from.

Reads stay direct (``src/environments/repository.py``): they are on the plan /
apply hot path, tolerate a row that is a moment stale, and cannot corrupt
anything. The asymmetry is the point, and ``tests/test_control_owned_tables_readonly.py``
is what keeps it from eroding.

Failure policy
--------------
Both calls are bookkeeping ABOUT an operation that already happened — the data
was applied, the branches were merged. Failing the business operation because
its record could not be written would hide a completed action, so every method
returns a boolean and never raises. An unconfigured channel is the same case:
it logs once per call and reports False.
"""

from __future__ import annotations

import typing as t
from datetime import datetime

import httpx
from datacode_common.security.internal_token import INTERNAL_TOKEN_HEADER

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

_PREFIX = "/internal/env-registry"
# Short on purpose: a slow control plane must not hold an apply's response
# open, and the write is already best-effort.
_TIMEOUT_SECONDS = 10.0


class EnvironmentRegistryClient:
    """Sends registry writes to the control plane. Never raises."""

    def __init__(
        self,
        *,
        base_url: str = "",
        token: str = "",
        transport: t.Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = (base_url or settings.control_service_url or "").rstrip("/")
        self._token = token or settings.internal_token
        self._transport = transport

    async def mark_applied(
        self,
        environment_id: str,
        *,
        commit_hash: str,
        dirty: bool = False,
        applied_at: t.Optional[datetime] = None,
    ) -> bool:
        """Record a successful apply's watermark. True when it was stored."""
        payload: dict = {
            "environment_id": environment_id,
            "commit_hash": commit_hash or "",
            "dirty": dirty,
        }
        if applied_at is not None:
            payload["applied_at"] = applied_at.isoformat()
        body = await self._post("/applied", payload, event="environment_applied_marker")
        return bool(body and body.get("updated"))

    async def record_event(
        self,
        *,
        environment_id: str,
        action: str,
        actor_user_id: str = "",
        trigger_type: str = "human",
        succeeded: bool = True,
        commit_hash: str = "",
        detail: t.Optional[dict] = None,
        duration_ms: int = 0,
    ) -> bool:
        """Append one lifecycle event. True when it was stored."""
        body = await self._post(
            "/events",
            {
                "environment_id": environment_id,
                "action": action,
                "actor_user_id": actor_user_id,
                "trigger_type": trigger_type,
                "succeeded": succeeded,
                "commit_hash": commit_hash,
                "detail": detail,
                "duration_ms": duration_ms,
            },
            event="environment_event",
        )
        return bool(body and body.get("event_id"))

    # ── internals ──────────────────────────────────────────────────────

    async def _post(self, path: str, payload: dict, *, event: str) -> t.Optional[dict]:
        """POST and return the body, or ``None`` with a warning."""
        if not self._base_url or not self._token:
            logger.warning(
                f"{event}_not_recorded",
                reason="control plane channel is not configured",
                hint="set WORKSPACE_RUNTIME_CONTROL_SERVICE_URL and the internal token",
            )
            return None
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}{_PREFIX}{path}",
                    json=payload,
                    headers={INTERNAL_TOKEN_HEADER: self._token},
                )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not raise
            logger.warning(f"{event}_not_recorded", path=path, error=str(exc))
            return None


_client: t.Optional[EnvironmentRegistryClient] = None


def get_registry_client() -> EnvironmentRegistryClient:
    """Get or create the process-wide client."""
    global _client
    if _client is None:
        _client = EnvironmentRegistryClient()
    return _client


def set_registry_client(client: t.Optional[EnvironmentRegistryClient]) -> None:
    """Replace the process-wide client (tests)."""
    global _client
    _client = client
