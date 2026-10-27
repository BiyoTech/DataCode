"""Entry point of the control-plane workspace syncer sidecar.

Run beside the Dagster webserver and daemon::

    python -m datacode_dagster_control_plane.main

Configuration is env-only; see :mod:`datacode_dagster_control_plane.config`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from datacode_dagster_control_plane.config import ConfigError, SyncerConfig
from datacode_dagster_control_plane.workspace_sync import WorkspaceSyncer

logger = logging.getLogger("datacode.dagster_control_plane")


def _setup_logging() -> None:
    """Plain stdout logging: the container runtime owns collection."""
    logging.basicConfig(
        level=os.getenv("DATACODE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


async def _run() -> None:
    """Run the syncer until a termination signal arrives."""
    config = SyncerConfig.from_env()
    syncer = WorkspaceSyncer(config)

    task = asyncio.create_task(syncer.run_forever())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Cancel the loop on shutdown so the current cycle finishes its write
        # instead of being killed mid-file.
        loop.add_signal_handler(sig, task.cancel)

    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> int:
    """Process entry point. Returns an exit code."""
    _setup_logging()
    try:
        asyncio.run(_run())
    except ConfigError as exc:
        # Misconfiguration must stop the container, not degrade into a syncer
        # that silently keeps the control plane's workspace empty.
        logger.error("Configuration error: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
