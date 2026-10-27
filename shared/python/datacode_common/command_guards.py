"""Generating the governance guards a sandbox carries on PATH (design 04 §3.6).

The command router in workspace-runtime serves the governed verbs
(``sqlmesh apply``, ``dc run``, ...) itself. But routing only applies to a SINGLE
SIMPLE COMMAND: a string carrying shell operators cannot be split into argv
without a shell, so ``sqlmesh apply prod && echo done`` reaches the sandbox as a
whole — where the native binary would happily deploy with no environment lock, no
apply history and no approval.

These generated scripts close that gap. They shadow the governed PROGRAMS on the
sandbox's PATH, refuse the governed VERBS with the same message the router uses,
and forward everything else to the real binary — so ``sqlmesh test``,
``sqlmesh audit``, ``sqlmesh dag`` and ``dc -e '2 2 + p'`` are unaffected.

Why generated, and why here
---------------------------
The verb list and the refusal messages live in :mod:`datacode_common.command_verbs`,
the same declaration the router dispatches on. A committed shell script would be
a second copy, and the two drifting IS the bypass: a verb the router does not
claim and the guard does not refuse runs natively.

It lives in the shared package because two vehicles need it at two different
times: the local backend writes the scripts at startup, while a container image
BAKES them at build time (into a root-owned directory the unprivileged task
cannot rewrite) via ``python -m datacode_common.command_guards <dir>``.
"""

from __future__ import annotations

import shlex
import stat
import sys
from pathlib import Path

from datacode_common.command_verbs import GOVERNED_PROGRAMS, GOVERNED_VERBS

# Directory name the guards are written into, under whichever root a vehicle
# chooses. Shared so a container's baked path and a local run's generated path
# have the same shape.
GUARD_DIR_NAME = "command-guards"

# Where a container image bakes them. Root-owned and outside every writable
# mount, so the unprivileged task cannot replace a guard with a passthrough.
CONTAINER_GUARD_ROOT = "/opt/datacode"


def render_guard_script(program: str) -> str:
    """Render the guard for one governed program.

    The script resolves the real binary with its OWN directory stripped from
    PATH, so it cannot recurse into itself regardless of where it is installed
    or how many times the directory appears.
    """
    cases = []
    for verb, message in GOVERNED_VERBS.items():
        if verb[0] != program:
            continue
        tail = verb[1:]
        # Single-token verbs today. A longer one needs its own pattern rather
        # than matching on a prefix, which is why the join is explicit.
        pattern = tail[0] if len(tail) == 1 else " ".join(tail)
        cases.append(
            f"    {shlex.quote(pattern)})\n"
            f"        _refuse {shlex.quote(message)}\n"
            f"        ;;"
        )
    case_body = "\n".join(cases)

    return f"""#!/usr/bin/env bash
# GENERATED from datacode_common.command_verbs. Do not edit.
#
# Governance guard for `{program}`: refuses the verbs the platform serves itself,
# forwards everything else to the real binary.
set -uo pipefail

_GUARD_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"

_refuse() {{
    echo "[datacode] $1" >&2
    exit 1
}}

# Resolve the real binary with this guard's directory removed from PATH, so the
# lookup can never find us again.
_real_binary() {{
    local cleaned="" dir
    local IFS=':'
    for dir in $PATH; do
        [[ "$dir" == "$_GUARD_DIR" ]] && continue
        if [[ -z "$cleaned" ]]; then cleaned="$dir"; else cleaned="$cleaned:$dir"; fi
    done
    PATH="$cleaned" command -v {program} 2>/dev/null || true
}}

case "${{1:-}}" in
{case_body}
esac

_BIN="$(_real_binary)"
if [[ -z "$_BIN" ]]; then
    echo "[datacode] {program}: not installed in this sandbox" >&2
    exit 127
fi
exec "$_BIN" "$@"
"""


def write_guards(directory: str | Path) -> str:
    """Write every guard script into *directory* and return its path.

    Idempotent and unconditional: the content is derived from the verb
    declaration, so rewriting is how a change to that declaration reaches an
    existing deployment. Mode 0755 — readable and executable by the sandbox's
    unprivileged uid, writable only by the owner.

    Raises:
        OSError: The directory or a script could not be written. Callers decide
            what an absent guard directory means for them; this function does
            not guess.
    """
    target = Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    for program in sorted(GOVERNED_PROGRAMS):
        script = target / program
        script.write_text(render_guard_script(program), encoding="utf-8")
        script.chmod(
            stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        )
    return str(target)


def main(argv: list[str] | None = None) -> int:
    """``python -m datacode_common.command_guards <root>`` — bake the guards.

    Used by container image builds: the scripts are generated at BUILD time from
    the shared declaration, so the image carries no hand-written copy of the verb
    list and needs no network or platform access to produce them.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    root = args[0] if args else CONTAINER_GUARD_ROOT
    path = write_guards(Path(root) / GUARD_DIR_NAME)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTAINER_GUARD_ROOT",
    "GUARD_DIR_NAME",
    "main",
    "render_guard_script",
    "write_guards",
]
