"""Egress proxy entry point.

Deliberately minimal: bind, serve, shut down cleanly on a signal. There is no
health endpoint because there is no HTTP server to hang one on — liveness is "the
port accepts a connection", which is what a Kubernetes ``tcpSocket`` probe checks
without adding an unauthenticated surface to a process reachable from sandboxes.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from src.config import settings
from src.server import EgressProxy

logger = logging.getLogger("egress")


def _configure_logging() -> None:
    """Diagnostics to stderr; the audit trail owns stdout.

    Keeping them apart means a log-level change cannot suppress the audit trail,
    and a log shipper can treat the two streams differently.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))


async def serve() -> int:
    """Run until a termination signal arrives. Returns the process exit code."""
    proxy = EgressProxy()
    try:
        server = await proxy.start()
    except RuntimeError as exc:
        # A misconfiguration: fail loudly rather than run a proxy that denies
        # every request for a reason nobody can see.
        logger.error("egress_proxy_start_failed", extra={"error": str(exc)})
        return 78  # EX_CONFIG

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    async with server:
        await stopping.wait()

    logger.info("egress_proxy_stopping")
    await proxy.close()
    return 0


def cli() -> int:
    _configure_logging()
    try:
        return asyncio.run(serve())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(cli())
