"""Reload jobs — "re-import this deployment's code" as an observable operation.

A reload used to be one synchronous request. Once it may also install the working
copy's declared Python packages it can take minutes, and a minutes-long HTTP
request is the wrong shape: it needs a gateway timeout raised for one route, it
tells the user nothing while it runs, and a dropped connection loses the result.

So a reload is a JOB:

* ``POST .../reload`` accepts it and returns an envelope immediately;
* progress arrives on the existing SSE channel, stage by stage, with the
  installer's output line by line;
* ``GET .../reload/status`` is the AUTHORITATIVE state — for a reconnect, a page
  refresh, a second tab, or a CLI that polls to a terminal state;
* the record is written to the working copy, so it survives a dropped
  connection, a page reload and a runtime restart.

Two decisions worth stating
---------------------------
**Records live in the working copy** (``.datacode/deploy/reload-jobs/``), not in
process memory alone. A working copy follows its lease, so its job history
follows the replica that can actually serve a status query for it — which is the
same replica the gateway routes to.

**A second trigger ATTACHES.** Three buttons in the UI and a CLI all express the
same intent, so a request arriving while a reload is in flight returns the SAME
job id with ``attached=true`` rather than a 409. A conflict is reserved for the
case where the conflict is real: another operation (an apply, a promote) holds
the environment lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from datacode_common import python_deps as deps_codes
from datacode_common.python_deps import utc_now_iso
from datacode_common.workspace_layout import PLATFORM_STATE_DIR

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Where job records are written, relative to the working copy.
JOBS_DIR = f"{PLATFORM_STATE_DIR}/deploy/reload-jobs"
LATEST_FILE = "latest.json"

# Stages, in order. The frontend renders a step per entry, so the order is part
# of the contract.
STAGE_QUEUED = "queued"
STAGE_RESOLVING_DEPS = "resolving_deps"
STAGE_INSTALLING_DEPS = "installing_deps"
STAGE_STARTING_LOCATION = "starting_code_location"
STAGE_LOADING_DEFINITIONS = "loading_definitions"
STAGE_READY = "ready"
STAGE_FAILED = "failed"

STAGE_ORDER: tuple[str, ...] = (
    STAGE_QUEUED,
    STAGE_RESOLVING_DEPS,
    STAGE_INSTALLING_DEPS,
    STAGE_STARTING_LOCATION,
    STAGE_LOADING_DEFINITIONS,
    STAGE_READY,
)

STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

# Failure reasons. Distinct codes because the user's next action differs: fix the
# declaration's syntax, accept the platform's version of a package, read pip's
# output, fix the code, or wait for the platform.
#
# The dependency codes come from the shared declaration so that one string means
# one thing from the installer through to the UI translation key — re-typing them
# here is how a platform-version conflict came to be rendered as "the file could
# not be read".
FAILURE_DEPS_DECLARATION = deps_codes.FAILURE_DECLARATION_INVALID
FAILURE_DEPS_PLATFORM = deps_codes.FAILURE_PLATFORM_CONFLICT
FAILURE_DEPS_INSTALL = deps_codes.FAILURE_INSTALL_FAILED
FAILURE_CODE_LOAD = "code_load_failed"
FAILURE_SANDBOX = "sandbox_unavailable"
FAILURE_INTERNAL = "internal_error"

# Records kept per working copy. A reload is a manual action, so this is weeks of
# history, and the directory sits on the user's own volume.
_MAX_RECORDS = 20

# Lines of installer output retained IN THE RECORD. The full transcript stays in
# the working copy; this is what the UI panel shows without a second request.
_MAX_OUTPUT_CHARS = 32768


@dataclass
class ReloadJob:
    """One reload attempt.

    Attributes:
        job_id: Opaque id, also the record's filename.
        workspace_path: Working copy being reloaded. Every status query must
            carry it: the runtime is a headless Service and only the replica
            holding this copy's lease knows about the job.
        deployment_name: The deployment (code location) involved.
        state: ``running`` | ``succeeded`` | ``failed`` | ``cancelled``.
        stage: Current entry of :data:`STAGE_ORDER`, or ``failed``.
        message: What is happening right now, in one human sentence.
        started_at / finished_at: UTC ISO-8601. The frontend localises.
        attached: This envelope describes a job the caller JOINED rather than
            started. Set per response, never stored.
        failure_code: One of the ``FAILURE_*`` values; empty unless failed.
        conflicts: When the failure is a platform conflict, the structured
            requirements involved — each with the rewrite that would fix it.
            Carried in the envelope rather than only in ``message`` so the same
            "apply this edit" action is available from a failed reload as from
            the read-only status; a paragraph is not something a UI (or an
            agent) can act on.
        transitive_conflicts: The same for a declared package whose own
            dependencies the baseline rejects — the one shape a check of the
            declaration cannot see, so it is only known after a resolve.
        relaxed: Platform packages this copy's overlay now overrides, set on a
            SUCCESSFUL reload that had to yield a pin to install. Carried in the
            same envelope as the failures because it is reported at the same
            moment and to the same reader: the reload panel is where the user
            finds out what their declaration did.
        output_tail: Installer transcript / import traceback, for the copyable
            panel.
        error_log_id: Record under ``{workspace}/logs/errors/`` holding the full
            structured failure, so the agent can diagnose it from the filesystem.
        deps_state: The dependency state after this job (shared ``STATE_*``).
        deployment: The resulting ``DeploymentInfo`` dict on success.
    """

    job_id: str
    workspace_path: str
    deployment_name: str
    state: str = STATE_RUNNING
    stage: str = STAGE_QUEUED
    message: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""
    attached: bool = False
    failure_code: str = ""
    conflicts: list[dict] = field(default_factory=list)
    transitive_conflicts: list[dict] = field(default_factory=list)
    relaxed: list[dict] = field(default_factory=list)
    output_tail: str = ""
    error_log_id: str = ""
    deps_state: str = ""
    deployment: Optional[dict] = None

    @property
    def terminal(self) -> bool:
        return self.state != STATE_RUNNING

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Derived, so the frontend does not have to keep its own copy of the
        # ladder to draw a progress bar.
        data["stages"] = list(STAGE_ORDER)
        return data


class ReloadJobStore:
    """Job records for one runtime: in memory, mirrored into the working copy.

    Memory serves the common case (a status poll seconds after the trigger)
    without touching the disk. The file is what makes a record survive a page
    refresh, a reconnect and a restart — and, because it lives in the working
    copy, it is present on whichever replica currently holds the lease.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, ReloadJob] = {}
        # Working copy -> id of the job currently running for it. The key to
        # attach semantics: one reload per copy, whoever asks joins it.
        self._active: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start_or_attach(
        self, workspace_path: str, deployment_name: str
    ) -> tuple[ReloadJob, bool]:
        """Return the running job for this copy, or create one.

        Returns:
            ``(job, created)``. ``created is False`` means the caller attached to
            a job someone else started; the envelope is marked accordingly so the
            UI can say "already reloading" instead of pretending it triggered it.
        """
        async with self._lock:
            existing_id = self._active.get(workspace_path)
            if existing_id:
                existing = self._jobs.get(existing_id)
                if existing is not None and not existing.terminal:
                    return replace(existing, attached=True), False
                # Terminal or evicted: the slot is stale.
                self._active.pop(workspace_path, None)

            job = ReloadJob(
                job_id=uuid.uuid4().hex[:16],
                workspace_path=workspace_path,
                deployment_name=deployment_name,
                message="queued",
            )
            self._jobs[job.job_id] = job
            self._active[workspace_path] = job.job_id
            self._persist(job)
            return job, True

    def get(self, job_id: str) -> Optional[ReloadJob]:
        return self._jobs.get(job_id)

    def latest(self, workspace_path: str) -> Optional[ReloadJob]:
        """The running job for a copy, else its most recent record.

        Reading from disk on a miss is what makes a status query survive a
        runtime restart: the job is gone from memory, but "the last reload failed
        because X" is still the answer the user needs.
        """
        active_id = self._active.get(workspace_path)
        if active_id and active_id in self._jobs:
            return self._jobs[active_id]

        candidates = [
            job
            for job in self._jobs.values()
            if job.workspace_path == workspace_path
        ]
        if candidates:
            return max(candidates, key=lambda job: job.started_at)
        return self._load_latest(workspace_path)

    def find(self, workspace_path: str, job_id: str = "") -> Optional[ReloadJob]:
        """Resolve a status query. Falls back to disk for a known id."""
        if not job_id:
            return self.latest(workspace_path)
        job = self._jobs.get(job_id)
        if job is not None:
            return job
        return self._load(workspace_path, job_id)

    # ── transitions ────────────────────────────────────────────────────

    async def advance(
        self, job_id: str, stage: str, message: str, *, deps_state: str = ""
    ) -> Optional[ReloadJob]:
        """Move a job to ``stage``. Publishes progress; never raises."""
        job = self._jobs.get(job_id)
        if job is None or job.terminal:
            return job
        job.stage = stage
        job.message = message
        if deps_state:
            job.deps_state = deps_state
        await _publish(job)
        return job

    async def append_output(self, job_id: str, line: str) -> None:
        """Add one installer line and publish it. Bounded, never raises."""
        job = self._jobs.get(job_id)
        if job is None or job.terminal or not line:
            return
        job.output_tail = f"{job.output_tail}{line}\n"[-_MAX_OUTPUT_CHARS:]
        await _publish(job, line=line)

    async def note_relaxed(self, job_id: str, relaxed: list[dict]) -> None:
        """Record platform pins the install yielded, and publish them.

        Separate from :meth:`advance` because this is not a stage: the install
        succeeded, and what changed is what the copy will import from now on. It
        is published immediately rather than only carried in the terminal
        envelope so the panel shows it while the code location is still starting
        — the same moment the transcript said it.
        """
        job = self._jobs.get(job_id)
        if job is None or job.terminal or not relaxed:
            return
        job.relaxed = list(relaxed)
        await _publish(job)

    async def succeed(
        self, job_id: str, deployment: dict, *, deps_state: str = ""
    ) -> Optional[ReloadJob]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        job.state = STATE_SUCCEEDED
        job.stage = STAGE_READY
        job.message = "the code location is serving the current code"
        job.deployment = deployment
        if deps_state:
            job.deps_state = deps_state
        return await self._finish(job)

    async def fail(
        self,
        job_id: str,
        *,
        failure_code: str,
        message: str,
        output_tail: str = "",
        error_log_id: str = "",
        deps_state: str = "",
        conflicts: Optional[list[dict]] = None,
        transitive_conflicts: Optional[list[dict]] = None,
    ) -> Optional[ReloadJob]:
        """Record a terminal failure with its reason.

        A reload that could not install the declared packages, or could not load
        the code, is a FAILURE — never a success with ``alive=false``. Reporting
        an unfinished reload as done is the one outcome the whole job machinery
        exists to prevent.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None
        job.state = STATE_FAILED
        job.stage = STAGE_FAILED
        job.failure_code = failure_code
        job.message = message
        if conflicts:
            job.conflicts = list(conflicts)
        if transitive_conflicts:
            job.transitive_conflicts = list(transitive_conflicts)
        if output_tail:
            job.output_tail = output_tail[-_MAX_OUTPUT_CHARS:]
        if error_log_id:
            job.error_log_id = error_log_id
        if deps_state:
            job.deps_state = deps_state
        return await self._finish(job)

    async def cancel(self, job_id: str, message: str = "cancelled") -> Optional[ReloadJob]:
        job = self._jobs.get(job_id)
        if job is None or job.terminal:
            return job
        job.state = STATE_CANCELLED
        job.stage = STAGE_FAILED
        job.message = message
        return await self._finish(job)

    async def _finish(self, job: ReloadJob) -> ReloadJob:
        job.finished_at = utc_now_iso()
        self._active.pop(job.workspace_path, None)
        self._persist(job)
        await _publish(job)
        logger.info(
            "deployment_reload_job_finished",
            workspace_path=job.workspace_path,
            deployment=job.deployment_name,
            job_id=job.job_id,
            state=job.state,
            failure_code=job.failure_code or None,
            error_log_id=job.error_log_id or None,
        )
        return job

    # ── persistence ────────────────────────────────────────────────────

    def _persist(self, job: ReloadJob) -> None:
        """Write the record and the ``latest`` pointer. Best-effort.

        A copy that cannot be written to (full disk, read-only mount) must not
        fail the reload: the record is how the result is READ later, not what
        makes it true.
        """
        try:
            directory = Path(job.workspace_path) / JOBS_DIR
            directory.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(job.to_dict(), ensure_ascii=False, indent=2)
            for target in (directory / f"{job.job_id}.json", directory / LATEST_FILE):
                tmp = target.with_suffix(".json.tmp")
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, target)
            self._prune(directory)
        except OSError as exc:
            logger.warning(
                "deployment_reload_job_unpersisted",
                workspace_path=job.workspace_path,
                job_id=job.job_id,
                error=str(exc),
            )

    def _load(self, workspace_path: str, job_id: str) -> Optional[ReloadJob]:
        return self._read(Path(workspace_path) / JOBS_DIR / f"{job_id}.json")

    def _load_latest(self, workspace_path: str) -> Optional[ReloadJob]:
        return self._read(Path(workspace_path) / JOBS_DIR / LATEST_FILE)

    @staticmethod
    def _read(path: Path) -> Optional[ReloadJob]:
        """Rebuild a record, ignoring fields this build does not know.

        Tolerant on purpose: a record is state written by a possibly older or
        newer runtime, and refusing to read one would turn a forward-compatible
        field into "your last reload has no result".
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        known = {f for f in ReloadJob.__dataclass_fields__}
        return ReloadJob(**{k: v for k, v in data.items() if k in known})

    @staticmethod
    def _prune(directory: Path) -> None:
        """Keep the newest records only. Best-effort."""
        try:
            files = sorted(
                (
                    entry
                    for entry in directory.iterdir()
                    if entry.suffix == ".json" and entry.name != LATEST_FILE
                ),
                key=lambda entry: entry.stat().st_mtime,
                reverse=True,
            )
            for stale in files[_MAX_RECORDS:]:
                stale.unlink(missing_ok=True)
        except OSError:
            return


