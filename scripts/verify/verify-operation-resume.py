#!/usr/bin/env python3
"""Acceptance for interrupted lifecycle operations (review P1: workspace_operations).

Before the operations record, the only compensation for a half-built create or
a half-done delete was in-process — a control replica killed mid-way (deploy,
OOM, node loss) left a workspace row without a copy, or files without a row,
with nothing that would ever finish the job.

This script kills a REAL control process with SIGKILL at the worst moments and
then runs the production resumer against what it left behind:

* a real ``workspace-runtime`` process on its own directory;
* real Redis (lease record + heartbeat in production format, namespaced by a
  throwaway cell id);
* a real control database (SQLite file) shared by the killed child and the
  resuming parent;
* the production ``WorkspaceManager`` / ``OperationRecorder`` /
  ``OperationResumer`` — the only interference is the kill itself, injected
  right after a runtime call returned, i.e. after its side effect happened and
  before the process recorded it.

Assertions:

1. create killed after the scaffold → the record is left ``running``; the
   resumer rolls it back: no workspace row, no environment rows, no owner
   directory on the replica; status ``rolled_back``;
2. delete killed after the replica purge, before the rows were deleted → the
   resumer finishes it forward: no rows, no files; status ``succeeded``;
3. a second resume pass finds nothing, and re-running the deletion on a gone
   workspace is a harmless no-op (every step converges on "absent").

Usage:
    python3 scripts/verify/verify-operation-resume.py

Needs: a reachable Redis and the control-service / workspace-runtime venvs.
Exit 0 = all passed, 1 = a failed assertion, 2 = a missing prerequisite.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
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

TOKEN = "verify-operation-resume-token"
TENANT, OWNER, ENV = "t1", "u1", "prod"

# Child mode: argv = [script, "child", <action>, <workspace name>]. Everything
# the child needs (DB, runtime URL, cell id) is inherited through the env.
CHILD = len(sys.argv) > 1 and sys.argv[1] == "child"


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

if not CHILD:
    # Settled BEFORE the first control import: settings read these at
    # construction, and the children inherit them.
    _SCRATCH = Path(tempfile.mkdtemp(prefix="verify-operation-resume-"))
    os.environ["VERIFY_SCRATCH"] = str(_SCRATCH)
    os.environ["DATACODE_CELL_ID"] = f"verify-{uuid.uuid4().hex[:8]}"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_SCRATCH / 'control.db'}"
    os.environ["CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN"] = TOKEN
    os.environ.setdefault("DATACODE_ENV", "dev")
    _RUNTIME_PORT = socket.socket()
    _RUNTIME_PORT.bind(("127.0.0.1", 0))
    os.environ["VERIFY_RUNTIME_PORT"] = str(_RUNTIME_PORT.getsockname()[1])
    _RUNTIME_PORT.close()

RUNTIME_PORT = int(os.environ["VERIFY_RUNTIME_PORT"])
RUNTIME_ADDR = f"127.0.0.1:{RUNTIME_PORT}"
RUNTIME_URL = f"http://{RUNTIME_ADDR}"
SCRATCH = Path(os.environ["VERIFY_SCRATCH"])
CELL_ID = os.environ["DATACODE_CELL_ID"]
RUNTIME_BASE = SCRATCH / "runtime" / "workspaces"
os.environ["CONTROL_SERVICE_RUNTIME_REST_URL"] = RUNTIME_URL
sys.path.insert(0, CONTROL_DIR)

import httpx  # noqa: E402
from datacode_common import naming  # noqa: E402
from datacode_common import workspace_paths as ws_paths  # noqa: E402
from datacode_common.lease import (  # noqa: E402
    RuntimeHeartbeat,
    encode_runtime_heartbeat,
    lease_redis_key,
    runtime_index_redis_key,
    runtime_redis_key,
)
from datacode_common.redis.client import get_redis, redis_available  # noqa: E402

# ── child: run one lifecycle call and die at the chosen point ─────────────


async def _child(action: str, name: str) -> None:
    from src.runtime.client import RuntimeClient, set_runtime_client
    from src.workspace import database
    from src.workspace.manager import WorkspaceManager

    client = RuntimeClient(base_url=RUNTIME_URL)
    set_runtime_client(client)
    await database.init_db()
    manager = WorkspaceManager()
    workspace_id = ws_paths.build_workspace_id(TENANT, ws_paths.slugify(name))

    def _die_after(method: str, suffix: str) -> None:
        original = getattr(client, method)

        async def wrapped(path, *args, **kwargs):
            result = await original(path, *args, **kwargs)
            if path.endswith(suffix):
                # The side effect happened; the record of it never will.
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        setattr(client, method, wrapped)

    if action == "create":
        await manager.create(TENANT, OWNER, name)
    elif action == "create-kill-after-scaffold":
        _die_after("post", "/workspace/scaffold")
        await manager.create(TENANT, OWNER, name)
    elif action == "delete-kill-after-purge":
        _die_after("post_all", "/workspace/hard-purge")
        await manager.delete(workspace_id, TENANT, OWNER)
    else:
        raise SystemExit(f"unknown child action {action!r}")
    await database.close_db()


if CHILD:
    sys.exit(asyncio.run(_child(sys.argv[2], sys.argv[3])))


# ── parent ────────────────────────────────────────────────────────────────

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


def _start_runtime() -> subprocess.Popen:
    RUNTIME_BASE.mkdir(parents=True)
    root = RUNTIME_BASE.parent
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)  # it would short-circuit the runtime's own DB
    env.update(
        {
            "DATACODE_RUNTIME_ID": "verify-resume",
            "WORKSPACE_RUNTIME_INTERNAL_TOKEN": TOKEN,
            "WORKSPACE_RUNTIME_WORKSPACE_BASE_PATH": str(RUNTIME_BASE),
            "WORKSPACE_RUNTIME_MATERIALIZE_ROOT": str(RUNTIME_BASE),
            "WORKSPACE_RUNTIME_LEASE_ENABLED": "false",
            "WORKSPACE_RUNTIME_MATERIALIZE_MOUNT_KIND": "shared",
            "WORKSPACE_RUNTIME_DAGSTER_HOME": str(root / "dagster_home"),
            "WORKSPACE_RUNTIME_DAGSTER_DAEMON_ENABLED": "false",
            "WORKSPACE_RUNTIME_DAGSTER_WEBSERVER_ENABLED": "false",
            "WORKSPACE_RUNTIME_DB_TYPE": "sqlite",
            "WORKSPACE_RUNTIME_DB_SQLITE_PATH": str(root / "runtime.db"),
            "PATH": os.path.dirname(RUNTIME_PYTHON) + os.pathsep + env.get("PATH", ""),
        }
    )
    log = open(root / "runtime.log", "w")
    return subprocess.Popen(
        [RUNTIME_PYTHON, "-m", "uvicorn", "src.main:app",
         "--host", "127.0.0.1", "--port", str(RUNTIME_PORT)],
        cwd=RUNTIME_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def _wait_ready(proc: subprocess.Popen, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            if httpx.get(f"{RUNTIME_URL}/health", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def _run_child(action: str, name: str) -> int:
    log = SCRATCH / f"child-{action}.log"
    with open(log, "w") as handle:
        proc = subprocess.run(
            [CONTROL_PYTHON, os.path.abspath(__file__), "child", action, name],
            env=dict(os.environ), stdout=handle, stderr=subprocess.STDOUT, timeout=300,
        )
    return proc.returncode


async def _publish_holder(redis, workspace_id: str) -> None:
    """The single replica holds the owner's production lease (production format)."""
    key = naming.lease_key(TENANT, workspace_id, OWNER, ENV)
    await redis.sadd(runtime_index_redis_key(), "verify-resume")
    await redis.set(
        runtime_redis_key("verify-resume"),
        encode_runtime_heartbeat(
            RuntimeHeartbeat(
                runtime_id="verify-resume",
                addr=RUNTIME_ADDR,
                lease_count=1,
                max_leases=16,
                memory_used_ratio=0.1,
                reported_at=datetime.now(timezone.utc),
                leases=(key,),
            )
        ),
        ex=300,
    )
    await redis.set(
        lease_redis_key(key),
        json.dumps(
            {
                "token": f"verify-{workspace_id}",
                "runtime_id": "verify-resume",
                "runtime_addr": RUNTIME_ADDR,
                "acquired_at": time.time(),
            }
        ),
        ex=300,
    )


