"""Wiring the generated governance guards into this runtime's sandboxes.

The scripts themselves are rendered by ``datacode_common.command_guards`` from the
same verb declaration the command router dispatches on — see that module for why
they exist and why they are generated rather than committed. What is local to this
service is the POLICY around them:

* where they are written when nothing is configured;
* what happens when they cannot be written (a logged governance gap, not a
  startup failure — see :func:`ensure_guard_dir`).
"""

from __future__ import annotations

import os

from datacode_common.command_guards import GUARD_DIR_NAME, write_guards
from datacode_common.command_verbs import GOVERNED_PROGRAMS

from src.utils.logging import get_logger

logger = get_logger(__name__)


def default_guard_root() -> str:
    """Where guards live when the deployment names no directory.

    Under the platform's own state directory, NOT inside a working copy: every
    copy is writable by the task that mounts it, so a guard placed there could be
    replaced by exactly the code it guards against.
    """
    return os.path.expanduser("~/.datacode/sandbox")


def ensure_guard_dir(root: str) -> str:
    """Write the guard scripts under *root* and return the directory to add to PATH.

    Returns ``""`` when they could not be written. That degrades to "no guard
    directory on PATH", which is safe but weaker: the router still serves governed
    verbs, while a COMPOUND command containing one would reach the native binary.
    Logged at ERROR for that reason — it is a governance gap, not a warning — but
    not fatal, because refusing to boot would take down every ordinary shell
    command over a directory permission problem.
    """
    try:
        directory = write_guards(os.path.join(os.path.expanduser(root), GUARD_DIR_NAME))
    except OSError as exc:
        logger.error(
            "sandbox_command_guards_unavailable",
            root=root,
            error=str(exc),
            effect="a compound command containing a governed verb would reach the "
            "native binary unlocked and unaudited",
        )
        return ""

    logger.info(
        "sandbox_command_guards_ready",
        directory=directory,
        programs=sorted(GOVERNED_PROGRAMS),
    )
    return directory


__all__ = ["default_guard_root", "ensure_guard_dir"]
