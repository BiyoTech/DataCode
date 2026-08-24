"""Run lifecycle orchestration.

Thin coordination layer on top of :class:`DagsterGraphQLClient`. Keeps the REST
layer declarative by owning multi-step flows (launch, poll-to-completion, log
retrieval) and normalizing status values. All timestamps returned by Dagster
are epoch seconds (UTC); conversion to local time is a frontend concern.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from src.core.dagster_client import DagsterGraphQLClient, get_dagster_client
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Dagster run statuses considered terminal (no further transitions expected).
_TERMINAL_STATUSES = {"SUCCESS", "FAILURE", "CANCELED"}


class RunManager:
    """Coordinates run launch / status / logs / cancellation."""

    def __init__(self, client: Optional[DagsterGraphQLClient] = None):
        self._client = client or get_dagster_client()

    async def launch(
        self,
        *,
        repository_location_name: str,
        repository_name: str,
        job_name: str,
        run_config: Optional[dict[str, Any]] = None,
        tags: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Launch a job run and return ``{run_id, status}``.

        ``tags`` carry the deployment attribution (environment, workspace,
        deployment). They are the only thing separating one environment's runs
        from another's in the shared open-source run store, so they are logged
        with the launch as well.
        """
        result = await self._client.launch_run(
            repository_location_name=repository_location_name,
            repository_name=repository_name,
            job_name=job_name,
            run_config=run_config,
            tags=tags,
        )
        logger.info(
            "run_launched",
            run_id=result["run_id"],
            job_name=job_name,
            location=repository_location_name,
            tags=tags or {},
        )
        return result

    async def get_status(self, run_id: str) -> dict[str, Any]:
        return await self._client.get_run_status(run_id)

    async def get_logs(self, run_id: str) -> list[dict[str, Any]]:
        return await self._client.get_run_logs(run_id)

    async def cancel(self, run_id: str) -> dict[str, Any]:
        result = await self._client.cancel_run(run_id)
        logger.info("run_canceled", run_id=run_id, status=result.get("status"))
        return result

    async def wait_for_completion(
        self, run_id: str, *, poll_interval: float = 2.0, timeout: float = 600.0
    ) -> dict[str, Any]:
        """Poll a run until it reaches a terminal status or ``timeout`` elapses.

        Returns the final status dict. Raises ``asyncio.TimeoutError`` if the
        run does not finish within ``timeout`` seconds.
        """
        elapsed = 0.0
        while True:
            status = await self._client.get_run_status(run_id)
            if status["status"] in _TERMINAL_STATUSES:
                logger.info("run_completed", run_id=run_id, status=status["status"])
                return status
            if elapsed >= timeout:
                raise asyncio.TimeoutError(f"Run {run_id} did not finish within {timeout}s")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval


_run_manager: Optional[RunManager] = None


def get_run_manager() -> RunManager:
    """Return the process-wide run manager singleton."""
    global _run_manager
    if _run_manager is None:
        _run_manager = RunManager()
    return _run_manager
