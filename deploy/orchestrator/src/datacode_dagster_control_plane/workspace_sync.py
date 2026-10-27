"""Control-plane workspace syncer: the code location registration channel.

## The problem

A Code Location gRPC server is always local to a dagster-service (data plane)
replica, because it loads user Python code from that replica's filesystem. The
control plane — webserver + daemon + metadata DB — can only drive a location
whose ``host:port`` it knows. Its ``workspace.yaml`` is the only place to state
that, and only the data plane knows what it runs.

## The mechanism

This module is the single WRITER of the control plane's workspace file:

1. poll every data plane replica's ``GET /internal/code-locations``,
2. merge the snapshots into one ``load_from`` list of ``grpc_server`` entries,
3. write the file atomically **only when the content changed**,
4. ask the webserver to ``reloadWorkspace``,
5. acknowledge the loaded revisions back to each replica via
   ``POST /internal/code-locations/heartbeat``.

The daemon needs no step 4: it re-reads the workspace file on its own 60s cycle
(``dagster._daemon.controller.RELOAD_WORKSPACE_INTERVAL``). The webserver only
reads the file at startup and on that mutation, hence the explicit reload.

## Why a single control-plane-side writer

The alternative — each data plane replica writing the control plane's file — needs
a distributed lock, a volume shared across the plane boundary, and a conflict
story for what is derived state. One writer polling N publishers has none of
that, and a replica that dies simply stops being advertised.

## Why not a ``python_module`` workspace target

``load_from: python_module`` produces a ``ManagedGrpcPythonEnvCodeLocationOrigin``:
the control plane SPAWNS a subprocess to import that module and expects a
``Definitions`` object from it. It cannot express "these are remote gRPC
endpoints", and the user code it would need is on the data plane's disk, not
here. Rendering ``grpc_server`` entries is the only workspace target that
describes a remote code location.

## Failure behavior

A replica that cannot be reached is SKIPPED, and its locations are dropped from
the rendered file — but only after ``failure_tolerance`` consecutive failures.
Reacting to the first timeout would unregister a whole replica's deployments
because of one slow response; never reacting would keep dialing endpoints that
are gone. The tolerance is that trade-off made explicit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml

from datacode_dagster_control_plane.config import SyncerConfig

logger = logging.getLogger(__name__)

# Consecutive poll failures tolerated before a replica's locations are dropped.
DEFAULT_FAILURE_TOLERANCE = 3

_RELOAD_WORKSPACE_MUTATION = """
mutation ReloadWorkspace {
  reloadWorkspace {
    __typename
    ... on Workspace {
      locationEntries {
        name
        loadStatus
        locationOrLoadError { __typename ... on PythonError { message } }
      }
    }
    ... on PythonError { message }
  }
}
"""


@dataclass
class _Advertisement:
    """One code location as published by a data plane replica."""

    location_name: str
    host: str
    port: int
    revision: int
    replica_url: str

    @classmethod
    def parse(cls, raw: dict[str, Any], replica_url: str) -> "_Advertisement | None":
        """Build from a payload entry, or None when it is unusable.

        Validated rather than trusted: a malformed entry that reaches the
        workspace file breaks the load for every OTHER location in it, so a bad
        entry is dropped with a log line instead.
        """
        name = str(raw.get("location_name") or "").strip()
        host = str(raw.get("host") or "").strip()
        try:
            port = int(raw.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not name or not host or not (0 < port < 65536):
            logger.warning(
                "Dropping malformed code location advertisement from %s: %r",
                replica_url,
                raw,
            )
            return None
        try:
            revision = int(raw.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        return cls(
            location_name=name,
            host=host,
            port=port,
            revision=revision,
            replica_url=replica_url,
        )


@dataclass
class _ReplicaHealth:
    """Consecutive-failure counter of one replica, for drop tolerance."""

    consecutive_failures: int = 0
    # Last successfully fetched advertisements, served while within tolerance.
    last_good: list[_Advertisement] = field(default_factory=list)


class WorkspaceSyncer:
    """Keeps the control plane's workspace file in step with the data plane."""

    def __init__(
        self,
        config: SyncerConfig,
        *,
        failure_tolerance: int = DEFAULT_FAILURE_TOLERANCE,
    ) -> None:
        self._config = config
        self._failure_tolerance = max(1, failure_tolerance)
        self._health: dict[str, _ReplicaHealth] = {
            url: _ReplicaHealth() for url in config.replica_urls
        }
        # Content of the file as this process last wrote it, so an unchanged
        # topology costs one poll and no reload. Reloading a workspace evicts
        # every location's cached definitions, so doing it needlessly every
        # interval would keep the control plane permanently re-importing.
        self._last_rendered: str | None = None

    # --- Public API ----------------------------------------------------------

    async def sync_once(self) -> dict[str, Any]:
        """Run one full cycle. Returns a summary for logging and tests."""
        per_replica = await self._collect()
        advertisements = self._merge(per_replica)

        rendered = _render_workspace_yaml(advertisements)
        changed = rendered != self._last_rendered
        if changed:
            _write_atomic(Path(self._config.workspace_file), rendered)
            self._last_rendered = rendered
            logger.info(
                "Workspace file updated with %d code location(s): %s",
                len(advertisements),
                ", ".join(ad.location_name for ad in advertisements) or "<none>",
            )
            await self._reload_webserver()

        # Acknowledge regardless of ``changed``: a replica restarting resets its
        # own acknowledgement state, and an unchanged topology must still confirm
        # that the control plane is serving it.
        #
        # Driven by the MERGED result, not by what each replica published: a
        # collision loser is not in the workspace file, so telling it that it is
        # being served would mark it healthy while its assets are unreachable.
        await self._acknowledge(advertisements)

        return {
            "locations": len(advertisements),
            "changed": changed,
            "replicas_ok": sum(
                1 for h in self._health.values() if h.consecutive_failures == 0
            ),
            "replicas_total": len(self._health),
        }

    async def run_forever(self) -> None:
        """Poll until cancelled. Never lets one bad cycle stop the loop."""
        logger.info(
            "Workspace syncer started: %d replica(s), every %.1fs, file=%s",
            len(self._config.replica_urls),
            self._config.poll_interval,
            self._config.workspace_file,
        )
        while True:
            try:
                summary = await self.sync_once()
                logger.debug("Sync cycle complete: %s", summary)
            except asyncio.CancelledError:
                logger.info("Workspace syncer stopping")
                raise
            except Exception:
                # The syncer is the only registration channel; if it dies the
                # control plane silently freezes at its last topology.
                logger.exception("Sync cycle failed; retrying next interval")
            await asyncio.sleep(self._config.poll_interval)

    # --- Collection ----------------------------------------------------------

    async def _collect(self) -> dict[str, list[_Advertisement]]:
        """Fetch every replica's advertisements concurrently.

        Concurrent because the cycle's latency would otherwise be the SUM of all
        replica timeouts, which at N replicas exceeds the poll interval and turns
        the loop into a permanent backlog.
        """
        async with self._client() as client:
            results = await asyncio.gather(
                *(self._fetch(client, url) for url in self._config.replica_urls),
                return_exceptions=True,
            )

        out: dict[str, list[_Advertisement]] = {}
        for url, result in zip(self._config.replica_urls, results):
            health = self._health[url]
            if isinstance(result, BaseException):
                health.consecutive_failures += 1
                if health.consecutive_failures <= self._failure_tolerance:
                    # Within tolerance: keep serving what it last published, so a
                    # single slow response does not unregister its deployments.
                    logger.warning(
                        "Replica %s unreachable (%d/%d), keeping %d cached "
                        "location(s): %s",
                        url,
                        health.consecutive_failures,
                        self._failure_tolerance,
                        len(health.last_good),
                        result,
                    )
                    out[url] = list(health.last_good)
                else:
                    if health.last_good:
                        logger.error(
                            "Replica %s unreachable for %d cycles; dropping its "
                            "%d code location(s)",
                            url,
                            health.consecutive_failures,
                            len(health.last_good),
                        )
                    health.last_good = []
                    out[url] = []
                continue

            health.consecutive_failures = 0
            health.last_good = result
            out[url] = result
        return out

    async def _fetch(
        self, client: httpx.AsyncClient, replica_url: str
    ) -> list[_Advertisement]:
        """Read one replica's advertised code locations."""
        resp = await client.get(f"{replica_url}/internal/code-locations")
        resp.raise_for_status()
        payload = resp.json()
        raw_locations = payload.get("locations") or []
        parsed = [
            ad
            for ad in (
                _Advertisement.parse(item, replica_url) for item in raw_locations
            )
            if ad is not None
        ]
        logger.debug(
            "Replica %s (id=%s) advertises %d location(s)",
            replica_url,
            payload.get("replica_id"),
            len(parsed),
        )
        return parsed

    def _merge(
        self, per_replica: dict[str, list[_Advertisement]]
    ) -> list[_Advertisement]:
        """Flatten all replicas' advertisements, resolving name collisions.

        Dagster refuses to load a workspace containing two locations with the
        same name (``location_origins_from_config`` raises), which would take the
        ENTIRE control plane workspace down — not just the duplicate. A collision
        means two replicas both believe they own one deployment (a rescheduled Pod
        whose predecessor has not yet been reaped); the higher revision is the
        newer process, so it wins, and the loser is logged as the anomaly it is.
        """
        winners: dict[str, _Advertisement] = {}
        for advertisements in per_replica.values():
            for ad in advertisements:
                incumbent = winners.get(ad.location_name)
                if incumbent is None:
                    winners[ad.location_name] = ad
                    continue
                loser, winner = (
                    (incumbent, ad)
                    if ad.revision > incumbent.revision
                    else (ad, incumbent)
                )
                winners[ad.location_name] = winner
                logger.warning(
                    "Code location %s advertised by two replicas; keeping "
                    "%s:%d (revision %d) over %s:%d (revision %d)",
                    ad.location_name,
                    winner.host,
                    winner.port,
                    winner.revision,
                    loser.host,
                    loser.port,
                    loser.revision,
                )
        # Sorted so an unchanged topology renders byte-identical output and the
        # "changed?" check does not fire on dict ordering alone.
        return sorted(winners.values(), key=lambda ad: ad.location_name)

    # --- Propagation ---------------------------------------------------------

    async def _reload_webserver(self) -> None:
        """Make the webserver re-read the workspace file.

        Best-effort: the daemon refreshes on its own cycle, and the webserver
        picks the file up on its next restart or reload, so a failure here delays
        visibility rather than breaking correctness.
        """
        headers = {"Content-Type": "application/json"}
        if self._config.graphql_token:
            headers["Authorization"] = f"Bearer {self._config.graphql_token}"
        try:
            async with httpx.AsyncClient(timeout=self._config.request_timeout) as client:
                resp = await client.post(
                    self._config.graphql_url,
                    json={"query": _RELOAD_WORKSPACE_MUTATION},
                    headers=headers,
                )
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Webserver reload failed (will retry next change): %s", exc)
            return

        if body.get("errors"):
            logger.warning("Webserver reload returned errors: %s", body["errors"])
            return
        for entry in _location_entries(body):
            error = entry.get("locationOrLoadError") or {}
            if error.get("__typename") == "PythonError":
                # The location's user code failed to import. Reported here
                # because this is the only place that sees the control plane's
                # view of a data plane replica's code.
                logger.error(
                    "Code location %s failed to load in the control plane: %s",
                    entry.get("name"),
                    error.get("message"),
                )

    async def _acknowledge(self, served: list[_Advertisement]) -> None:
        """Tell each replica which of its locations the control plane now serves.

        Args:
            served: the MERGED list actually written to the workspace file. Every
                replica is contacted, including those contributing nothing, so a
                replica whose locations were dropped learns that they are no
                longer served instead of waiting on a stale acknowledgement.
        """
        by_replica: dict[str, list[_Advertisement]] = {
            url: [] for url in self._config.replica_urls
        }
        for ad in served:
            by_replica.setdefault(ad.replica_url, []).append(ad)

        async with self._client() as client:
            await asyncio.gather(
                *(
                    self._heartbeat(client, url, advertisements)
                    for url, advertisements in by_replica.items()
                ),
                return_exceptions=True,
            )

    async def _heartbeat(
        self,
        client: httpx.AsyncClient,
        replica_url: str,
        advertisements: Iterable[_Advertisement],
    ) -> None:
        """Send one replica its acknowledgement and log what it disowns."""
        acknowledged = {ad.location_name: ad.revision for ad in advertisements}
        try:
            resp = await client.post(
                f"{replica_url}/internal/code-locations/heartbeat",
                json={
                    "control_plane_id": self._config.control_plane_id,
                    "acknowledged": acknowledged,
                },
            )
            resp.raise_for_status()
            unknown = (resp.json() or {}).get("unknown") or []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Heartbeat to %s failed: %s", replica_url, exc)
            return
        if unknown:
            # The replica no longer runs something we just acknowledged: our view
            # is stale and the next poll will correct it.
            logger.info(
                "Replica %s disowns %d acknowledged location(s): %s",
                replica_url,
                len(unknown),
                ", ".join(unknown),
            )

    def _client(self) -> httpx.AsyncClient:
        """HTTP client carrying the internal token on every request."""
        from datacode_common.security.internal_token import auth_headers

        return httpx.AsyncClient(
            timeout=self._config.request_timeout,
            headers=auth_headers(self._config.internal_token),
        )


