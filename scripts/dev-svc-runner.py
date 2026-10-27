#!/usr/bin/env python3
"""Per-service dev supervisor used by the dev scripts.

Why this exists
---------------
dev-start.sh needs every service in its own session/process group so a
single ``kill -- -PGID`` reaches the WHOLE tree (go run + compiled binary,
uvicorn reloader + reload worker, npm + vite). A plain background job cannot
do that: without job control all children share the script's group, and
killing that group would kill the dev script itself.

What it does
------------
1. calls ``setsid()`` — becomes session + group leader; its PID (which the
   dev script captured via ``$!``) IS the process-group id of the tree;
2. spawns the service command with stdout+stderr merged into one pipe;
3. prefixes every line with ``[name] `` and writes it to BOTH the console
   and a per-run log file (truncated at start), flushed line by line;
4. forwards SIGTERM/SIGINT to the service (a group signal already reaches
   it, but a targeted kill of the supervisor alone must not orphan it);
5. exits with the service's exit code.

Usage:
    dev-svc-runner.py --name NAME --log FILE --cwd DIR -- CMD [ARGS...]

Options must appear before ``--``; everything after it is the command.
"""

import argparse
import os
import signal
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="dev service supervisor")
    parser.add_argument("--name", required=True, help="console prefix")
    parser.add_argument("--log", required=True, help="log file (truncated per run)")
    parser.add_argument("--cwd", required=True, help="working directory for the command")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("no command given (use: -- CMD [ARGS...])")

    # New session: this process becomes session & group leader, so the PID
    # the dev script captured doubles as the PGID of the whole service tree.
    # Refuse to run without it — a shared group would make the script's
    # group-kill terminate the dev script itself.
    try:
        os.setsid()
    except OSError as exc:
        print(f"dev-svc-runner: setsid failed ({exc}); refusing to start {cmd[0]}", file=sys.stderr)
        return 125

    log_fd = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)

    child = subprocess.Popen(
        cmd,
        cwd=args.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    def _forward(signum, _frame):
        # Forward to the direct child only; it propagates to its own
        # children (uvicorn reloader -> worker) as designed.
        try:
            os.kill(child.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    prefix = f"[{args.name}] ".encode()
    stream = child.stdout
    assert stream is not None
    console_alive = True
    while True:
        line = stream.readline()
        if not line:
            break
        os.write(log_fd, line)
        if console_alive:
            try:
                sys.stdout.buffer.write(prefix + line)
                sys.stdout.buffer.flush()
            except BrokenPipeError:
                # The console reader (the dev script's terminal) is gone —
                # e.g. the script itself was SIGKILLed. Keep mirroring into
                # the log file; the services are stopped via process groups.
                console_alive = False
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())

    rc = child.wait()
    tail = f"[{args.name}] exited with code {rc}\n".encode()
    os.write(log_fd, tail)
    try:
        sys.stdout.buffer.write(tail)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        pass
    os.close(log_fd)
    return rc


if __name__ == "__main__":
    sys.exit(main())
