"""Local task backend — one hardened child process per task (design 06 P6a).

The development and single-node vehicle. It provides the SAME contract as the
Kubernetes backend (spec file in, structured result on stdout) so both dev and
production exercise one code path; what it cannot provide is the container
boundary, and it is honest about that: it advertises only
:data:`IsolationLevel.PROCESS`, so the policy layer will not route L2/L3-base
work here.

What it does provide is the whole of L3-proc:

* the child starts from an EMPTY environment (platform secrets unreachable),
* resource limits taken from the profile itself,
* cwd pinned to the one working copy,
* uid separation where the process has the privilege for it.

Without those, "isolation works in dev" would be an illusion that only breaks in
production.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Optional

from datacode_common.isolation import IsolationLevel, IsolationProfile
from datacode_common.task_protocol import (
    ERR_NO_RESULT,
    ERR_TIMEOUT,
    TaskResult,
    TaskSpec,
    TaskStatus,
    extract_result,
)

from src.config import settings
from src.isolation.guards import default_guard_root, ensure_guard_dir
from src.isolation.hardening import (
    ResourceLimits,
    apply_rlimits,
    build_clean_env,
    try_uid_separation,
)
from src.tasks.backends.base import TaskBackend, TaskBackendError
from src.tasks.credentials import SandboxCredentials
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Grace period between SIGTERM and SIGKILL when a task overruns. Long enough for
# the runner's own alarm handler to emit a timed_out result, short enough that a
# wedged child cannot hold the slot.
_TERMINATE_GRACE_SECONDS = 5.0

# Written to fd 2 by ``try_uid_separation`` when the drop was refused. Matched on
# the child's stderr so the parent can report honestly what was applied.
_UID_SEPARATION_FAILED_MARKER = "sandbox_uid_separation_unavailable"


def _resolve_runner_root() -> Path:
    """Locate the task-runner package.

    Falls back to the sibling checkout directory, which is where it lives in
    development. A missing package is a configuration error, not something to
    work around: silently skipping execution would look like a task that does
    nothing.
    """
    configured = (settings.task_runner_root or "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        # .../workspace-runtime/src/tasks/backends/local.py
        #  -> parents[3] is workspace-runtime/, parents[4] the repo root.
        root = Path(__file__).resolve().parents[4] / "task-runner"

    if not (root / "src" / "main.py").is_file():
        raise TaskBackendError(
            f"task-runner package not found at {root} "
            "(expected src/main.py); set task_runner_root"
        )
    return root


class LocalProcessBackend(TaskBackend):
    """Runs each task as a one-shot hardened child process."""

    def __init__(self) -> None:
        self._runner_root = _resolve_runner_root()
        # Package roots the child imports from: the runner itself and the shared
        # library. Filesystem locations, not secrets.
        self._python_path = (
            str(self._runner_root),
            str(self._runner_root.parent / "shared" / "python"),
        )
        # Platform-owned PATH segments, at the two ends of the system directories
        # for two different reasons (see hardening.sandbox_path):
        #
        #   * the guard directory is PREPENDED — it must beat a real `sqlmesh` in
        #     /usr/local/bin, which is where pip installs one on the host;
        #   * the interpreter's own bin/ is APPENDED. This vehicle execs
        #     `sys.executable`, so the child runs THIS service's interpreter, and
        #     that virtualenv is where `sqlmesh` and `dagster` actually are in a
        #     development install. Without it `sqlmesh test` fails with "command
        #     not found" — a container image installs them into /usr/local/bin, so
        #     only this vehicle needs the entry. Appended, so it can never shadow
        #     `git` or `python`.
        #
        #     NOT resolved through symlinks: a virtualenv's `python` is a link to
        #     the base interpreter, so resolving it lands in the BASE install's
        #     bin/ (e.g. Homebrew's) — which holds no `sqlmesh` and is exactly the
        #     directory the entry exists to avoid.
        self._guard_dir = ensure_guard_dir(
            settings.sandbox_tool_root or default_guard_root()
        )
        self._tool_dirs = (str(Path(sys.executable).absolute().parent),)

    @property
    def name(self) -> str:
        return "local"

    @property
    def supported_levels(self) -> frozenset[IsolationLevel]:
        # Honest: a child process is not a container, whatever the profile asks
        # for. The policy layer disables container profiles accordingly.
        return frozenset({IsolationLevel.PROCESS})

    async def execute(
        self,
        spec: TaskSpec,
        profile: IsolationProfile,
        credentials: Optional[SandboxCredentials] = None,
    ) -> TaskResult:
        """Run the task and return its result."""
        limits = ResourceLimits.from_profile(profile.settings)
        uid = self._target_uid(profile)

        with tempfile.TemporaryDirectory(prefix="datacode-task-") as workdir:
            spec_path = Path(workdir) / "spec.json"
            spec_path.write_text(spec.to_json(), encoding="utf-8")
            # The child runs as another uid where separation is available, so it
            # must be able to read the spec it was given.
            os.chmod(spec_path, 0o644)
            os.chmod(workdir, 0o755)

            return await self._run_child(spec, spec_path, limits, uid, credentials)

    async def _run_child(
        self,
        spec: TaskSpec,
        spec_path: Path,
        limits: ResourceLimits,
        uid: Optional[int],
        credentials: Optional[SandboxCredentials] = None,
    ) -> TaskResult:
        """Spawn the runner, wait for it, and turn its output into a result."""
        extra = {"DATACODE_TASK_ID": spec.task_id}
        if credentials is not None:
            # Merged into the child's environment because a child process has no
            # Secret to mount. The values never enter a log: build_clean_env does
            # not log, and the credential summary that IS logged names keys only.
            extra.update(credentials.env)

        env = build_clean_env(
            workspace_path=spec.workspace_path,
            environment=spec.environment,
            python_path=self._python_path,
            guard_dir=self._guard_dir,
            tool_dirs=self._tool_dirs,
            extra=extra,
        )

        def _preexec() -> None:
            # Runs in the child between fork and exec: the last point where the
            # sandbox can be tightened, and the logging machinery must not be
            # touched here.
            os.setsid()
            apply_rlimits(limits)
            if uid is not None:
                try_uid_separation(uid)

        logger.info(
            "task_local_start",
            task_id=spec.task_id,
            kind=spec.kind,
            profile=spec.isolation_profile,
            timeout_s=spec.timeout_seconds,
            # REQUESTED, not applied: the drop happens post-fork, so this side
            # cannot observe the outcome. _interpret() reports whether it
            # actually took effect, because an operator judging threat coverage
            # must not read this line as a guarantee.
            uid_separation_requested=uid is not None,
        )

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "src.main",
            str(spec_path),
            cwd=str(self._runner_root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_preexec,  # noqa: PLW1509 — the hardening IS the point
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=spec.timeout_seconds
            )
        except asyncio.TimeoutError:
            await self._terminate(process, spec.task_id)
            return TaskResult.failure(
                spec.task_id,
                ERR_TIMEOUT,
                f"task exceeded its {spec.timeout_seconds}s budget "
                "and was terminated by the backend",
                status=TaskStatus.TIMED_OUT,
            )

        return self._interpret(spec, process.returncode, stdout, stderr)

    def _interpret(
        self,
        spec: TaskSpec,
        exit_code: Optional[int],
        stdout: bytes,
        stderr: bytes,
    ) -> TaskResult:
        """Prefer the runner's own result; synthesize one only if it produced none."""
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        err_tail = err_text[-settings.task_log_tail_bytes :]

        # The child writes this marker to fd 2 when the uid drop failed. Surfaced
        # here so an operator reading the logs learns which threats are actually
        # covered, instead of inferring full coverage from the profile name.
        if _UID_SEPARATION_FAILED_MARKER in err_text:
            logger.warning(
                "task_local_uid_separation_inactive",
                task_id=spec.task_id,
                profile=spec.isolation_profile,
                hint="the task ran under the platform's own uid; grant "
                "CAP_SETUID/CAP_SETGID, or accept that this vehicle provides "
                "env-clear + rlimits only",
            )

        result = extract_result(out_text.splitlines())
        if result is not None:
            logger.info(
                "task_local_finished",
                task_id=spec.task_id,
                status=result.status.value,
                duration_ms=result.duration_ms,
                error_code=result.error_code or None,
            )
            return result

        # No result line: the child died before reporting. Its exit code and
        # stderr are all the evidence there is, so both go into the record.
        logger.error(
            "task_local_no_result",
            task_id=spec.task_id,
            exit_code=exit_code,
            stderr_tail=err_tail[-2048:],
        )
        return TaskResult.failure(
            spec.task_id,
            ERR_NO_RESULT,
            f"the task vehicle exited with code {exit_code} without reporting a "
            f"result; stderr: {err_tail[-2048:] or '(empty)'}",
            exit_code=exit_code,
        )

    @staticmethod
    def _target_uid(profile: IsolationProfile) -> Optional[int]:
        """Resolve the uid the child should drop to, if the profile asks for one.

        ``run_as_user_base`` is a per-slot base in the profile. Tasks are
        one-shot and hold no slot, so the base itself is used: separation from
        the PLATFORM uid is what matters here, not separation between tasks
        (which is what the container profiles provide).
        """
        base = profile.settings.get("run_as_user_base")
        if base is None:
            base = profile.settings.get("run_as_user")
        return int(base) if base is not None else None

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process, task_id: str) -> None:
        """Stop an overrunning child: SIGTERM to its group, then SIGKILL."""
        if process.returncode is not None:
            return
        try:
            # The child called setsid(), so signal the whole group — a task that
            # spawned helpers must not leave them behind.
            os.killpg(os.getpgid(process.pid), 15)
        except (ProcessLookupError, PermissionError):
            process.terminate()

        try:
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass

        logger.warning("task_local_kill", task_id=task_id, pid=process.pid)
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (ProcessLookupError, PermissionError):
            process.kill()
        # Reap it so the event loop does not keep a zombie transport around. A
        # child that still cannot be reaped must not turn a completed task into
        # an exception.
        with suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)


__all__ = ["LocalProcessBackend"]