async def _publish(job: ReloadJob, *, line: str = "") -> None:
    """Push the job's current state onto the SSE bus. Best-effort.

    Progress is an optimisation: a client that missed every event still gets the
    right answer from the status endpoint, which is why a failure here is logged
    at debug and never propagated.
    """
    try:
        from src.sqlmesh.event_bus import EventTopic, SSEEvent, get_event_bus

        payload = job.to_dict()
        if line:
            # The full tail is in the record; the event carries only what is new,
            # so a long install does not re-send its whole transcript per line.
            payload = {**payload, "output_tail": "", "line": line}
        await get_event_bus().publish(
            SSEEvent(
                topic=EventTopic.DEPLOYMENT,
                data=payload,
                workspace_path=job.workspace_path,
            )
        )
    except Exception as exc:  # noqa: BLE001 — progress must not break the job
        logger.debug("deployment_reload_publish_failed", error=str(exc))


_store: Optional[ReloadJobStore] = None


def get_reload_job_store() -> ReloadJobStore:
    """Return the process-wide job store."""
    global _store
    if _store is None:
        _store = ReloadJobStore()
    return _store


__all__ = [
    "FAILURE_CODE_LOAD",
    "FAILURE_DEPS_DECLARATION",
    "FAILURE_DEPS_INSTALL",
    "FAILURE_DEPS_PLATFORM",
    "FAILURE_INTERNAL",
    "FAILURE_SANDBOX",
    "JOBS_DIR",
    "STAGE_FAILED",
    "STAGE_INSTALLING_DEPS",
    "STAGE_LOADING_DEFINITIONS",
    "STAGE_ORDER",
    "STAGE_QUEUED",
    "STAGE_READY",
    "STAGE_RESOLVING_DEPS",
    "STAGE_STARTING_LOCATION",
    "STATE_CANCELLED",
    "STATE_FAILED",
    "STATE_RUNNING",
    "STATE_SUCCEEDED",
    "ReloadJob",
    "ReloadJobStore",
    "get_reload_job_store",
]
