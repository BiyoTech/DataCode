"""DepsService — turn a declaration into an installed overlay, visibly.

One method does the work (:meth:`DepsService.sync`):

* read the declaration, decide whether anything must happen, then submit a
  ``deps.sync`` task;
* stream the runner's transcript to a callback while it runs, so the caller has
  something to put in a progress bar;
* write the manifest — the runner never does, so an interrupted install cannot
  leave a manifest describing a tree that is not there;
* classify every failure into a stable code with a sentence that names the next
  action, and archive it to ``{workspace}/logs/errors/`` so the agent can
  diagnose it with ordinary file tools instead of scraping an API response.

Why the transcript is a file: both vehicles share the working copy, but only the
local child process could stream stdout to the runtime. A file gives dev and
Kubernetes the same live progress and outlives the vehicle, so a user who
reloads the page after a failure can still read what pip said.

Why THIS service owns the platform baseline
-------------------------------------------
The overlay is consumed by this process's interpreter: Dagster code locations
share it rather than getting a per-workspace venv. So "what the platform
provides" is a question only this process can answer correctly, and the answer
travels to the installer in the task parameters (see
:func:`platform_baseline`). Letting the sandbox introspect its own interpreter
instead would describe the task-runner image, which is a different and much
smaller dependency set — and identical to this one in development, where the
local backend runs pip on this very interpreter, so the divergence appears only
in production.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional
from urllib.parse import urlparse

from datacode_common.python_deps import (
    FAILURE_DECLARATION_INVALID,
    FAILURE_INDEX_NOT_CONFIGURED,
    FAILURE_INSTALL_FAILED,
    FAILURE_PLATFORM_CONFLICT,
    FAILURE_PLATFORM_TRANSITIVE,
    STATE_EMPTY,
    STATE_READY,
    TASK_KIND_DEPS_SYNC,
    DepsDeclaration,
    DepsDeclarationError,
    InstallPlan,
    PlatformConflict,
    PlatformCoreConflict,
    TransitiveConflict,
    manifest_path,
    site_packages_path,
)
from datacode_common.task_protocol import TaskResult

from src.codeloc.errors import ErrorCode
from src.config import settings
from src.deps.baseline import conflicts_for, platform_baseline, relaxable_packages
from src.deps.manifest import (
    DepsStatus,
    failed_manifest,
    manifest_from_task_result,
    read_declaration,
    read_install_transcript,
    read_status,
    write_manifest,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Called with each new transcript line while an install runs. Async because the
# only caller publishes to the SSE bus.
LineSink = Callable[[str], Awaitable[None]]

# Transcript poll interval. pip emits a line every few hundred milliseconds at
# most; anything shorter only burns CPU on a file the user watches scroll.
_TAIL_INTERVAL_SECONDS = 0.5

# Failure codes carried in the reload job envelope. Shared with the runner's
# ``ERR_DEPS_*`` values through one declaration, so a code crossing the task
# boundary needs no translation and cannot drift into a message about the wrong
# problem.
FAILURE_DECLARATION = FAILURE_DECLARATION_INVALID
FAILURE_PLATFORM = FAILURE_PLATFORM_CONFLICT
# A declared package whose own dependencies fight the baseline. Carried under
# its own code because the fix is another release of what the user declared —
# or an operator — never "accept the platform's version of it".
FAILURE_TRANSITIVE = FAILURE_PLATFORM_TRANSITIVE
FAILURE_INSTALL = FAILURE_INSTALL_FAILED
FAILURE_INDEX = FAILURE_INDEX_NOT_CONFIGURED
FAILURE_SANDBOX = "sandbox_unavailable"


class IndexNotConfigured(RuntimeError):
    """No usable package index, so an install cannot be attempted."""


@dataclass(frozen=True)
class DepsSyncOutcome:
    """What one :meth:`DepsService.sync` did.

    Attributes:
        ok: Whether the overlay is now usable. False must fail the reload that
            asked for it — a code location started without the packages its
            assets import produces a definitions error nobody can act on.
        skipped: Nothing needed installing (the fingerprint already matched).
        installed: Packages in the overlay afterwards.
        status: Read-only status after the operation.
        failure_code: One of the ``FAILURE_*`` constants; empty on success.
        message: Human-readable reason, shown verbatim in the UI and CLI.
        conflicts: Structured platform conflicts, when that is the failure.
            Carried separately from ``message`` so the UI can offer "rewrite to
            the platform version" per line instead of printing a paragraph.
        transitive_conflicts: Declared packages whose OWN dependencies the
            baseline rejects, each with the rewrite the installer verified. The
            same idea as ``conflicts`` for the one shape a declaration check
            cannot see, since nothing in the file mentions the blocking package.
        relaxed: Platform pins that were YIELDED so this install could succeed,
            as ``{name, platform_version, overlay_version, required_by}``. A
            successful outcome, but one the user is told about: from now on this
            working copy runs a different version of those packages than the
            platform does, and that difference has to be visible somewhere other
            than a log line.
        output_tail: End of the transcript, for the copyable panel.
        error_log_id: Id of the archived record under ``logs/errors/``.
    """

    ok: bool
    skipped: bool = False
    installed: int = 0
    status: Optional[DepsStatus] = None
    failure_code: str = ""
    message: str = ""
    conflicts: tuple[PlatformConflict, ...] = ()
    transitive_conflicts: tuple[TransitiveConflict, ...] = ()
    relaxed: tuple[Mapping[str, str], ...] = ()
    output_tail: str = ""
    error_log_id: str = ""


class DepsService:
    """Installs a working copy's declared Python packages."""

    def __init__(self, submitter=None) -> None:
        """Args:
            submitter: Task submitter. Injected in tests; the shared one also
                resolves this kind's isolation profile and index egress grant.
        """
        self._submitter = submitter

    @property
    def submitter(self):
        if self._submitter is None:
            from src.tasks.submitter import get_task_submitter

            self._submitter = get_task_submitter()
        return self._submitter

    @staticmethod
    def inspect(workspace_path: str) -> DepsStatus:
        """Compare declaration and manifest. NEVER installs.

        What the ``ensure`` endpoint calls on every asset-page open. The
        separation is the point of the design: an implicit install there would be
        a multi-minute operation behind a request expected to answer instantly.
        """
        return read_status(workspace_path)

    async def sync(
        self,
        workspace_path: str,
        *,
        source: str,
        actor_user_id: str = "",
        tenant_id: str = "",
        workspace_id: str = "",
        environment: str = "",
        on_line: Optional[LineSink] = None,
    ) -> DepsSyncOutcome:
        """Bring the overlay in line with the declaration.

        Args:
            workspace_path: Working copy, as this runtime sees it.
            source: ``ui`` | ``cli`` | ``reload``; recorded in the manifest.
            actor_user_id: Who asked, for the manifest and the audit trail.
            tenant_id: Task audit and quota attribution.
            workspace_id: Task audit.
            environment: Environment the caller acts on, for audit.
            on_line: Called with each new transcript line while pip runs.

        Returns:
            The outcome. Failures are RETURNED, not raised: the caller is a job
            that records a terminal state either way, and an exception would make
            "the install failed" indistinguishable from "the job crashed".
        """
        try:
            declaration = read_declaration(workspace_path)
        except DepsDeclarationError as exc:
            return self._failure(
                workspace_path,
                FAILURE_DECLARATION,
                str(exc),
                error_code=ErrorCode.DEPS_DECLARATION_INVALID,
                fingerprint="",
                source=source,
                actor_user_id=actor_user_id,
            )

        if declaration.is_empty:
            # Any overlay left from an earlier declaration is stale. Removing it
            # keeps the invariant that installed state is a pure function of the
            # declaration.
            await asyncio.to_thread(self._discard_overlay, workspace_path)
            return DepsSyncOutcome(
                ok=True, skipped=True, status=DepsStatus(state=STATE_EMPTY)
            )

        status = read_status(workspace_path)
        if status.state == STATE_READY:
            logger.info(
                "deps_already_installed",
                workspace_path=workspace_path,
                fingerprint=declaration.fingerprint,
                packages=status.installed,
            )
            return DepsSyncOutcome(
                ok=True, skipped=True, installed=status.installed, status=status
            )

        try:
            plan = self._build_plan(declaration)
        except IndexNotConfigured as exc:
            return self._failure(
                workspace_path,
                FAILURE_INDEX,
                str(exc),
                error_code=ErrorCode.DEPS_INDEX_NOT_CONFIGURED,
                fingerprint=declaration.fingerprint,
                source=source,
                actor_user_id=actor_user_id,
            )
        except PlatformCoreConflict as exc:
            # Refused HERE, before a sandbox is started: the answer is a pure
            # comparison against the baseline this process already knows, so
            # spending a vehicle and a pip resolve to reach the same verdict
            # would only make the user wait for it.
            return self._failure(
                workspace_path,
                FAILURE_PLATFORM,
                str(exc),
                error_code=ErrorCode.DEPS_PLATFORM_CONFLICT,
                fingerprint=declaration.fingerprint,
                source=source,
                actor_user_id=actor_user_id,
                conflicts=exc.conflicts,
            )

        logger.info(
            "deps_sync_start",
            workspace_path=workspace_path,
            fingerprint=declaration.fingerprint,
            requirements=len(plan.requirements),
            reason=status.state,
            source=source,
        )

        result, submit_error = await self._run_task(
            workspace_path,
            plan,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            environment=environment,
            actor_user_id=actor_user_id,
            on_line=on_line,
        )

        if submit_error is not None:
            # Nothing ran. Reported as a sandbox problem rather than an install
            # failure so the user does not go looking at their declaration for a
            # problem that is in the deployment.
            return self._failure(
                workspace_path,
                FAILURE_SANDBOX,
                submit_error,
                error_code=ErrorCode.DEPS_INSTALL_FAILED,
                fingerprint=declaration.fingerprint,
                source=source,
                actor_user_id=actor_user_id,
            )

        if result is None or not result.succeeded:
            message = (
                result.error_message
                if result is not None and result.error_message
                else "the dependency install failed"
            )
            return self._failure(
                workspace_path,
                # The runner's codes are more specific than "install failed";
                # carried through so the UI can key on them.
                (result.error_code if result is not None else "") or FAILURE_INSTALL,
                message,
                error_code=ErrorCode.DEPS_INSTALL_FAILED,
                fingerprint=declaration.fingerprint,
                source=source,
                actor_user_id=actor_user_id,
                # Diagnosed by the runner, which is where pip and the index are.
                # A failure carries it in its result payload, not in its
                # message, so the UI gets an edit and not a paragraph.
                transitive_conflicts=_transitive_from_result(result),
            )

        manifest = manifest_from_task_result(
            dict(result.result),
            fingerprint=declaration.fingerprint,
            source=source,
            actor_user_id=actor_user_id,
        )
        await asyncio.to_thread(write_manifest, workspace_path, manifest)

        if manifest.shadowed:
            # Unreachable by design: resolution is constrained to the baseline
            # and anything landing on a baseline version is dropped from the
            # tree, so the runner fails the install rather than reporting this.
            # Logged as an ERROR because seeing it means one of those two
            # mechanisms stopped working.
            logger.error(
                "deps_overlay_shadows_platform_packages",
                workspace_path=workspace_path,
                names=[entry["name"] for entry in manifest.shadowed],
                hint="the platform baseline did not reach the installer",
            )

        if manifest.relaxed:
            # A successful install that changed what this copy's processes
            # import relative to the platform's own. WARNING, not info: six
            # months from now "why does only this workspace behave like that"
            # has to be answerable from the logs as well as from the manifest.
            logger.warning(
                "deps_platform_pins_yielded",
                workspace_path=workspace_path,
                packages=[
                    f"{entry['name']} {entry['platform_version']}"
                    f"->{entry['overlay_version']}"
                    for entry in manifest.relaxed
                ],
                hint="declared dependencies required versions of these "
                "platform packages; they are overridden for this working copy "
                "only",
            )

        return DepsSyncOutcome(
            ok=True,
            installed=len(manifest.packages),
            status=read_status(workspace_path),
            relaxed=manifest.relaxed,
            output_tail=str(result.result.get("output_tail") or ""),
        )

    # ── task plumbing ──────────────────────────────────────────────────

    async def _run_task(
        self,
        workspace_path: str,
        plan: InstallPlan,
        *,
        tenant_id: str,
        workspace_id: str,
        environment: str,
        actor_user_id: str,
        on_line: Optional[LineSink],
    ) -> tuple[Optional[TaskResult], Optional[str]]:
        """Submit the install and tail its transcript until it finishes.

        Returns ``(result, None)`` or ``(None, submission_error)``.
        """
        from src.tasks.submitter import TaskSubmissionError

        submission = asyncio.create_task(
            self.submitter.submit(
                TASK_KIND_DEPS_SYNC,
                workspace_path,
                environment=environment,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                params=plan.to_params(),
                actor_user_id=actor_user_id,
                timeout_seconds=settings.deps_install_timeout_seconds,
            )
        )
        tail = (
            asyncio.create_task(self._tail_transcript(workspace_path, on_line))
            if on_line is not None
            else None
        )

        try:
            return await submission, None
        except TaskSubmissionError as exc:
            logger.error(
                "deps_sync_not_submitted",
                workspace_path=workspace_path,
                error=str(exc),
            )
            return None, str(exc)
        finally:
            if tail is not None:
                tail.cancel()
                # Drain what the task wrote between the last poll and its exit;
                # without this the progress panel ends mid-download even on
                # success.
                await self._flush_transcript(workspace_path, on_line)

    async def _tail_transcript(
        self, workspace_path: str, on_line: Optional[LineSink]
    ) -> None:
        """Poll the transcript and forward what is new. Never raises."""
        if on_line is None:
            return
        seen = 0
        while True:
            try:
                text = await asyncio.to_thread(read_install_transcript, workspace_path)
                lines = text.splitlines()
                for line in lines[seen:]:
                    await on_line(line)
                seen = max(seen, len(lines))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — progress is best-effort
                logger.debug(
                    "deps_transcript_tail_failed",
                    workspace_path=workspace_path,
                    error=str(exc),
                )
            await asyncio.sleep(_TAIL_INTERVAL_SECONDS)

    @staticmethod
    async def _flush_transcript(
        workspace_path: str, on_line: Optional[LineSink]
    ) -> None:
        """Emit the transcript's final line, if any. Best-effort."""
        if on_line is None:
            return
        try:
            text = await asyncio.to_thread(read_install_transcript, workspace_path)
            lines = text.splitlines()
            if lines:
                await on_line(lines[-1])
        except Exception:  # noqa: BLE001
            return

    @staticmethod
    def _build_plan(declaration: DepsDeclaration) -> InstallPlan:
        """Assemble install parameters from PLATFORM configuration.

        Raises:
            IndexNotConfigured: No index configured, or the configured URL embeds
                credentials. The second is refused rather than stripped: the spec
                is written to audit records and, on Kubernetes, to a ConfigMap,
                so a URL with a token must not travel that path at all.
            PlatformCoreConflict: A declared package is pinned away from the
                version the platform ships. Checked while BUILDING the plan, so
                there is no path that submits a task which cannot succeed.
        """
        baseline = platform_baseline()
        conflicts = conflicts_for(declaration)
        if conflicts:
            raise PlatformCoreConflict(conflicts)

        index_url = (settings.deps_index_url or "").strip()
        if not index_url:
            raise IndexNotConfigured(
                "this deployment has no package index configured "
                "(deps.index_url), so declared Python dependencies cannot be "
                "installed. Ask an operator to point it at the index this "
                "platform may reach."
            )

        extras = _split(settings.deps_extra_index_urls)
        for candidate in (index_url, *extras):
            parsed = urlparse(candidate)
            if parsed.username or parsed.password:
                raise IndexNotConfigured(
                    "the configured package index embeds credentials in its URL. "
                    "A task specification is written to audit records and to a "
                    "ConfigMap, so it must not carry one: use an index that "
                    "authorises by network location, or a pull-through mirror."
                )

        return InstallPlan(
            requirements=declaration.requirements,
            fingerprint=declaration.fingerprint,
            # The whole baseline, not only the declared names: it becomes pip's
            # constraint set, which is what bounds TRANSITIVE resolution. A
            # subset would leave exactly the packages nobody declared — numpy,
            # pyarrow — free to resolve to a version that shadows the
            # platform's.
            platform_packages=tuple(sorted(baseline.items())),
            # Which of those pins the installer may release if — and only if —
            # keeping them makes the declaration unsatisfiable. Derived here
            # because the test is "does this distribution ship a compiled
            # extension", which only the interpreter that HAS it can answer.
            relaxable_packages=tuple(sorted(relaxable_packages())),
            max_bytes=settings.deps_max_bytes,
            index_url=index_url,
            extra_index_urls=extras,
            trusted_hosts=_split(settings.deps_trusted_hosts),
            # Packages an operator allows to be built from a source distribution.
            # Carried in the plan rather than read by the installer from its own
            # environment, for the same reason as the index: the two vehicles
            # build that environment differently, so a setting that survives in
            # only one of them is a setting that silently does not apply.
            allow_source_packages=_canonical(settings.deps_allow_source_packages),
        )

    # ── failure handling ───────────────────────────────────────────────

    def _failure(
        self,
        workspace_path: str,
        failure_code: str,
        message: str,
        *,
        error_code: ErrorCode,
        fingerprint: str,
        source: str,
        actor_user_id: str,
        conflicts: tuple[PlatformConflict, ...] = (),
        transitive_conflicts: tuple[TransitiveConflict, ...] = (),
    ) -> DepsSyncOutcome:
        """Record a failed install and return it as an outcome.

        Three things happen, each for its own reason:

        1. a FAILED manifest replaces whatever was there. The overlay has been
           cleared by the runner at this point, so a stale "ready" manifest would
           make consumers inject a directory that is gone;
        2. the transcript tail is captured — that is where pip's own explanation
           lives;
        3. the whole thing is archived to ``{workspace}/logs/errors/``, the
           platform's existing convention for actionable failures, which is why
           the agent can diagnose this with no new tooling.
        """
        tail = read_install_transcript(workspace_path)
        try:
            write_manifest(
                workspace_path,
                failed_manifest(
                    fingerprint=fingerprint,
                    source=source,
                    actor_user_id=actor_user_id,
                    error_code=failure_code,
                    error_detail=message,
                ),
            )
        except OSError as exc:
            # A full or read-only copy. The install already failed; not being
            # able to say so in the manifest must not hide the reason.
            logger.warning(
                "deps_failed_manifest_unwritable",
                workspace_path=workspace_path,
                error=str(exc),
            )

        error_log_id = archive_failure(
            workspace_path,
            operation="deps.sync",
            error_code=error_code,
            failure_code=failure_code,
            message=message,
            output_tail=tail,
            context={
                "source": source,
                "fingerprint": fingerprint,
                # Archived structured as well as in prose: the agent's repair
                # path reads this directory, and a list of
                # {requirement, suggested} pairs is something it can act on
                # without parsing English.
                **(
                    {"conflicts": [item.to_dict() for item in conflicts]}
                    if conflicts
                    else {}
                ),
                **(
                    {
                        "transitive_conflicts": [
                            item.to_dict() for item in transitive_conflicts
                        ]
                    }
                    if transitive_conflicts
                    else {}
                ),
            },
        )

        logger.error(
            "deps_sync_failed",
            workspace_path=workspace_path,
            failure_code=failure_code,
            error_log_id=error_log_id,
            error=message[:2048],
        )
        return DepsSyncOutcome(
            ok=False,
            failure_code=failure_code,
            message=message,
            conflicts=conflicts,
            transitive_conflicts=transitive_conflicts,
            output_tail=tail,
            error_log_id=error_log_id,
            status=read_status(workspace_path),
        )


    @staticmethod
    def _discard_overlay(workspace_path: str) -> None:
        """Remove the overlay and its manifest. Best-effort, never raises."""
        for target in (
            Path(site_packages_path(workspace_path)),
            Path(manifest_path(workspace_path)),
        ):
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            except OSError as exc:
                logger.warning(
                    "deps_overlay_discard_failed",
                    workspace_path=workspace_path,
                    target=str(target),
                    error=str(exc),
                )


