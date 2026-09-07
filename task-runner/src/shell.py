"""Gated shell execution — the agent's L3 vehicle (design 04 §3.6, P6c).

``agent.shell`` runs arbitrary user commands. Its vehicle is whatever the
isolation policy picks for the kind: an L3-base container in production, the
hardened one-shot process in dev. What this module owns is the contract that
holds in BOTH:

* the command runs with the working copy as cwd and nothing else;
* the environment the VEHICLE built is the environment the command sees — see
  ``_SHELL_ARGV`` for why that needs saying;
* git hooks are disabled — a repository the user pushed can carry a
  ``post-checkout`` hook, and running it here would be executing user code the
  platform never saw (the same reason the runtime's own git calls carry the
  flag explicitly);
* the output shape is the one the executor's shell op used to return, so
  agent-service's tool schema is unchanged by the migration (the op itself is
  gone — shell is a task now, see ``datacode_common.executor_protocol``).

What is deliberately NOT here: environment hardening. The vehicle applies it
(the local backend clears the environment and applies rlimits; the container
profile declares its own). This module must not re-implement any of it, because
a second copy of "clear the environment" that forgets one variable is
indistinguishable from not clearing it at all.
"""

from __future__ import annotations

import subprocess
from typing import Any, Mapping

from src.sqlmesh_ops import SqlMeshOpError

# Output caps: the result travels back through the structured result line, so
# unbounded output would balloon the frame. Tail-truncation keeps the END of the
# output — for a build or test log the end is where the error is.
_MAX_OUTPUT_CHARS = 65536

# Mirrors the executor's shell op; restated because the two processes must not
# import each other's modules.
_GIT_HOOKS_DISABLED = "'core.hooksPath=/dev/null'"

# NON-LOGIN, non-interactive shell. `-l` was actively harmful and the reason is
# worth keeping in front of whoever edits this line:
#
#   * a login shell sources /etc/profile, which on Debian (this image's base)
#     OVERWRITES PATH unconditionally. That silently discarded the PATH the
#     vehicle built — including the governance guard directory, whose whole job
#     is to refuse `sqlmesh apply` when it arrives inside a compound command;
#   * it also sources ~/.profile, and HOME points INSIDE the working copy. A
#     `.profile` committed to the user's repository would then execute before
#     every single command, with the sandbox's environment.
#
# Neither is something a sandbox should offer. A non-login shell keeps exactly
# the environment the vehicle assembled.
_SHELL_ARGV = ("bash", "-c")


def op_shell(
    workspace_path: str,
    params: Mapping[str, Any],
    *,
    gateway: str = "",
    environment: str = "",
    sql_only: bool = False,
    project_subdir: str = "",
    progress: Any = None,
) -> dict[str, Any]:
    """Execute one shell command in the working copy.

    Args:
        workspace_path: The sandbox's mount point — the ONLY directory the
            command can meaningfully reach.
        params: ``command`` (required) and ``timeout_s`` (default 120).
        gateway: Unused here; accepted for handler-signature uniformity.
        environment: Unused here; same.
        sql_only: Unused (a shell command is never SQL-only); same.
        project_subdir: Unused; same.

    Returns:
        ``{exit_code, stdout, stderr, timed_out}`` — the shape the REST shell
        endpoint has always returned.
    """
    del gateway, environment, sql_only, project_subdir, progress

    command = str(params.get("command") or "").strip()
    if not command:
        raise SqlMeshOpError("bad_spec", "shell requires a non-empty 'command'")

    timeout = min(max(float(params.get("timeout_s") or 120), 1.0), 1800.0)

    import os

    env = dict(os.environ)
    # A pushed repository can carry hooks; running them would be executing code
    # the platform never inspected. Best-effort: a command that re-configures
    # git explicitly can still run hooks, and that is within what "arbitrary
    # shell" means — the vehicle boundary, not this flag, is the real control.
    env["GIT_CONFIG_PARAMETERS"] = _GIT_HOOKS_DISABLED

    try:
        completed = subprocess.run(  # noqa: S603 — the whole point is gated exec
            [*_SHELL_ARGV, command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workspace_path,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "timed out",
            "timed_out": True,
        }

    return {
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[-_MAX_OUTPUT_CHARS:],
        "stderr": (completed.stderr or "")[-_MAX_OUTPUT_CHARS:],
        "timed_out": False,
    }


__all__ = ["op_shell"]
