#!/usr/bin/env python3
"""Multi-replica acceptance for control-plane lease routing (review P0-1).

The defect this guards against was invisible with one replica and nearly total
with several: control-service dispatched every env-control call to the headless
Service address, so each call reached an ARBITRARY replica — each with its own
volume. A workspace scaffolded on replica A and later leased to replica B
could not be opened; probes answered "nothing pending" about a disk that never
held the copy.

This script reproduces that topology with REAL processes:

* two real ``workspace-runtime`` servers (uvicorn), each on its OWN directory,
  so a file written by one is genuinely absent from the other;
* real Redis carrying a lease record and both replicas' heartbeats in the
  production wire format;
* control-service's production :class:`RuntimeClient` and :class:`GitBinder`,
  configured with replica A as the "Service address" — the address every call
  used to go to.

Assertions:

1. ``ensure-repository`` / ``ensure-worktree`` / ``scaffold`` land on the lease
   holder (B), and replica A's volume stays empty for the workspace;
2. the old behaviour is real: asking the Service address (A) for pending work
   reports zero — the "looks successful, is wrong" answer;
3. through the binder, pending work is reported correctly;
4. after the lease disappears (released / expired) while the files stay, the
   binder STILL reports the pending work — probes fan out instead of trusting
   the first replica that answers;
5. the member union and ``remove-worktree`` fan-out reach B's volume.

Seams: none in the code under test. The lease subsystem itself is OFF in the
two runtimes (it would try to materialize from a workspace row this throwaway
setup does not have); its Redis records are written here in the exact format
the holder writes, which is the contract the router reads.

Usage:
    python3 scripts/verify/verify-control-lease-routing.py

Needs: a reachable Redis (the dev stack's), and the control-service and
workspace-runtime venvs. Exit 0 = all passed, 1 = a failed assertion,
2 = a missing prerequisite.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONTROL_DIR = os.path.join(REPO_ROOT, "control-service")
RUNTIME_DIR = os.path.join(REPO_ROOT, "workspace-runtime")
CONTROL_PYTHON = os.path.join(CONTROL_DIR, ".venv", "bin", "python")
RUNTIME_PYTHON = os.path.join(RUNTIME_DIR, ".venv", "bin", "python")

TOKEN = "verify-control-routing-token"
# A cell id of our own: every Redis key is namespaced by it, so this run can
# neither read nor clobber a dev stack's leases.
CELL_ID = f"verify-{uuid.uuid4().hex[:8]}"


def _ensure_control_venv() -> None:
    """Re-exec inside control-service's venv (decided by interpreter path)."""
    if os.path.abspath(sys.executable) == os.path.abspath(CONTROL_PYTHON):
        return
    for path in (CONTROL_PYTHON, RUNTIME_PYTHON):
        if not os.path.exists(path):
            print(f"fatal: {path} not found — run scripts/dev-setup.sh", file=sys.stderr)
            sys.exit(2)
    os.execv(CONTROL_PYTHON, [CONTROL_PYTHON, os.path.abspath(__file__)] + sys.argv[1:])


_ensure_control_venv()

# Settled BEFORE the first control import: settings read these at construction.
os.environ.setdefault("DATACODE_ENV", "dev")
os.environ["DATACODE_CELL_ID"] = CELL_ID
os.environ["CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN"] = TOKEN
sys.path.insert(0, CONTROL_DIR)

import httpx  # noqa: E402
from datacode_common.lease import (  # noqa: E402
    RuntimeHeartbeat,
    encode_runtime_heartbeat,
    lease_redis_key,
    runtime_index_redis_key,
    runtime_redis_key,
)
from datacode_common.lease_routing import LeaseRouter  # noqa: E402
from datacode_common.naming import lease_key  # noqa: E402
from datacode_common.redis.client import get_redis, redis_available  # noqa: E402

from src.environments.git_binder import GitBinder  # noqa: E402
from src.runtime.client import RuntimeClient, RuntimeTarget  # noqa: E402
from src.runtime.fleet import RuntimeFleet  # noqa: E402

