#!/usr/bin/env python3
"""End-to-end check of the shell execution plane (design 04 §3.6, stages A + C).

Runs the REAL pieces against a REAL working copy:

* the actual ``/rt/v1/tools/shell`` route, so the ROUTING decision under test is
  the one the deployment makes;
* the actual ``LocalProcessBackend``, so ordinary commands go through a real
  hardened child process — env-cleared, rlimited, cwd-pinned;
* the actual generated guards on that child's real PATH.

What is stubbed: nothing in the sandbox path. On the governance path the SQLMesh
services are replaced by recorders, because reaching them is the assertion and
exercising them needs a warehouse.

Usage:
    python scripts/verify/verify-shell-planes.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "workspace-runtime"
RUNTIME_PYTHON = RUNTIME_ROOT / ".venv" / "bin" / "python"


def _reexec_into_runtime_venv() -> None:
    """Re-exec under workspace-runtime's interpreter.

    Selected by PATH, not by probing for an importable package: the host's
    python3 may well have fastapi and lack sqlalchemy, which would make a probe
    report success and then fail deep inside the app.
    """
    if Path(sys.executable).resolve() == RUNTIME_PYTHON.resolve():
        return
    if not RUNTIME_PYTHON.is_file():
        print(f"FATAL: {RUNTIME_PYTHON} not found; create the venv first")
        raise SystemExit(2)
    os.execv(str(RUNTIME_PYTHON), [str(RUNTIME_PYTHON), str(Path(__file__).resolve())])


_reexec_into_runtime_venv()

sys.path.insert(0, str(RUNTIME_ROOT))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}{(': ' + detail) if detail else ''}")


def _working_copy(base: Path) -> Path:
    """Build the 5-segment layout the path validator requires."""
    copy = base / "t1" / "ws-t1-shop" / "u1" / "envs" / "dev"
    (copy / "sql-warehouse" / "models").mkdir(parents=True)
    (copy / "sql-warehouse" / "config.yaml").write_text(
        "gateways:\n  local:\n    connection:\n      type: duckdb\n", encoding="utf-8"
    )
    (copy / "sql-warehouse" / "models" / "orders.sql").write_text(
        "MODEL (name db.orders);\nSELECT 1 AS id\n", encoding="utf-8"
    )
    (copy / "README.md").write_text("marker-string-for-grep\n", encoding="utf-8")
    return copy


def main() -> int:
    workspaces = Path(tempfile.mkdtemp(prefix="verify-shell-"))
    copy = _working_copy(workspaces)

    # Configure BEFORE importing the app: settings are read at import time.
    os.environ["WORKSPACE_RUNTIME_WORKSPACE_BASE_PATH"] = str(workspaces)
    os.environ["WORKSPACE_RUNTIME_MATERIALIZE_ROOT"] = str(workspaces)
    os.environ["WORKSPACE_RUNTIME_TASK_BACKEND"] = "local"
    os.environ["WORKSPACE_RUNTIME_SANDBOX_TOOL_ROOT"] = str(workspaces / ".platform")
    os.environ["WORKSPACE_RUNTIME_LEASE_ENABLED"] = "false"
    # The URL builder short-circuits on this and would try a real database.
    os.environ.pop("DATABASE_URL", None)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.rest import tools as tools_api
    from src.config import settings
    from src.isolation.policy import available_levels, runtime_isolation_policy

    print("\n== Stage A: the execution plane is open ==")
    check(
        "task.backend resolves from YAML/env (not the built-in 'disabled')",
        settings.task_backend == "local",
        f"got {settings.task_backend!r}",
    )
    levels = {level.value for level in available_levels()}
    check("process isolation is available", "process" in levels, str(sorted(levels)))
    policy = runtime_isolation_policy(refresh=True)
    check(
        "a fail-safe sandbox profile exists",
        policy.default_profile is not None,
        "no profile -> every sandboxed task is refused",
    )
    profile = policy.profile_for("agent.shell")
    check(
        "agent.shell resolves to a process profile",
        profile.level.value == "process",
        f"{profile.name} / {profile.level.value}",
    )

    app = FastAPI()
    app.include_router(tools_api.router)
    client = TestClient(app)

    def shell(command: str, **extra):
        return client.post(
            "/rt/v1/tools/shell",
            json={
                "workspace_path": str(copy),
                "command": command,
                "timeout_s": 60,
                **extra,
            },
        )

    print("\n== Sandbox plane: ordinary commands ==")
    response = shell("ls -1")
    body = response.json() if response.status_code == 200 else {}
    check(
        "`ls` runs (was 503 SANDBOX_UNAVAILABLE before stage A)",
        response.status_code == 200 and body.get("exit_code") == 0,
        f"status={response.status_code} body={body}",
    )
    check(
        "cwd is the working copy",
        "sql-warehouse" in body.get("stdout", ""),
        body.get("stdout", ""),
    )

    body = shell("grep -r marker-string-for-grep .").json()
    check(
        "`grep` sees the copy's content",
        body.get("exit_code") == 0 and "README.md" in body.get("stdout", ""),
        str(body),
    )

    body = shell("git --version").json()
    check("`git` is on the sandbox PATH", body.get("exit_code") == 0, str(body))

    body = shell("env").json()
    env_text = body.get("stdout", "")
    check(
        "no platform address reaches the sandbox",
        "DATACODE_WORKSPACE_RUNTIME_URL" not in env_text,
        "a sandbox with a route into the platform breaks T3/T6",
    )
    from src.isolation.hardening import platform_secret_names

    leaked = [name for name in platform_secret_names() if f"\n{name}=" in "\n" + env_text]
    check(
        "no platform secret reaches the sandbox",
        not leaked,
        f"leaked: {leaked}",
    )
    body = shell("echo $PATH").json()
    sandbox_path = body.get("stdout", "").strip()
    check(
        "the guard directory is FIRST on PATH",
        sandbox_path.split(":")[0].endswith("command-guards"),
        f"appended guards lose to a real sqlmesh in /usr/local/bin; PATH={sandbox_path}",
    )

    print("\n== Sandbox plane: the governance guard ==")
    body = shell("sqlmesh apply prod && echo SHOULD-NOT-RUN").json()
    check(
        "a compound command with a governed verb is refused in the sandbox",
        body.get("exit_code") != 0 and "SHOULD-NOT-RUN" not in body.get("stdout", ""),
        str(body),
    )
    check(
        "the refusal explains why",
        "[datacode]" in body.get("stderr", ""),
        body.get("stderr", ""),
    )

    body = shell("sqlmesh --version").json()
    check(
        "non-governed sqlmesh verbs reach the real binary",
        body.get("exit_code") == 0 and body.get("stdout", "").strip(),
        str(body),
    )

    print("\n== Governance plane: routing ==")
    calls: dict[str, list] = {"preview": [], "apply": []}

    class _Recorder:
        async def preview(self, **kwargs):
            calls["preview"].append(kwargs)
            return {"has_changes": False}

        async def apply(self, **kwargs):
            calls["apply"].append(kwargs)
            return {
                "applied": True,
                "changes": {"added": [], "removed": [], "modified": {}},
                "backfills": [],
            }

    import src.api.deps as deps

    recorder = _Recorder()
    deps.get_plan_service = lambda: recorder  # type: ignore[assignment]

    body = shell("sqlmesh plan dev").json()
    check(
        "`sqlmesh plan` reaches the plan service, not the sandbox",
        len(calls["preview"]) == 1 and body.get("stdout") == "No changes to apply.",
        str(body),
    )

    body = shell("sqlmesh apply prod", user_id="u-actor", session_id="s-9").json()
    check(
        "`sqlmesh apply` reaches the plan service",
        len(calls["apply"]) == 1,
        str(body),
    )
    if calls["apply"]:
        applied = calls["apply"][0]
        check(
            "the environment comes from the working copy, not the argument",
            applied.get("environment") == "dev",
            f"got {applied.get('environment')!r}",
        )
        check(
            "the disagreement is reported to the caller",
            "prod" in body.get("stderr", ""),
            body.get("stderr", ""),
        )
        check(
            "attribution travels from the request",
            applied.get("user_id") == "u-actor"
            and applied.get("session_id") == "s-9",
            str(applied),
        )
        check(
            "apply is auto-applied",
            (applied.get("plan_options") or {}).get("auto_apply") is True,
            str(applied.get("plan_options")),
        )

    body = shell("sqlmesh run").json()
    check(
        "`sqlmesh run` is refused by the router",
        body.get("exit_code") != 0 and "disabled" in body.get("stderr", ""),
        str(body),
    )

    body = shell("sqlmesh plan dev --auto-apply").json()
    check(
        "`plan --auto-apply` is refused",
        body.get("exit_code") != 0 and len(calls["preview"]) == 1,
        str(body),
    )

    print("\n== Result shape ==")
    sandbox_keys = set(shell("true").json())
    governed_keys = set(shell("sqlmesh plan dev").json())
    check(
        "both planes return the identical shape",
        sandbox_keys == governed_keys == {"exit_code", "stdout", "stderr", "timed_out"},
        f"{sorted(sandbox_keys)} vs {sorted(governed_keys)}",
    )

    subprocess.run(["rm", "-rf", str(workspaces)], check=False)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
