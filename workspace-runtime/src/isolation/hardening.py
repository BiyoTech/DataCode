"""Process-level sandbox primitives, shared by every process-based vehicle.

Two things in this runtime start a child process that may execute user code:

* the **persistent L3-proc executor** (``src/executor/process.py``), which keeps
  a SQLMesh Context hot behind the frozen frame protocol;
* the **local task backend** (``src/tasks/backends/local.py``), which runs a
  one-shot task-runner where there is no container platform.

They differ in lifetime and transport, not in hardening — and hardening is the
part that must not drift. A second copy of "clear the environment" that forgets
one variable is indistinguishable from no hardening at all, so the primitives
live here and both callers import them.

What these primitives do NOT provide: a filesystem namespace, a network
namespace, or protection from a process reading another working copy on the same
volume. That is the honest boundary of L3-proc (see
``conf/isolation-profiles.yaml``), and the reason P6 introduces containers.
"""

from __future__ import annotations

import os
import resource
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from datacode_common.workspace_layout import sqlmesh_home, tool_home

# Environment variables a child is allowed to inherit by PREFIX. Only the
# working copy's own warehouse gateway settings qualify: the user configured
# them, so reading them back is within their authority (Q5 decision). Platform
# secrets never match.
INHERITED_ENV_PREFIXES = ("DATACODE_WAREHOUSE_",)

# Minimal PATH. Not inherited: a PATH pointing into a user-writable directory
# would let a task shadow `git` or `python` for anything that runs after it.
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


def sandbox_path(
    *, guard_dir: str = "", tool_dirs: Sequence[str] = ()
) -> str:
    """The PATH a sandboxed child gets, in the order that makes each part work.

    Three segments, and the ORDER of each is a decision:

    1. ``guard_dir`` — PREPENDED. It holds the generated governance guards, one
       script per governed program (``sqlmesh``, ``dc``). Appending them would
       make them useless whenever the host has a real ``sqlmesh`` in
       ``/usr/local/bin``, which is where pip installs one. Prepending cannot
       shadow ``git`` or ``python`` — by construction the directory contains
       nothing but the governed program names (see
       ``datacode_common.command_verbs.GOVERNED_PROGRAMS``).
    2. :data:`DEFAULT_PATH` — the system directories. Never inherited: a PATH
       pointing into a user-writable directory would let a task shadow ``git``
       or ``python`` for anything that runs after it.
    3. ``tool_dirs`` — APPENDED. Platform-owned directories holding the real
       tools where they are not in a system directory: the task-runner's
       virtualenv ``bin/`` in a development install. Appended precisely so they
       cannot shadow a system binary. A container image installs its tools into
       ``/usr/local/bin`` and passes none of these.

    Non-existent directories are dropped rather than passed through: a PATH entry
    that does not exist is harmless, but keeping it hides which of several
    candidates the deployment actually resolved.
    """
    entries: list[str] = []
    if guard_dir and Path(guard_dir).is_dir():
        entries.append(guard_dir)
    entries.append(DEFAULT_PATH)
    for directory in tool_dirs:
        candidate = str(directory or "").strip()
        if candidate and Path(candidate).is_dir():
            entries.append(candidate)
    return os.pathsep.join(entries)



@dataclass(frozen=True)
class ResourceLimits:
    """Soft resource limits applied to a child process.

    Only soft limits are touched: an unprivileged process may always tighten
    them, which is the direction we want, and raising a hard limit would need
    privileges this process should not have.
    """

    address_space_bytes: int = 4 * 1024**3
    cpu_seconds: int = 1800
    max_processes: int = 256
    max_file_size_bytes: int = 2 * 1024**3
    max_open_files: int = 1024

    @classmethod
    def from_profile(cls, profile_settings: Mapping[str, object]) -> "ResourceLimits":
        """Build limits from an isolation profile's ``rlimits`` block.

        The profile is the authoritative declaration of a level's constraints,
        so a vehicle that can read it should not also carry its own copy of the
        numbers.
        """
        raw = profile_settings.get("rlimits") or {}
        if not isinstance(raw, Mapping):
            return cls()
        defaults = cls()
        return cls(
            address_space_bytes=int(
                raw.get("address_space_bytes", defaults.address_space_bytes)
            ),
            cpu_seconds=int(raw.get("cpu_seconds", defaults.cpu_seconds)),
            max_processes=int(raw.get("max_processes", defaults.max_processes)),
            max_file_size_bytes=int(
                raw.get("max_file_size_bytes", defaults.max_file_size_bytes)
            ),
            max_open_files=int(raw.get("max_open_files", defaults.max_open_files)),
        )


