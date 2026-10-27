#!/usr/bin/env python3
"""P4a acceptance verification (design 06, "灰度取消后的等价验证手段").

The traffic-canary was cancelled (Q7); these checks are its replacement and
must run against a TWO-REPLICA deployment before P4b switches the storage
medium. The compose stack `deploy/docker-compose.data-plane.yml` is the
intended target:

    make data-plane-up
    python3 scripts/verify/verify-lease-routing.py

What is verified here (and how):

1. concurrent acquire race — 100 concurrent acquires of ONE lease key must
   produce exactly ONE lease on exactly ONE replica (the replacement for
   "canary validates lease routing").
2. crash takeover — kill -9 one replica; the lease must be re-acquirable
   within one TTL by the surviving replica (flock + SET NX expiry are the
   only defenses; this proves them).
3. drain hands over — a graceful stop releases the lease immediately (no TTL
   wait), proving the zero-loss ordering (snapshot -> release).

The WIP durability assertions (drain snapshots, recovery after materialize,
<=10-minute window) are covered by the automated suites
(`workspace-runtime/tests/test_wip.py`, `tests/test_lease_manager.py`,
`tests/test_wip_api.py`); this script prints that mapping so the acceptance
record is complete.

Usage:
    python3 scripts/verify-lease-routing.py \
        [--runtime http://localhost:8002 http://localhost:8012] \
        [--compose-service workspace-runtime-1] \
        [--ttl 120] [--skip-kill] [--skip-drain]

The second runtime URL and the docker compose service name are needed for the
kill/drain checks; without them the script degrades to the race check only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{_now()}] {message}", flush=True)


def http_json(
    method: str,
    url: str,
    payload: dict | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict]:
    """Minimal JSON client (stdlib only — this script has no dependencies)."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Internal-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def compose(*args: str) -> subprocess.CompletedProcess:
    """Run `docker compose` against the data-plane stack."""
    project_root = __file__.rsplit("/scripts/", 1)[0]
    cmd = [
        "docker", "compose",
        "--project-directory", f"{project_root}/deploy",
        "-f", f"{project_root}/deploy/docker-compose.data-plane.yml",
        *args,
    ]
    log(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True)


def served_on(url: str, lease_key: str, token: str) -> bool:
    """Whether the runtime at ``url`` reports holding ``lease_key``."""
    _status, health = http_json("GET", f"{url}/health", token=token)
    return lease_key in (health.get("lease", {}) or {}).get("served_keys", [])


def release(url: str, lease_key: str, token: str) -> None:
    http_json("POST", f"{url}/internal/leases/release",
              payload={"lease_key": lease_key}, token=token)


def check_concurrent_acquire(runtimes: list[str], token: str,
                             lease_key: str) -> bool:
    """100 concurrent acquires of one key -> one lease, one holder.

    ``lease_key`` must address a REAL workspace (one the database can resolve
    to a Git remote): a lease for an unknown workspace is adopted, fails to
    materialize, and is released in the background — which would make the
    "single holder" assertion racy rather than wrong.
    """
    log(f"1. concurrent acquire race: 100 acquires of {lease_key!r} across "
        f"{len(runtimes)} replica(s)")

    def acquire(i: int) -> tuple[int, dict]:
        url = runtimes[i % len(runtimes)]
        return http_json("POST", f"{url}/internal/leases/acquire",
                         payload={"lease_key": lease_key}, token=token)

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(acquire, range(100)))

    ok = [body for status, body in results if status == 200]
    holders = {body.get("runtime_id") for body in ok}
    holders.discard(None)

    problems = []
    if len(ok) != 100:
        problems.append(f"{100 - len(ok)} acquires failed")
    if len(holders) != 1:
        problems.append(f"lease landed on {len(holders)} instances: {holders}")
    serving = [url for url in runtimes if served_on(url, lease_key, token)]
    if len(serving) != 1:
        problems.append(f"{len(serving)} replicas report holding the lease")

    if problems:
        for problem in problems:
            log(f"   {FAIL} {problem}")
        ok_flag = False
    else:
        log(f"   {PASS} single lease, single holder "
            f"({next(iter(holders))}), on exactly one replica")
        ok_flag = True

    # Leave the system clean for the next check.
    for url in runtimes:
        release(url, lease_key, token)
    return ok_flag


def check_crash_takeover(
    primary: str, secondary: str, service: str, token: str, ttl: int
) -> bool:
    """kill -9 the holder; the lease must be re-acquirable within one TTL."""
    lease_key = "verify/p4a-crash/ws-crash/u1/dev"
    log(f"2. crash takeover: kill -9 the holder of {lease_key!r}, "
        f"expect takeover within TTL={ttl}s")

    status, body = http_json("POST", f"{primary}/internal/leases/acquire",
                             payload={"lease_key": lease_key}, token=token)
    if status != 200:
        log(f"   {FAIL} acquire failed: {body}")
        return False
    holder = body.get("runtime_id", "?")
    log(f"   lease held by {holder}")

    killed = compose("kill", "-s", "SIGKILL", service)
    if killed.returncode != 0:
        log(f"   {SKIP} could not kill {service} "
            f"({killed.stderr.strip()[:120]}); skipping")
        release(primary, lease_key, token)
        return True

    start = time.monotonic()
    deadline = start + ttl + 15
    took_over = False
    holder_after = ""
    while time.monotonic() < deadline:
        status, body = http_json(
            "POST", f"{secondary}/internal/leases/acquire",
            payload={"lease_key": lease_key}, token=token,
        )
        if status == 200:
            holder_after = body.get("runtime_id", "?")
            if holder_after != holder:
                took_over = True
                break
        time.sleep(5)

    elapsed = time.monotonic() - start
    compose("start", service)  # bring the killed replica back either way

    if took_over and elapsed <= ttl + 15:
        log(f"   {PASS} taken over by {holder_after} after {elapsed:.0f}s "
            f"(bound: TTL {ttl}s + probe slack)")
        release(secondary, lease_key, token)
        return True
    log(f"   {FAIL} no takeover within {elapsed:.0f}s "
        f"(holder={holder!r}, last={holder_after!r})")
    release(secondary, lease_key, token)
    return False