async def _operations(workspace_id: str) -> list:
    from sqlalchemy import select

    from src.operations.models import WorkspaceOperationModel
    from src.workspace.database import get_session

    async with get_session() as session:
        rows = (
            await session.execute(
                select(WorkspaceOperationModel)
                .where(WorkspaceOperationModel.workspace_id == workspace_id)
                .order_by(WorkspaceOperationModel.created_at)
            )
        ).scalars().all()
    return [(row.kind, row.step, row.status) for row in rows]


async def _rows_left(workspace_id: str) -> tuple[bool, int]:
    from sqlalchemy import func, select

    from src.environments.models import EnvironmentModel
    from src.workspace.database import get_session
    from src.workspace.models import WorkspaceModel

    async with get_session() as session:
        workspace = await session.get(WorkspaceModel, workspace_id)
        envs = await session.scalar(
            select(func.count()).select_from(EnvironmentModel).where(
                EnvironmentModel.workspace_id == workspace_id
            )
        )
    return workspace is not None, int(envs or 0)


async def main() -> int:
    if not await redis_available():
        print("fatal: Redis is not reachable; start the dev stack first", file=sys.stderr)
        return 2
    redis = get_redis()

    from src.config import settings
    from src.operations.resumer import OperationResumer
    from src.runtime.client import RuntimeClient, set_runtime_client
    from src.workspace import database

    print(f"cell: {CELL_ID}\nscratch: {SCRATCH}\nruntime: {RUNTIME_URL}\n")
    runtime = _start_runtime()
    try:
        if not _wait_ready(runtime):
            print(f"fatal: runtime did not start; see {RUNTIME_BASE.parent}/runtime.log",
                  file=sys.stderr)
            return 2
        await database.init_db()
        set_runtime_client(RuntimeClient(base_url=RUNTIME_URL))
        # Treat anything not progressing right now as abandoned, and act on it.
        settings.operation_resume_grace_seconds = 0
        resumer = OperationResumer()

        # ── 1. create killed after the scaffold ─────────────────────────
        name_a = f"resume-a-{uuid.uuid4().hex[:6]}"
        ws_a = ws_paths.build_workspace_id(TENANT, ws_paths.slugify(name_a))
        await _publish_holder(redis, ws_a)
        code = _run_child("create-kill-after-scaffold", name_a)
        owner_dir = RUNTIME_BASE / TENANT / ws_a / OWNER
        check("child was killed by SIGKILL mid-create", code == -signal.SIGKILL,
              f"returncode={code}")
        ops = await _operations(ws_a)
        check(
            "the interrupted create is left running at 'opened' with the copy on disk",
            ops == [("create", "opened", "running")] and owner_dir.is_dir(),
            f"ops={ops} owner_dir={owner_dir.is_dir()}",
        )

        outcome = await resumer.run_once(repair=True)
        row_left, envs_left = await _rows_left(ws_a)
        ops = await _operations(ws_a)
        check(
            "resumer rolls the half-built create back completely",
            not row_left and envs_left == 0 and not owner_dir.exists(),
            f"row={row_left} envs={envs_left} owner_dir={owner_dir.exists()}",
        )
        check("the record is closed as rolled_back",
              ops == [("create", "opened", "rolled_back")], f"ops={ops} outcome={outcome}")

        # ── 2. delete killed after the replica purge ───────────────────
        name_b = f"resume-b-{uuid.uuid4().hex[:6]}"
        ws_b = ws_paths.build_workspace_id(TENANT, ws_paths.slugify(name_b))
        await _publish_holder(redis, ws_b)
        code = _run_child("create", name_b)
        workspace_dir = RUNTIME_BASE / TENANT / ws_b
        check("a workspace is created normally", code == 0 and workspace_dir.is_dir(),
              f"returncode={code}")

        code = _run_child("delete-kill-after-purge", name_b)
        row_left, envs_left = await _rows_left(ws_b)
        ops = await _operations(ws_b)
        check(
            "delete killed after the purge leaves rows without files",
            code == -signal.SIGKILL and row_left and not workspace_dir.exists()
            and ops[-1] == ("delete", "resources_released", "running"),
            f"returncode={code} row={row_left} files={workspace_dir.exists()} ops={ops}",
        )

        await resumer.run_once(repair=True)
        row_left, envs_left = await _rows_left(ws_b)
        ops = await _operations(ws_b)
        check(
            "resumer finishes the deletion forward",
            not row_left and envs_left == 0 and ops[-1][0] == "delete"
            and ops[-1][2] == "succeeded",
            f"row={row_left} envs={envs_left} ops={ops}",
        )

        # ── 3. convergence ──────────────────────────────────────────────
        again = await resumer.run_once(repair=True)
        check("a second pass finds nothing to resume", again.stuck == 0,
              f"stuck={again.stuck}")
        from src.workspace.manager import get_workspace_manager

        try:
            await get_workspace_manager().run_deletion(
                workspace_id=ws_b, tenant_id=TENANT, user_id=OWNER, op=None
            )
            rerun_ok = True
        except Exception as exc:  # noqa: BLE001 - reported as a failed check
            rerun_ok = False
            print(f"        rerun error: {exc}")
        check("re-running the deletion on a gone workspace is a no-op", rerun_ok)
    finally:
        runtime.terminate()
        try:
            runtime.wait(timeout=10)
        except subprocess.TimeoutExpired:
            runtime.kill()
        async for redis_key in redis.scan_iter(match=f"*{CELL_ID}*"):
            await redis.delete(redis_key)
        await database.close_db()
        if failed == 0:
            shutil.rmtree(SCRATCH, ignore_errors=True)
        else:
            print(f"\nkept {SCRATCH} for inspection (runtime + child logs inside)")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
