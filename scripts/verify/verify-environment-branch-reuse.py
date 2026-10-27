#!/usr/bin/env python3
"""Environment destroy/recreate acceptance: the remote branch is never lost.

Destroying an environment used to run ``git push origin --delete env/<name>``,
so the team's branch — often the only durable copy of that work — disappeared
with a resource the platform merely derived from it. This script proves the new
contract end to end, through the REAL env-control router (the same routes
control-service drives) against a REAL local git remote:

1. the capability is gone — no ``push --delete`` anywhere in the repository, no
   endpoint and no binder method that could ask for one (a promise is only
   worth what can be verified about it);
2. destroy keeps the remote — after the worktree and the local branch are
   released, the remote branch AND its commits are still there, byte for byte;
3. recreating the same name RESUMES it — the new working copy holds the user's
   own code, not a fresh copy of the base, and reports ``reused_remote``;
4. unpushed commits are reported, not refused — ``pending-work`` counts them
   before the user confirms, which is the whole reason the deletion stays
   available;
5. a stale local branch cannot shadow the remote — an interrupted destroy
   leaves one behind, and git prefers it, so it is fast-forwarded;
6. unpushed commits on that stale branch are preserved instead, because they
   exist nowhere else.

Test seam: exactly one. A local bare repository stands in for the hosted
remote, which the SSRF URL policy rejects BY DESIGN, so only the credential
resolver is stubbed (the throwaway run has no stored credential). Every git
operation — ls-remote, fetch, worktree add, push, rev-list — is real.

Usage:
    python3 scripts/verify/verify-environment-branch-reuse.py
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
# environment must be settled BEFORE the first `src.*` import: the internal
# token is read at settings construction time.
sys.path.insert(0, RUNTIME_DIR)
os.environ["WORKSPACE_RUNTIME_INTERNAL_TOKEN"] = SMOKE_TOKEN
os.environ.pop("DATABASE_URL", None)

import httpx
import src.workspace.git_credentials as _creds
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from src.api.rest import env_control
from src.config import settings
from src.utils.errors import ServiceError


async def _smoke_resolve_auth(user_id: str, repo_url: str):
    """The only seam: the throwaway run stores no Git credential.

    The provider is unprobeable on purpose, so write-access checks report
    "not performed" and real ``git`` decides everything against the local bare
    repository.
    """
    from src.git.ops import GitAuth

    return _creds.ResolvedGitAuth(
        GitAuth(token="smoke-token", provider="smoke", username="smoke"), "", ""
    )


_creds.resolve_auth_for_url = _smoke_resolve_auth

PASS: list[str] = []
FAIL: list[str] = []

T, W, U = "t1", "ws-t1-shop", "u1"
ENV = "dev"
BRANCH = "env/dev"


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(
        f"{'PASS' if cond else 'FAIL'}  {name}"
        + (f"  -- {extra}" if extra and not cond else "")
    )


def git(cwd, *args, check_rc: bool = True):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check_rc,
    )


def commit(path, filename: str, content: str, message: str) -> str:
    (Path(path) / filename).write_text(content)
    git(path, "add", filename)
    git(
        path,
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=a",
        "commit",
        "-m",
        message,
    )
    return git(path, "rev-parse", "HEAD").stdout.strip()


def make_remote(path: Path) -> str:
    """A local bare repository seeded with a base commit on ``main``."""
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    seed = path.parent / (path.name + "-seed")
    seed.mkdir()
    git(seed, "init", "-b", "main")
    commit(seed, "base.sql", "select 1", "base")
    git(seed, "remote", "add", "origin", str(path))
    git(seed, "push", "origin", "main")
    return str(path)


def remote_branch_head(remote: str, branch: str) -> str:
    """Commit the remote's branch points at ("" when it does not exist)."""
    result = git(remote, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                 check_rc=False)
    return result.stdout.strip()