TENANT, WORKSPACE, USER, ENV = "t1", "ws-t1-routing", "u1", "prod"

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Replica:
    """One real workspace-runtime process on its own volume."""

    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.root = root
        self.base = root / "workspaces"
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None
        self.log = root / "runtime.log"

    @property
    def addr(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.addr}"

    def start(self) -> None:
        self.base.mkdir(parents=True)
        env = dict(os.environ)
        env.update(
            {
                "DATACODE_ENV": "dev",
                "DATACODE_CELL_ID": CELL_ID,
                "DATACODE_RUNTIME_ID": f"verify-{self.name}",
                "WORKSPACE_RUNTIME_INTERNAL_TOKEN": TOKEN,
                "WORKSPACE_RUNTIME_WORKSPACE_BASE_PATH": str(self.base),
                "WORKSPACE_RUNTIME_MATERIALIZE_ROOT": str(self.base),
                # Leases off (see the module docstring); a shared medium is the
                # only shape the storage contract accepts without them.
                "WORKSPACE_RUNTIME_LEASE_ENABLED": "false",
                "WORKSPACE_RUNTIME_MATERIALIZE_MOUNT_KIND": "shared",
                "WORKSPACE_RUNTIME_DAGSTER_HOME": str(self.root / "dagster_home"),
                "WORKSPACE_RUNTIME_DAGSTER_DAEMON_ENABLED": "false",
                "WORKSPACE_RUNTIME_DAGSTER_WEBSERVER_ENABLED": "false",
                "WORKSPACE_RUNTIME_DB_TYPE": "sqlite",
                "WORKSPACE_RUNTIME_DB_SQLITE_PATH": str(self.root / "runtime.db"),
                # `sqlmesh init` during scaffold resolves through PATH.
                "PATH": os.path.dirname(RUNTIME_PYTHON) + os.pathsep + env.get("PATH", ""),
            }
        )
        env.pop("DATABASE_URL", None)
        handle = open(self.log, "w")
        self.proc = subprocess.Popen(
            [RUNTIME_PYTHON, "-m", "uvicorn", "src.main:app",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=RUNTIME_DIR,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                if httpx.get(f"{self.url}/health", timeout=2).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def workspace_dir(self) -> Path:
        return self.base / TENANT / WORKSPACE

    def worktree(self) -> Path:
        return self.workspace_dir() / USER / "envs" / ENV


async def _publish_fleet(redis, replicas: list[Replica], key: str, holder: Replica) -> None:
    """Heartbeats for both replicas + the lease record, in production format."""
    now = datetime.now(timezone.utc)
    for replica in replicas:
        runtime_id = f"verify-{replica.name}"
        await redis.sadd(runtime_index_redis_key(), runtime_id)
        await redis.set(
            runtime_redis_key(runtime_id),
            encode_runtime_heartbeat(
                RuntimeHeartbeat(
                    runtime_id=runtime_id,
                    addr=replica.addr,
                    lease_count=1 if replica is holder else 0,
                    max_leases=16,
                    memory_used_ratio=0.1,
                    reported_at=now,
                    leases=(key,) if replica is holder else (),
                )
            ),
            ex=120,
        )
    await _write_lease(redis, key, holder)


async def _write_lease(redis, key: str, holder: Replica) -> None:
    await redis.set(
        lease_redis_key(key),
        json.dumps(
            {
                "token": "verify-token",
                "runtime_id": f"verify-{holder.name}",
                "runtime_addr": holder.addr,
                "acquired_at": time.time(),
            }
        ),
        ex=120,
    )


async def _cleanup_redis(redis) -> None:
    async for redis_key in redis.scan_iter(match=f"*{CELL_ID}*"):
        await redis.delete(redis_key)


def _client(service: Replica) -> RuntimeClient:
    """The production client, with replica A as the "Service address".

    Fresh router and fleet per phase: their 10s caches would otherwise carry a
    decision from one phase into the next, which is a property of the test, not
    of the code.
    """
    return RuntimeClient(
        base_url=service.url,
        router=LeaseRouter(fallback_url=service.url),
        fleet=RuntimeFleet(),
    )


async def main() -> int:
    if not await redis_available():
        print("fatal: Redis is not reachable; start the dev stack first", file=sys.stderr)
        return 2
    redis = get_redis()

    tmp = Path(tempfile.mkdtemp(prefix="verify-control-routing-"))
    replica_a = Replica("a", tmp / "a")
    replica_b = Replica("b", tmp / "b")
    replicas = [replica_a, replica_b]
    key = lease_key(TENANT, WORKSPACE, USER, ENV)
    copy = RuntimeTarget(
        tenant_id=TENANT, workspace_id=WORKSPACE, user_id=USER, environment=ENV
    )
    print(f"cell: {CELL_ID}\nlease key: {key}\nscratch: {tmp}\n")

    try:
        for replica in replicas:
            replica.start()
        for replica in replicas:
            if not replica.wait_ready():
                print(f"fatal: replica {replica.name} did not start; see {replica.log}",
                      file=sys.stderr)
                return 2
        print(f"replica a (Service address): {replica_a.url}")
        print(f"replica b (lease holder):    {replica_b.url}\n")

        await _publish_fleet(redis, replicas, key, holder=replica_b)

        # ── 1. creation lands on the holder ─────────────────────────────
        client = _client(replica_a)
        binder = GitBinder(client=client)
        await binder.ensure_repository(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            user_id=USER,
            environment=ENV,
            bootstrap_branch="main",
        )
        materialized = await binder.ensure_worktree(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            user_id=USER,
            environment=ENV,
            branch="main",
        )
        await client.post(
            "/internal/env-control/workspace/scaffold",
            {
                "workspace_path": materialized.workspace_path,
                "name": "routing",
                "dialect": "duckdb",
                "workspace_id": WORKSPACE,
            },
            target=copy,
        )
        check(
            "creation (repository, worktree, scaffold) lands on the lease holder",
            (replica_b.worktree() / "sql-warehouse").is_dir(),
            f"b worktree: {replica_b.worktree()}",
        )
        check(
            "the Service address replica holds nothing for the workspace",
            not replica_a.workspace_dir().exists(),
            f"a has: {replica_a.workspace_dir()}",
        )

        # Uncommitted work on the copy, written on B's disk.
        (replica_b.worktree() / "sql-warehouse" / "unsaved.sql").write_text("select 1")

        # ── 2. the old behaviour, for contrast ──────────────────────────
        raw = httpx.get(
            f"{replica_a.url}/internal/env-control/git/pending-work",
            params={
                "tenant_id": TENANT, "workspace_id": WORKSPACE,
                "user_id": USER, "environment": ENV, "branch": "main",
            },
            headers={"X-Internal-Token": TOKEN},
        ).json()
        check(
            "asking the Service address reports zero (the defect, reproduced)",
            raw.get("uncommitted") == 0,
            f"raw={raw}",
        )

        # ── 3. routed probe reports the truth ───────────────────────────
        work = await binder.pending_work(
            tenant_id=TENANT, workspace_id=WORKSPACE, user_id=USER,
            environment=ENV, branch="main",
        )
        check(
            "routed pending-work reports the uncommitted file",
            work.uncommitted >= 1,
            f"work={work}",
        )

        # ── 4. lease gone, files stay: fan-out still finds them ─────────
        await redis.delete(lease_redis_key(key))
        binder = GitBinder(client=_client(replica_a))
        work = await binder.pending_work(
            tenant_id=TENANT, workspace_id=WORKSPACE, user_id=USER,
            environment=ENV, branch="main",
        )
        check(
            "without a lease, pending work is still found on the replica holding it",
            work.uncommitted >= 1,
            f"work={work}",
        )

        # ── 5. unions and removals reach B ──────────────────────────────
        members = await binder.list_member_user_ids(
            tenant_id=TENANT, workspace_id=WORKSPACE
        )
        check("member union includes the copy on replica b", members == [USER],
              f"members={members}")
        removed = await binder.remove_worktree(
            tenant_id=TENANT, workspace_id=WORKSPACE, user_id=USER, environment=ENV
        )
        check(
            "remove-worktree fans out and removes the copy on replica b",
            removed and not replica_b.worktree().exists(),
            f"removed={removed} exists={replica_b.worktree().exists()}",
        )
    finally:
        for replica in replicas:
            replica.stop()
        await _cleanup_redis(redis)
        if failed == 0:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\nkept {tmp} for inspection (runtime logs inside)")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