def _transitive_from_result(
    result: Optional[TaskResult],
) -> tuple[TransitiveConflict, ...]:
    """Rebuild the runner's structured diagnosis, if it sent one.

    Defensive on purpose: the records come from a task result written by a
    different image, so one this build cannot read is dropped rather than
    allowed to turn a failed install into a crash. The prose message still
    reaches the user either way.
    """
    if result is None:
        return ()
    raw = result.result.get("transitive_conflicts")
    if not isinstance(raw, list):
        return ()
    parsed: list[TransitiveConflict] = []
    for item in raw:
        if isinstance(item, Mapping):
            parsed.append(TransitiveConflict.from_dict(item))
    return tuple(parsed)


def archive_failure(
    workspace_path: str,
    *,
    operation: str,
    error_code: ErrorCode,
    failure_code: str,
    message: str,
    output_tail: str = "",
    context: Optional[dict] = None,
) -> str:
    """Write one actionable failure to ``{workspace}/logs/errors/``.

    Shared by the dependency install and the reload job so both land in the same
    directory, in the same shape, with the same redaction and retention. The
    agent's repair path already reads that directory; a second format would need
    a second reader.

    There is no live exception at these call sites — the failure is a non-zero
    exit plus a transcript, not a Python traceback — so a marker exception
    carries the message and the actionable content travels in ``context``.

    Returns:
        The archive's ``error_id``, or ``""`` when archiving is off or failed.
        Never raises: an unwritable archive must not mask the failure it
        describes.
    """
    from src.utils.error_archive import ErrorArchive, ErrorArchiveContext
    from src.utils.errors import ServiceError

    try:
        payload = dict(context or {})
        payload["failure_code"] = failure_code
        if output_tail:
            payload["output_tail"] = output_tail
        error_id = ErrorArchive.record(
            exc=RuntimeError(message),
            service_error=ServiceError(
                code=error_code,
                # First line only: the archive's `message` is an index entry,
                # while `detail` keeps the full text.
                message=message.split("\n", 1)[0][:512],
                detail=message,
                status_code=422,
            ),
            archive_context=ErrorArchiveContext(
                workspace_path=workspace_path,
                operation=operation,
                context=payload,
            ),
        )
        return error_id or ""
    except Exception:  # noqa: BLE001 — diagnostics must never break the caller
        logger.warning("deps_failure_archive_failed", exc_info=True)
        return ""


def _split(raw: str) -> tuple[str, ...]:
    """Split a comma-separated configuration value, dropping blanks."""
    return tuple(item.strip() for item in (raw or "").split(",") if item.strip())


def _canonical(raw: str) -> tuple[str, ...]:
    """Same, for values that are distribution NAMES.

    Canonicalised here rather than at the comparison site: the installer matches
    these against names derived from requirements, and ``PySpark`` in a config
    file must mean the same package as ``pyspark`` in a declaration.
    """
    from packaging.utils import canonicalize_name

    return tuple(sorted({canonicalize_name(item) for item in _split(raw)}))


_service: Optional[DepsService] = None


def get_deps_service() -> DepsService:
    """Return the shared service."""
    global _service
    if _service is None:
        _service = DepsService()
    return _service


__all__ = [
    "FAILURE_DECLARATION",
    "FAILURE_INDEX",
    "FAILURE_INSTALL",
    "FAILURE_PLATFORM",
    "FAILURE_SANDBOX",
    "FAILURE_TRANSITIVE",
    "DepsService",
    "DepsSyncOutcome",
    "IndexNotConfigured",
    "archive_failure",
    "get_deps_service",
]
