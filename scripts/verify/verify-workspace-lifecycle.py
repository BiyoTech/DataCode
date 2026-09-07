#!/usr/bin/env python3
"""Workspace lifecycle acceptance: error passthrough, create-or-reuse, purge.

The scenario that motivated the rework: a user binds a workspace to a Git URL
whose repository does not exist, and the failure surfaced as the generic
"Git operation failed: add worktree for environment prod" — the remote's own
words ("Repository not found") had been discarded one layer above where they
were still known. This script replays that exact request, then the two
behaviors that came with the fix, end to end through the REAL env-control
router (the same routes control-service drives) over REAL local git remotes:

1. error passthrough — a missing remote answers 422 with
   ``git_error_reason=repo_not_found`` and the remote's wording in
   ``git_detail``, and the message carries no stacked prefixes;
2. create-or-reuse — ``provision-remote`` classifies an existing repository
   with branches as ``reusable`` (``needs_scaffold=False``: the user's code is
   cloned, no platform template is written) and a branchless one as
   ``remote_empty`` (still no scaffold; one EMPTY root commit so a worktree
   can attach at all);
3. hard purge — the workspace's directories AND the rows of the three tables
   this service owns disappear, a prefix neighbour (``ws-x`` vs
   ``ws-x-archive``) survives on disk and in the database, the remote
   repository is untouched, and a second purge is a clean no-op.

Test seams (as narrow as possible, and only in this process): local bare
repositories stand in for the hosted remote, which the SSRF URL policy
rejects BY DESIGN — so only the scaffold module's validator reference is
swapped, and the credential resolver is stubbed to a fake token with an
unprobeable provider, leaving REAL ``git ls-remote`` / fetch / worktree
operations to decide everything. The row deletion drives a real throwaway
SQLite database seeded through the production models; the SQLMesh state and
Dagster deployment steps are covered by their own unit tests and degrade to
best-effort warnings here.

Usage:
    python3 scripts/verify/verify-workspace-lifecycle.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME_DIR = os.path.join(REPO_ROOT, "workspace-runtime")
RUNTIME_VENV_PYTHON = os.path.join(RUNTIME_DIR, ".venv", "bin", "python")

SMOKE_TOKEN = "verify-internal-token"


def _ensure_runtime_venv() -> None:
    """Run inside the workspace-runtime venv (``src.*`` needs its deps).

    Decided by interpreter path, not by probing packages: a host Python that
    happens to carry some of the dependencies would otherwise pass the probe
    and die later on the first missing one.
    """
    if os.path.abspath(sys.executable) == RUNTIME_VENV_PYTHON:
        return
    if not os.path.exists(RUNTIME_VENV_PYTHON):
        print(
            f"fatal: {RUNTIME_VENV_PYTHON} not found — run scripts/dev-setup.sh first",
            file=sys.stderr,
        )
        sys.exit(2)
    os.execv(
        RUNTIME_VENV_PYTHON,
        [RUNTIME_VENV_PYTHON, os.path.abspath(__file__)] + sys.argv[1:],
    )


_ensure_runtime_venv()

# The runtime service is importable as `src.*` from its own directory, and the
# environment below must be settled BEFORE the first `src.*` import: the token
# is read at settings construction, the database type lazily per access.
sys.path.insert(0, RUNTIME_DIR)
# The verify run owns its process: pin the internal token (an ambient dev
# token would make every request a 401) and keep DATABASE_URL from hijacking
# the throwaway SQLite database (it short-circuits every URL the builder makes).
os.environ["WORKSPACE_RUNTIME_INTERNAL_TOKEN"] = SMOKE_TOKEN
os.environ.pop("DATABASE_URL", None)

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from src.api.rest import env_control  # noqa: E402
from src.config import settings  # noqa: E402
from src.utils.errors import ServiceError  # noqa: E402

# --- Test seams: local bare repositories stand in for the remote ------------
#
# Two platform gates reject local paths BY DESIGN, so the verify script
# replaces them in the narrowest scope possible (this process only, the exact
# modules below; the gates themselves stay intact and are covered by their own
# unit tests):
#
# 1. The SSRF URL policy: a workspace remote must be a public https/ssh URL.
#    Only the scaffold module's reference is swapped, so provision-remote can
#    classify local paths. Everything downstream is the real code.
# 2. The credential resolver: the throwaway database has no credential. It is
#    stubbed to a fake token with an UNPROBEABLE provider, so
#    check_remote_write_access reports performed=False and REAL
#    `git ls-remote` decides existence against the local bare repos.
import src.workspace.git_credentials as _creds  # noqa: E402
import src.workspace.scaffold as _scaffold  # noqa: E402


async def _smoke_validate_url(url: str) -> str:
    return url


async def _smoke_resolve_auth(user_id: str, repo_url: str):
    from src.git.ops import GitAuth

    return _creds.ResolvedGitAuth(
        GitAuth(token="smoke-token", provider="smoke", username="smoke"), "", ""
    )


_scaffold.validate_git_repo_url_async = _smoke_validate_url
_creds.resolve_auth_for_url = _smoke_resolve_auth

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {extra}" if extra and not cond else ""))


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def make_remote(path: Path, with_code: bool) -> str:
    """A local bare repository, optionally carrying one user commit."""
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    if with_code:
        work = path.parent / (path.name + "-w")
        work.mkdir()
        git(work, "init", "-b", "main")
        (work / "user_code.sql").write_text("select 1")
        git(work, "add", ".")
        git(work, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-m", "user code")
        git(work, "remote", "add", "origin", str(path))
        git(work, "push", "origin", "main")
    return str(path)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="verify-workspace-"))
    settings.workspace_base_path = str(tmp / "ws")
    (tmp / "ws").mkdir()

    # A throwaway SQLite database for the three tables this service owns, so
    # the purge's row deletion runs for real (seeded through the production
    # models) instead of degrading to a best-effort warning.
    os.environ["WORKSPACE_RUNTIME_DB_TYPE"] = "sqlite"
    os.environ["WORKSPACE_RUNTIME_DB_SQLITE_PATH"] = str(tmp / "runtime.db")
    from src.workspace.database import get_session, init_db, reset_engine

    reset_engine()
    await init_db()
    from src.workspace.models import (
        FileDraftModel,
        KnowledgeDocumentModel,
        PlanApplyHistoryModel,
    )

    app = FastAPI()
    app.include_router(env_control.router)

    @app.exception_handler(ServiceError)
    async def _service_error(_request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response_dict(),
            headers=exc.headers,
        )

    # Raw HTTP against the real router: this body is exactly what
    # control-service's RuntimeClient parses (its translation is pinned by its
    # own unit tests).
    transport = httpx.ASGITransport(app=app)

    class Client:
        def __init__(self):
            self._c = httpx.AsyncClient(
                transport=transport,
                base_url="http://runtime",
                headers={"X-Internal-Token": SMOKE_TOKEN},
            )

        async def post(self, path, payload):
            r = await self._c.post(path, json=payload)
            if r.status_code >= 400:
                raise CtlError(r.status_code, r.json())
            return r.json()

    class CtlError(Exception):
        def __init__(self, status, body):
            self.status_code, self._body = status, body
            super().__init__(body.get("message", ""))

        def to_response_dict(self):
            return self._body

    client = Client()

    T, W, U = "t1", "ws-t1-shop", "u1"

    # --- 1. The reported failure: a remote that does not exist --------------
    try:
        await client.post(
            "/internal/env-control/git/ensure-repository",
            {
                "tenant_id": T,
                "workspace_id": W,
                "user_id": U,
                "git_repo_url": str(tmp / "does-not-exist.git"),
                "bootstrap_branch": "main",
            },
        )
        check("missing remote is rejected", False, "no error raised")
    except CtlError as exc:
        body = exc.to_response_dict()
        check("missing remote -> 422", exc.status_code == 422, str(exc.status_code))
        check(
            "reason is repo_not_found",
            body.get("git_error_reason") == "repo_not_found",
            str(body.get("git_error_reason")),
        )
        detail = body.get("git_detail", "").lower()
        check(
            "remote wording is carried",
            "does not appear to be a git repository" in detail or "not found" in detail,
            body.get("git_detail", ""),
        )
        check(
            "message is not double-prefixed",
            body.get("message", "").count("Git operation failed:") <= 1,
            body.get("message", ""),
        )

    # --- 2. Reuse: existing code must survive, no template ------------------
    remote = make_remote(tmp / "with-code.git", with_code=True)
    body = await client.post(
        "/internal/env-control/workspace/provision-remote",
        {"git_repo_url": remote, "user_id": U, "private": True},
    )
    check("existing remote -> reusable", body["state"] == "reusable", body["state"])
    check("reuse asks for NO scaffold", body["needs_scaffold"] is False)
    await client.post(
        "/internal/env-control/git/ensure-repository",
        {
            "tenant_id": T,
            "workspace_id": W,
            "user_id": U,
            "git_repo_url": remote,
            "bootstrap_branch": "main",
            "allow_bootstrap_commit": body["needs_bootstrap_commit"],
        },
    )
    wt = await client.post(
        "/internal/env-control/git/ensure-worktree",
        {
            "tenant_id": T,
            "workspace_id": W,
            "user_id": U,
            "environment": "prod",
            "branch": "main",
        },
    )
    copy = Path(wt["workspace_path"])
    check("user code was cloned", (copy / "user_code.sql").is_file())
    check("no platform template written", not (copy / "sql-warehouse").exists())

    # --- 3. Empty remote: worktree, still no template -----------------------
    empty = make_remote(tmp / "empty.git", with_code=False)
    body2 = await client.post(
        "/internal/env-control/workspace/provision-remote",
        {"git_repo_url": empty, "user_id": U, "private": True},
    )
    check("empty remote -> remote_empty", body2["state"] == "remote_empty", body2["state"])
    check("empty remote asks for NO scaffold", body2["needs_scaffold"] is False)
    check("empty remote needs a root commit", body2["needs_bootstrap_commit"] is True)
    W2 = "ws-t1-empty"
    await client.post(
        "/internal/env-control/git/ensure-repository",
        {
            "tenant_id": T,
            "workspace_id": W2,
            "user_id": U,
            "git_repo_url": empty,
            "bootstrap_branch": "main",
            "allow_bootstrap_commit": True,
        },
    )
    wt2 = await client.post(
        "/internal/env-control/git/ensure-worktree",
        {
            "tenant_id": T,
            "workspace_id": W2,
            "user_id": U,
            "environment": "prod",
            "branch": "main",
        },
    )
    copy2 = Path(wt2["workspace_path"])
    check("empty remote yields a usable copy", copy2.is_dir())
    check(
        "empty copy has no files",
        [p.name for p in copy2.iterdir() if p.name != ".git"] == [],
    )

    # --- 4. Hard purge: nothing left, neighbours and remote intact ----------
    # Seed the three owned tables for the workspace being purged AND for a
    # prefix neighbour: without the trailing-slash guard the neighbour's rows
    # would be deleted too — silent cross-workspace data loss.
    neighbour = Path(tmp / "ws" / T / "ws-t1-shop-archive" / U / "envs" / "prod")
    neighbour.mkdir(parents=True)
    (neighbour / "marker.txt").write_text("x")

    async def seed_rows(workspace_path: str) -> None:
        async with get_session() as session:
            session.add(
                KnowledgeDocumentModel(
                    id=f"kd-{workspace_path}",
                    workspace_path=workspace_path,
                    title="doc",
                    filename="doc.md",
                    content_type="markdown",
                    file_size=1,
                )
            )
            session.add(
                FileDraftModel(
                    id=f"fd-{workspace_path}",
                    workspace_path=workspace_path,
                    file_path="models/a.sql",
                    content="select 1",
                )
            )
            session.add(
                PlanApplyHistoryModel(
                    id=f"pah-{workspace_path}",
                    workspace_path=workspace_path,
                    environment="prod",
                    applied=1,
                )
            )

    await seed_rows(str(copy))
    await seed_rows(str(neighbour))

    purge = await client.post(
        "/internal/env-control/workspace/hard-purge", {"tenant_id": T, "workspace_id": W}
    )
    check("purge reports success", purge["purged"] is True)
    check(
        "purge deleted the owned rows",
        purge["rows_deleted"]
        == {"knowledge_documents": 1, "file_drafts": 1, "plan_apply_history": 1},
        str(purge["rows_deleted"]),
    )
    check("workspace root is gone", not (tmp / "ws" / T / W).exists())
    check("the OTHER workspace survives", (tmp / "ws" / T / W2).is_dir())
    check("prefix neighbour directory survives", neighbour.is_dir())
    async with get_session() as session:
        result = await session.execute(
            select(func.count()).select_from(FileDraftModel).where(
                FileDraftModel.workspace_path == str(neighbour)
            )
        )
        check(
            "prefix neighbour rows survive",
            int(result.scalar_one() or 0) == 1,
            "neighbour rows were deleted",
        )
    check("remote repository untouched", Path(remote).is_dir())
    check(
        "remote still has its branch",
        "main"
        in subprocess.run(
            ["git", "ls-remote", "--heads", remote], capture_output=True, text=True
        ).stdout,
    )
    again = await client.post(
        "/internal/env-control/workspace/hard-purge", {"tenant_id": T, "workspace_id": W}
    )
    check("purge is idempotent", again["purged"] is True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