# --- Rendering ---------------------------------------------------------------


def _render_workspace_yaml(advertisements: list[_Advertisement]) -> str:
    """Serialize advertisements into Dagster workspace YAML.

    ``grpc_server`` is the only workspace target that names a code location the
    control plane does not own the code of.
    """
    load_from = [
        {
            "grpc_server": {
                "host": ad.host,
                "port": ad.port,
                "location_name": ad.location_name,
            }
        }
        for ad in advertisements
    ]
    header = (
        "# Generated by datacode_dagster_control_plane.workspace_sync.\n"
        "# Do not edit: rewritten on every data plane topology change.\n"
    )
    return header + yaml.safe_dump({"load_from": load_from}, default_flow_style=False)


def _write_atomic(path: Path, content: str) -> None:
    """Replace a file's content atomically.

    The webserver and daemon read this file concurrently with the write; a
    truncated read would drop every code location at once. ``os.replace`` on a
    same-directory temp file makes readers see either the old or the new file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _location_entries(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract location entries from a reloadWorkspace response."""
    result = ((body.get("data") or {}).get("reloadWorkspace")) or {}
    if result.get("__typename") != "Workspace":
        if result.get("message"):
            logger.warning("Webserver reload error: %s", result["message"])
        return []
    return result.get("locationEntries") or []


__all__ = ["WorkspaceSyncer"]