def grep_repo(pattern: str) -> str:
    """Search the whole repository for a pattern (empty output = absent)."""
    result = subprocess.run(
        ["grep", "-rn", "--include=*.py", "--include=*.go", "--include=*.ts",
         "--include=*.vue", pattern, REPO_ROOT],
        capture_output=True,
        text=True,
        check=False,
    )
    # The verify script itself and the comments that explain the removal are
    # expected matches; only executable uses elsewhere would be a regression.
    lines = [
        line
        for line in result.stdout.splitlines()
        if "verify-environment-branch-reuse" not in line
        and "/tests/" not in line
        and not line.split(":", 2)[-1].lstrip().startswith("#")
    ]
    return "\n".join(lines)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="verify-env-branch-"))
    settings.workspace_base_path = str(tmp / "ws")
    (tmp / "ws").mkdir()
    remote = make_remote(tmp / "remote.git")

    app = FastAPI()
    app.include_router(env_control.router)

    @app.exception_handler(ServiceError)
    async def _service_error(_request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response_dict(),
            headers=exc.headers,
        )

    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime",
        headers={"X-Internal-Token": SMOKE_TOKEN},
    )

    async def post(path, payload):
        response = await http.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    async def get(path, params):
        response = await http.get(path, params=params)
        response.raise_for_status()
        return response.json()

    ids = {"tenant_id": T, "workspace_id": W, "user_id": U}

    async def materialize() -> dict:
        """The production create path: ensure the clone, then the worktree."""
        await post(
            "/internal/env-control/git/ensure-repository",
            {**ids, "git_repo_url": remote, "bootstrap_branch": "main"},
        )
        return await post(
            "/internal/env-control/git/ensure-worktree",
            {**ids, "environment": ENV, "branch": BRANCH, "start_branch": "main"},
        )

    # --- 1. The capability is gone ------------------------------------------
    check(
        "no push --delete anywhere in the codebase",
        grep_repo("push.*--delete") == "",
        grep_repo("push.*--delete"),
    )
    check(
        "no delete-remote-branch endpoint",
        grep_repo("delete-remote-branch") == "",
        grep_repo("delete-remote-branch"),
    )
    check(
        "no delete_remote_branch caller",
        grep_repo("delete_remote_branch") == "",
        grep_repo("delete_remote_branch"),
    )
    routes = {route.path for route in env_control.router.routes}
    check(
        "router exposes no remote-branch deletion",
        not any("delete-remote" in path for path in routes),
    )

    # --- 2. First life: create, commit, publish -----------------------------
    first = await materialize()
    check(
        "new environment branches from its base",
        first["branch_source"] == "created_from_base",
        first["branch_source"],
    )
    copy = Path(first["workspace_path"])
    check("base code is present", (copy / "base.sql").exists())

    published = commit(copy, "user_model.sql", "select 'mine'", "user work")
    git(copy, "push", "-u", "origin", BRANCH)
    check(
        "branch reached the remote",
        remote_branch_head(remote, BRANCH) == published,
        remote_branch_head(remote, BRANCH),
    )

    # --- 3. Unpushed commits are reported before the user decides -----------
    local_only = commit(copy, "wip.sql", "select 2", "not pushed yet")
    pending = await get(
        "/internal/env-control/git/pending-work",
        {**ids, "environment": ENV, "branch": BRANCH},
    )
    check(
        "pending-work counts the unpushed commit",
        pending["unpushed"] == 1,
        str(pending),
    )
    check("pending-work sees no uncommitted change", pending["uncommitted"] == 0)

    (copy / "draft.sql").write_text("select 3")
    pending = await get(
        "/internal/env-control/git/pending-work",
        {**ids, "environment": ENV, "branch": BRANCH},
    )
    check(
        "pending-work counts the uncommitted change",
        pending["uncommitted"] == 1,
        str(pending),
    )
    (copy / "draft.sql").unlink()

    # --- 4. Destroy releases local state only -------------------------------
    # What control-service's destroy drives for each member. The remote is not
    # addressed at all — there is no endpoint left that could.
    removed = await post("/internal/env-control/git/remove-worktree",
                         {**ids, "environment": ENV})
    check("worktree was removed", removed.get("removed") is True, str(removed))
    check("working copy is gone from disk", not copy.exists())
    check(
        "remote branch survived the destroy",
        remote_branch_head(remote, BRANCH) == published,
        remote_branch_head(remote, BRANCH),
    )

    # --- 5. A stale local branch must not shadow the remote -----------------
    # The local branch still holds the never-pushed commit, so it is KEPT: that
    # commit exists nowhere else.
    second = await materialize()
    check(
        "stale local branch with unpushed work is adopted",
        second["branch_source"] == "adopted_local",
        second["branch_source"],
    )
    check(
        "the unpushed commit is still checked out",
        second["commit"] == local_only,
        second["commit"],
    )
    check(
        "its file survived",
        (Path(second["workspace_path"]) / "wip.sql").exists(),
    )

    # Publish it, then drop the worktree again: now the local branch is fully
    # published and may safely follow the remote.
    git(second["workspace_path"], "push", "origin", BRANCH)
    await post("/internal/env-control/git/remove-worktree", {**ids, "environment": ENV})

    # A teammate advances the remote while this member holds nothing.
    teammate = tmp / "teammate"
    git(tmp, "clone", "-b", BRANCH, remote, str(teammate))
    advanced = commit(teammate, "teammate.sql", "select 4", "teammate work")
    git(teammate, "push", "origin", BRANCH)

    third = await materialize()
    check(
        "published local branch is fast-forwarded to the remote",
        third["branch_source"] == "reused_remote",
        third["branch_source"],
    )
    check(
        "the teammate's commit is checked out",
        third["commit"] == advanced,
        third["commit"],
    )

    # --- 6. A clean recreation resumes the remote branch --------------------
    # The state a destroy leaves for the NEXT member (or after a local prune):
    # no local branch, only the preserved remote one.
    await post("/internal/env-control/git/remove-worktree", {**ids, "environment": ENV})
    await post("/internal/env-control/git/delete-branch",
               {**ids, "branch": BRANCH})
    fourth = await materialize()
    check(
        "recreated environment resumes the remote branch",
        fourth["branch_source"] == "reused_remote",
        fourth["branch_source"],
    )
    recreated = Path(fourth["workspace_path"])
    check(
        "it carries the user's code, not a fresh base",
        (recreated / "user_model.sql").read_text() == "select 'mine'",
    )
    check("and the teammate's later work", (recreated / "teammate.sql").exists())
    upstream = git(
        recreated, "rev-parse", "--abbrev-ref", f"{BRANCH}@{{upstream}}",
        check_rc=False,
    )
    check(
        "the branch tracks its remote",
        upstream.stdout.strip() == f"origin/{BRANCH}",
        upstream.stdout.strip(),
    )

    await http.aclose()

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed checks: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