def build_clean_env(
    *,
    workspace_path: str,
    environment: str = "",
    python_path: Sequence[str] = (),
    guard_dir: str = "",
    tool_dirs: Sequence[str] = (),
    extra: Optional[Mapping[str, str]] = None,
    source_env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:

    """Build a child environment from scratch, inheriting almost nothing.

    THE point of the process-level profiles: the child starts with an EMPTY
    environment and only the entries below are re-injected, so platform secrets
    (``DB_PASSWORD``, LLM keys, ``TOKEN_ENCRYPTION_SECRET``, internal tokens)
    cannot be read from the child even if the code it runs is fully malicious.

    The HOME directory is NOT created here: this builds an environment and must
    not touch a filesystem. Every tool that needs its home creates it (SQLMesh
    does), and a missing home is harmless for the ones that only read from it.

    Args:
        workspace_path: The one working copy this child may touch. Its HOME is
            a platform-owned directory INSIDE it, so a tool writing a dotfile
            stays in the sandbox without dropping the file among the user's
            models — and without a committed ``.gitconfig`` at the copy's root
            becoming the child's global Git configuration.
        environment: SQLMesh environment name, exposed for diagnostics.
        python_path: Package roots the child must import from. Filesystem
            locations, not secrets.
        guard_dir: Directory holding the governance guards, PREPENDED to PATH
            (see :func:`sandbox_path`). Empty leaves them out, which is a
            governance gap the caller is expected to have logged.
        tool_dirs: Platform-owned directories APPENDED to PATH — where the real
            tools live in a development install. Empty in a container, whose
            image installs them into the system directories.
        extra: Additional variables to inject. Callers must not pass platform
            credentials; this function cannot tell them apart from data.
        source_env: Environment to take prefix-matched variables from. Defaults
            to this process's. Injectable for tests, which must not have to
            mutate the real environment to check what is filtered.

    Returns:
        A fresh mapping; the caller's own environment is never modified.
    """
    env: dict[str, str] = {
        "PATH": sandbox_path(guard_dir=guard_dir, tool_dirs=tool_dirs),
        "HOME": tool_home(workspace_path),
        # Where SQLMesh keeps its personal directory. Redundant with HOME by
        # construction (it resolves to the same place), and set anyway because
        # SQLMesh reads it at IMPORT time: a child that imports the library
        # before anything sets HOME still lands in the platform-owned tree
        # rather than writing a telemetry id — or reading an attacker-supplied
        # personal config — next to the user's project.
        "SQLMESH_HOME": sqlmesh_home(workspace_path),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # UTC everywhere: the platform stores and compares UTC, and a child
        # inheriting a local zone would render timestamps that disagree.
        "TZ": "UTC",
        "PYTHONUNBUFFERED": "1",
        "DATACODE_WORKSPACE_PATH": workspace_path,
        "DATACODE_ENVIRONMENT": environment,
    }

    if python_path:
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in python_path if p)

    for key, value in (source_env if source_env is not None else os.environ).items():
        if key.startswith(INHERITED_ENV_PREFIXES):
            env[key] = value

    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def lower_limit(res: int, target: int, *, best_effort: bool = False) -> None:
    """Set a SOFT limit to ``target`` without ever raising the hard limit.

    If the current hard limit is already below target, clamp to it — a lower cap
    than configured is safe (stricter); the reverse is not.

    ``best_effort`` swallows platform quirks (macOS rejects ``RLIMIT_AS`` below
    the current unlimited value; Linux, the production target, accepts it). A
    skipped limit is written to fd 2 rather than logged, because this runs
    post-fork where the logging machinery must not be touched.
    """
    _soft, hard = resource.getrlimit(res)
    try:
        resource.setrlimit(res, (min(target, hard), hard))
    except (OSError, ValueError) as exc:
        if not best_effort:
            raise
        os.write(2, f"sandbox_rlimit_skipped res={res} error={exc}\n".encode())


def apply_rlimits(limits: ResourceLimits) -> None:
    """Lower the CURRENT process's resource limits. Call in the child.

    Platform notes (both are macOS-dev quirks; Linux enforces everything):
      * ``RLIMIT_AS``: macOS rejects values below the current unlimited one.
      * ``RLIMIT_NPROC``: macOS counts the whole USER's processes, so a busy dev
        machine trips the cap on every fork. Applied on Linux only, where it is
        per-process and actually stops fork bombs.
    """
    lower_limit(resource.RLIMIT_AS, limits.address_space_bytes, best_effort=True)
    lower_limit(resource.RLIMIT_CPU, limits.cpu_seconds, best_effort=True)
    if sys.platform == "linux":
        lower_limit(resource.RLIMIT_NPROC, limits.max_processes)
    lower_limit(resource.RLIMIT_FSIZE, limits.max_file_size_bytes)
    lower_limit(resource.RLIMIT_NOFILE, limits.max_open_files)


def try_uid_separation(target_uid: int) -> bool:
    """Move the current process to a dedicated unprivileged uid. Call in the child.

    Returns True when applied. Requires CAP_SETUID/CAP_SETGID (root or a
    capability-bearing container). In unprivileged dev the caller keeps the
    env-clear + rlimit core of the profile and must report that uid separation
    is inactive, so an operator knows exactly which threats are covered rather
    than assuming all of them are.
    """
    try:
        os.setgroups([])
        os.setgid(target_uid)
        os.setuid(target_uid)
        return True
    except (PermissionError, OSError) as exc:
        os.write(
            2,
            f"sandbox_uid_separation_unavailable uid={target_uid} error={exc}\n".encode(),
        )
        return False


def platform_secret_names(env: Optional[Mapping[str, str]] = None) -> list[str]:
    """List variables in ``env`` that must never reach a child.

    Used by tests and by the security verification script (design 06): the
    assertion "no platform credential is visible in the sandbox" needs a
    concrete list to check against, and deriving it here keeps that list from
    diverging from what the platform actually sets.
    """
    markers = ("PASSWORD", "SECRET", "TOKEN", "_KEY", "APIKEY", "CREDENTIAL")
    source: Iterable[str] = (env if env is not None else os.environ).keys()
    return sorted(
        name
        for name in source
        if any(marker in name.upper() for marker in markers)
        # The working copy's own warehouse settings are the user's, not the
        # platform's, and are inherited on purpose.
        and not name.startswith(INHERITED_ENV_PREFIXES)
    )


__all__ = [
    "DEFAULT_PATH",
    "INHERITED_ENV_PREFIXES",
    "ResourceLimits",
    "apply_rlimits",
    "build_clean_env",
    "lower_limit",
    "platform_secret_names",
    "sandbox_path",
    "try_uid_separation",
]
