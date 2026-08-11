"""Git operations for workspace version control.

Provides both local Git CLI operations and remote Git provider API integration.
Supports: Gitea, GitHub, GitLab (provider-agnostic design).
"""

import os
import subprocess
import typing as t
from dataclasses import dataclass

import asyncio
import httpx

from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GitRepoInfo:
    """Remote Git repository metadata."""
    clone_url: str
    html_url: str
    name: str
    default_branch: str = "main"


class GitOperations:
    """Git operations combining local CLI and remote provider API.

    Remote operations require user-level token (from user_git_credentials).
    Local operations use subprocess git.
    """

    # ---- Local Git CLI operations ----

    @staticmethod
    def init(workspace_path: str) -> bool:
        """Initialize a local Git repository."""
        git_dir = os.path.join(workspace_path, ".git")
        if os.path.exists(git_dir):
            return True

        result = _run_git(workspace_path, ["init"])
        if result.returncode == 0:
            _run_git(workspace_path, ["add", "."])
            _run_git(workspace_path, ["commit", "-m", "Initial commit", "--allow-empty"])
            logger.info("git_initialized", workspace=workspace_path)
            return True
        return False

    @staticmethod
    def clone(repo_url: str, local_path: str, branch: str = "main", token: str = "") -> bool:
        """Clone a remote repository to local path.

        If token is provided, injects it into the URL for HTTPS auth.
        """
        # Inject token into URL for authentication
        auth_url = _inject_token(repo_url, token) if token else repo_url

        result = subprocess.run(
            ["git", "clone", "--branch", branch, auth_url, local_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("git_cloned", repo_url=repo_url, local_path=local_path)
            return True
        else:
            logger.error("git_clone_failed", repo_url=repo_url, stderr=result.stderr)
            return False

    @staticmethod
    def add_remote(workspace_path: str, remote_url: str, token: str = "", remote_name: str = "origin") -> bool:
        """Add or update a remote URL for the workspace."""
        auth_url = _inject_token(remote_url, token) if token else remote_url

        # Remove existing remote if present
        _run_git(workspace_path, ["remote", "remove", remote_name])
        result = _run_git(workspace_path, ["remote", "add", remote_name, auth_url])
        return result.returncode == 0

    @staticmethod
    def push(workspace_path: str, branch: str = "main", remote: str = "origin") -> bool:
        """Push local commits to remote."""
        result = _run_git(workspace_path, ["push", "-u", remote, branch])
        if result.returncode == 0:
            logger.info("git_pushed", workspace=workspace_path, branch=branch)
            return True
        else:
            logger.error("git_push_failed", workspace=workspace_path, stderr=result.stderr)
            return False

    @staticmethod
    def pull(workspace_path: str, branch: str = "main", remote: str = "origin") -> bool:
        """Pull latest from remote."""
        result = _run_git(workspace_path, ["pull", remote, branch])
        if result.returncode == 0:
            logger.info("git_pulled", workspace=workspace_path, branch=branch)
            return True
        else:
            logger.error("git_pull_failed", workspace=workspace_path, stderr=result.stderr)
            return False

    @staticmethod
    def commit_all(workspace_path: str, message: str) -> t.Optional[str]:
        """Stage all changes and commit. Returns commit hash or None."""
        _run_git(workspace_path, ["add", "."])
        result = _run_git(workspace_path, ["commit", "-m", message])
        if result.returncode != 0:
            logger.info("git_commit_no_changes", workspace=workspace_path)
            return None

        hash_result = _run_git(workspace_path, ["rev-parse", "HEAD"])
        commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else ""
        logger.info("git_committed", workspace=workspace_path, hash=commit_hash[:8])
        return commit_hash

    @staticmethod
    def status(workspace_path: str) -> t.Dict[str, t.Any]:
        """Get workspace Git status."""
        result = _run_git(workspace_path, ["status", "--porcelain"])
        if result.returncode != 0:
            return {"branch": "", "changes": [], "has_uncommitted": False}

        changes = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            status_code = line[:2].strip()
            file_path = line[3:]
            status_map = {
                "M": "modified", "A": "added", "D": "deleted",
                "??": "untracked", "R": "renamed",
            }
            changes.append({
                "path": file_path,
                "status": status_map.get(status_code, "modified"),
            })

        branch_result = _run_git(workspace_path, ["branch", "--show-current"])
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

        return {
            "branch": branch,
            "changes": changes,
            "has_uncommitted": len(changes) > 0,
        }

    @staticmethod
    def diff(workspace_path: str, file_path: str = "") -> str:
        """Get diff of uncommitted changes."""
        cmd = ["diff"]
        if file_path:
            cmd.append(file_path)
        result = _run_git(workspace_path, cmd)
        return result.stdout if result.returncode == 0 else ""

    # ---- Remote Git Provider API ----

    @staticmethod
    async def create_remote_repo(
        name: str,
        token: str,
        server_url: str,
        provider: str = "gitea",
        org: str = "",
        private: bool = True,
    ) -> t.Optional[GitRepoInfo]:
        """Create a new remote repository via provider API.

        Args:
            name: Repository name.
            token: User's API token.
            server_url: Git server base URL (from user credential).
            provider: Provider type (gitea/github/gitlab).
            org: Organization name (if empty, create under user).
            private: Whether repo is private.

        Returns:
            GitRepoInfo with clone URL, or None on failure.
        """
        if not server_url and provider != "github":
            logger.error("git_server_url_required", provider=provider)
            return None

        if provider == "gitea":
            return await _create_gitea_repo(server_url, token, name, org, private)
        elif provider == "github":
            return await _create_github_repo(token, name, org, private)
        elif provider == "gitlab":
            return await _create_gitlab_repo(server_url, token, name, org, private)
        else:
            logger.error("unsupported_git_provider", provider=provider)
            return None

    @staticmethod
    async def delete_remote_repo(
        name: str,
        token: str,
        server_url: str = "",
        provider: str = "gitea",
        owner: str = "",
    ) -> bool:
        """Delete a remote repository."""
        if provider == "gitea":
            if not server_url:
                logger.error("git_server_url_required_for_delete", provider=provider)
                return False
            return await _delete_gitea_repo(server_url, token, owner, name)
        else:
            logger.warning("delete_remote_not_implemented", provider=provider)
            return False


# ---- Internal helpers ----

def _run_git(workspace_path: str, args: t.List[str]) -> subprocess.CompletedProcess:
    """Run a git command in the workspace directory."""
    try:
        return subprocess.run(
            ["git"] + args,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.error("git_command_timeout", args=args, workspace=workspace_path)
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="timeout")
    except FileNotFoundError:
        logger.warning("git_not_installed")
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="git not found")


async def run_git_async(workspace_path: str, args: t.List[str]) -> subprocess.CompletedProcess:
    """Run git command without blocking the asyncio event loop."""
    return await asyncio.to_thread(_run_git, workspace_path, args)


def _inject_token(url: str, token: str) -> str:
    """Inject token into HTTPS Git URL for authentication.

    Example: https://gitea.example.com/org/repo.git
          -> https://oauth2:{token}@gitea.example.com/org/repo.git
    """
    if not token or not url.startswith("https://"):
        return url
    return url.replace("https://", f"https://oauth2:{token}@")


async def _create_gitea_repo(
    base_url: str, token: str, name: str, org: str, private: bool
) -> t.Optional[GitRepoInfo]:
    """Create repository on Gitea instance."""
    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}

    if org:
        url = f"{base_url}/api/v1/orgs/{org}/repos"
    else:
        url = f"{base_url}/api/v1/user/repos"

    payload = {
        "name": name,
        "private": private,
        "auto_init": False,
        "default_branch": "main",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code in (201, 200):
        data = resp.json()
        return GitRepoInfo(
            clone_url=data.get("clone_url", ""),
            html_url=data.get("html_url", ""),
            name=data.get("name", name),
            default_branch=data.get("default_branch", "main"),
        )
    else:
        logger.error(
            "gitea_create_repo_failed",
            status=resp.status_code,
            body=resp.text,
        )
        return None


async def _create_github_repo(
    token: str, name: str, org: str, private: bool
) -> t.Optional[GitRepoInfo]:
    """Create repository on GitHub."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    if org:
        url = f"https://api.github.com/orgs/{org}/repos"
    else:
        url = "https://api.github.com/user/repos"

    payload = {"name": name, "private": private, "auto_init": False}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code in (201, 200):
        data = resp.json()
        return GitRepoInfo(
            clone_url=data.get("clone_url", ""),
            html_url=data.get("html_url", ""),
            name=data.get("name", name),
            default_branch=data.get("default_branch", "main"),
        )
    else:
        logger.error("github_create_repo_failed", status=resp.status_code, body=resp.text)
        return None


async def _create_gitlab_repo(
    base_url: str, token: str, name: str, namespace: str, private: bool
) -> t.Optional[GitRepoInfo]:
    """Create repository on GitLab."""
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    url = f"{base_url}/api/v4/projects"
    payload = {
        "name": name,
        "visibility": "private" if private else "public",
    }
    if namespace:
        payload["namespace_id"] = namespace

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code in (201, 200):
        data = resp.json()
        return GitRepoInfo(
            clone_url=data.get("http_url_to_repo", ""),
            html_url=data.get("web_url", ""),
            name=data.get("name", name),
            default_branch=data.get("default_branch", "main"),
        )
    else:
        logger.error("gitlab_create_repo_failed", status=resp.status_code, body=resp.text)
        return None


async def _delete_gitea_repo(
    base_url: str, token: str, owner: str, name: str
) -> bool:
    """Delete repository on Gitea instance."""
    headers = {"Authorization": f"token {token}"}
    url = f"{base_url}/api/v1/repos/{owner}/{name}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(url, headers=headers)

    if resp.status_code == 204:
        logger.info("gitea_repo_deleted", owner=owner, name=name)
        return True
    else:
        logger.error("gitea_delete_repo_failed", status=resp.status_code, body=resp.text)
        return False


# ---- Backward-compatible module-level functions ----
# These are used by existing API endpoints in main.py

def git_init(workspace_path: str) -> bool:
    """Initialize a Git repository (backward-compatible wrapper)."""
    return GitOperations.init(workspace_path)


def git_status(workspace_path: str) -> t.Dict[str, t.Any]:
    """Get Git status (backward-compatible wrapper)."""
    return GitOperations.status(workspace_path)


def git_commit(workspace_path: str, message: str, paths: t.Optional[t.List[str]] = None) -> t.Optional[str]:
    """Commit changes (backward-compatible wrapper)."""
    if paths:
        for p in paths:
            _run_git(workspace_path, ["add", p])
        result = _run_git(workspace_path, ["commit", "-m", message])
        if result.returncode != 0:
            return None
        hash_result = _run_git(workspace_path, ["rev-parse", "HEAD"])
        return hash_result.stdout.strip() if hash_result.returncode == 0 else ""
    return GitOperations.commit_all(workspace_path, message)


def git_diff(workspace_path: str, file_path: str = "") -> str:
    """Get diff (backward-compatible wrapper)."""
    return GitOperations.diff(workspace_path, file_path)