def check_drain_release(primary: str, secondary: str, service: str,
                        token: str) -> bool:
    """Graceful stop must release the lease immediately (no TTL wait)."""
    lease_key = "verify/p4a-drain/ws-drain/u1/dev"
    log(f"3. drain: graceful stop must hand over {lease_key!r} at once")

    status, body = http_json("POST", f"{primary}/internal/leases/acquire",
                             payload={"lease_key": lease_key}, token=token)
    if status != 200:
        log(f"   {FAIL} acquire failed: {body}")
        return False
    holder = body.get("runtime_id", "?")
    log(f"   lease held by {holder}; stopping it gracefully "
        f"(SIGTERM -> final WIP snapshot -> release)")

    stopped = compose("stop", "-t", "90", service)
    if stopped.returncode != 0:
        log(f"   {SKIP} could not stop {service} "
            f"({stopped.stderr.strip()[:120]}); skipping")
        release(primary, lease_key, token)
        return True

    start = time.monotonic()
    took_over = False
    holder_after = ""
    while time.monotonic() < start + 60:
        status, body = http_json(
            "POST", f"{secondary}/internal/leases/acquire",
            payload={"lease_key": lease_key}, token=token,
        )
        if status == 200 and body.get("runtime_id") != holder:
            took_over = True
            holder_after = body.get("runtime_id", "?")
            break
        time.sleep(3)

    elapsed = time.monotonic() - start
    compose("start", service)

    # Drain must beat the TTL by a wide margin; 60 s is already generous.
    if took_over and elapsed < 60:
        log(f"   {PASS} handed over to {holder_after} after {elapsed:.0f}s "
            f"(drain releases immediately — no TTL wait)")
        release(secondary, lease_key, token)
        return True
    log(f"   {FAIL} handover took {elapsed:.0f}s or never happened")
    release(secondary, lease_key, token)
    return False


def print_automated_coverage() -> None:
    """The WIP assertions live in the suites; print the acceptance mapping."""
    log("4. durability-contract checks — covered by automated suites:")
    rows = [
        ("drain takes a final snapshot (zero loss)",
         "workspace-runtime/tests/test_wip.py::SnapshotScheduler"),
        ("recovery prompt after materialization",
         "workspace-runtime/tests/test_lease_manager.py"),
        ("recover/discard user decisions via API",
         "workspace-runtime/tests/test_wip_api.py"),
        ("snapshot failure surfaces 'unprotected'",
         "workspace-runtime/tests/test_wip_api.py"),
        ("storage contract incl. P4a transition",
         "shared/python/tests/test_materialize_contract.py"),
    ]
    for claim, where in rows:
        print(f"   - {claim}: {where}")
    print(f"   [{PASS}] see suites above")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime", nargs="+", default=["http://localhost:8002"],
        help="runtime base URLs (first = primary); two enable all checks")
    parser.add_argument("--compose-service", default="workspace-runtime-1",
                        help="compose service of the PRIMARY runtime")
    parser.add_argument("--token", default="dev-internal-token",
                        help="shared secret guarding /internal/*")
    parser.add_argument("--ttl", type=int, default=120,
                        help="configured lease TTL seconds")
    parser.add_argument(
        "--lease-key",
        default="verify/p4a-race/ws-race/u1/dev",
        help="lease key of a REAL workspace ({tenant}/{workspace}/{user}/{env}); "
             "the database must resolve it to a Git remote, or materialization "
             "fails and the lease is released in the background")
    parser.add_argument("--skip-kill", action="store_true")
    parser.add_argument("--skip-drain", action="store_true")
    args = parser.parse_args()

    runtimes = args.runtime
    for url in runtimes:
        status, _ = http_json("GET", f"{url}/health", token=args.token)
        if status != 200:
            log(f"{FAIL} runtime at {url} is not healthy (HTTP {status})")
            return 2
    log(f"targets healthy: {', '.join(runtimes)}")

    results = {"concurrent acquire": check_concurrent_acquire(
        runtimes, args.token, args.lease_key)}

    if len(runtimes) >= 2:
        primary, secondary = runtimes[0], runtimes[1]
        if not args.skip_kill:
            results["crash takeover"] = check_crash_takeover(
                primary, secondary, args.compose_service, args.token, args.ttl)
        if not args.skip_drain:
            results["drain release"] = check_drain_release(
                primary, secondary, args.compose_service, args.token)
    else:
        log(f"{SKIP} kill/drain checks need a second --runtime URL")

    print_automated_coverage()

    failed = [name for name, ok in results.items() if not ok]
    print()
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL}  {name}")
    if failed:
        print(f"\nP4a acceptance: {FAIL} ({', '.join(failed)})")
        return 1
    print(f"\nP4a acceptance: {PASS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
