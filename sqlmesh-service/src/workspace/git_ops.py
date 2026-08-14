"""Git operations for workspace version control.

Provides both local Git CLI operations and remote Git provider API integration.
Supports: Gitea, GitHub, GitLab (provider-agnostic design).

Security note: authenticated URLs (token injected) must NEVER be logged. Every
log statement in this module logs the caller-supplied ``repo_url`` only.
"""

import ipaddress
import os
import re
import socket
import subprocess
import tempfile
import threading
import typing as t
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import asyncio
import httpx

from src.utils.errors import GitRepoUrlInvalidError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Only these transports are accepted for a workspace remote.
_ALLOWED_URL_SCHEMES = ("https", "ssh")

# Extra IPv4 blocks denied on top of ipaddress' private/loopback/link-local
# classification, as required by the platform SSRF policy.
_EXTRA_DENIED_IPV4_PREFIXES = ("9.", "10.", "11.", "21.", "30.")

# Public host a provider serves when the credential carries no explicit
# ``server_url``. Used to match a stored credential against a remote URL so a
# GitHub remote is never pushed with a Gitea token.
PROVIDER_DEFAULT_HOSTS: t.Dict[str, str] = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "bitbucket": "bitbucket.org",
}

# HTTPS basic-auth username to pair with a token. The password slot always
# carries the token; providers only differ in what they expect (or ignore) in
# the username slot.
PROVIDER_TOKEN_USERNAMES: t.Dict[str, str] = {
    "github": "x-access-token",
    "gitlab": "oauth2",
    "gitea": "oauth2",
    "bitbucket": "x-token-auth",
}
_DEFAULT_TOKEN_USERNAME = "oauth2"

# Git takes arguments positionally, so a value beginning with "-" would be read
# as an OPTION rather than a ref. Every caller-supplied ref is therefore matched
# against these patterns before it reaches the command line.
#
# Branch: must start alphanumeric, then a conservative ref character set.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
# Remote-qualified start point, e.g. "origin/feature/x".
_START_POINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
# Abbreviated or full object name.
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# ``git reset`` modes the platform exposes. Anything else is rejected instead of
# being forwarded, so no unexpected option can reach git.
RESET_MODES = ("soft", "mixed", "hard")


def is_valid_branch_name(name: str) -> bool:
    """Whether ``name`` is a branch name safe to pass to git."""
    return bool(_BRANCH_RE.match(name or ""))


def is_valid_start_point(ref: str) -> bool:
    """Whether ``ref`` is a start point (possibly ``remote/branch``) safe for git."""
    return bool(_START_POINT_RE.match(ref or ""))


def is_valid_commit_hash(value: str) -> bool:
    """Whether ``value`` is an abbreviated or full Git object name."""
    return bool(_COMMIT_RE.match(value or ""))


class GitRemoteReason:
    """Stable reason codes for remote Git failures.

    These codes cross the API boundary unchanged so the frontend can render a
    localized, actionable sentence instead of a raw git stderr dump.
    """

    CREDENTIAL_MISSING = "credential_missing"
    INVALID_CREDENTIAL = "invalid_credential"
    NO_WRITE_ACCESS = "no_write_access"
    REPO_NOT_FOUND = "repo_not_found"
    NON_FAST_FORWARD = "non_fast_forward"
    BRANCH_NOT_FOUND = "branch_not_found"
    HOST_UNREACHABLE = "host_unreachable"
    UNKNOWN = "unknown"


@dataclass
class GitAuth:
    """HTTPS credential material for one remote.

    The token is never written to ``.git/config`` nor passed on the command
    line: it is handed to git through the GIT_ASKPASS helper (see
    :func:`_git_env`), which reads it from the process environment.
    """

    token: str = ""
    provider: str = ""
    username: str = ""

    def basic_username(self) -> str:
        """Username to send in HTTPS basic auth alongside the token."""
        if self.username:
            return self.username
        return PROVIDER_TOKEN_USERNAMES.get(
            (self.provider or "").lower(), _DEFAULT_TOKEN_USERNAME
        )


@dataclass
class GitCommandResult:
    """Outcome of a remote git command.

    ``reason`` is one of :class:`GitRemoteReason` and is empty on success;
    ``detail`` holds the redacted stderr for diagnostics.
    """

    ok: bool
    reason: str = ""
    detail: str = ""


@dataclass
class RemoteAccessCheck:
    """Result of the pre-flight write-access probe against a provider API.

    ``performed`` is False when the provider is unknown or its API could not be
    reached, in which case the caller must not treat ``ok`` as an authorization
    decision.
    """

    ok: bool
    reason: str = ""
    detail: str = ""
    performed: bool = True


@dataclass
class WorktreeChange:
    """One change entry of the working tree.

    ``status`` is the raw porcelain letter so the frontend can decorate exactly
    like an editor does: ``M`` modified, ``A`` added, ``D`` deleted, ``R``
    renamed, ``C`` copied, ``T`` type-changed, ``?`` untracked, ``U`` conflict.

    A path that is both staged and dirty in the working tree produces two
    entries (one per group), which is how Git itself models it.
    """

    path: str
    status: str
    staged: bool
    original_path: str = ""


@dataclass
class GitRepoInfo:
    """Remote Git repository metadata."""
    clone_url: str
    html_url: str
    name: str
    default_branch: str = "main"


def validate_git_repo_url(url: str) -> str:
    """Validate a Git remote URL against the platform SSRF policy.

    Accepts ``https://host/...``, ``ssh://user@host/...`` and the scp-like
    ``git@host:org/repo.git`` form. Rejects any other scheme plus hosts that
    resolve to loopback, link-local, private or explicitly denied ranges, so a
    workspace remote can never be used to probe internal services.

    Returns the normalized URL, or raises :class:`GitRepoUrlInvalidError`.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise GitRepoUrlInvalidError("URL must not be empty")
    if len(candidate) > 512:
        raise GitRepoUrlInvalidError("URL exceeds 512 characters")

    host = _extract_host(candidate)
    if not host:
        raise GitRepoUrlInvalidError("URL must contain a host")

    _assert_host_is_public(host)
    return candidate


def _extract_host(url: str) -> str:
    """Extract the hostname from a supported Git URL form."""
    # scp-like syntax: git@host:org/repo.git (no scheme, single colon).
    if "://" not in url:
        if "@" not in url or ":" not in url.split("@", 1)[1]:
            raise GitRepoUrlInvalidError(
                "unsupported URL form; use https://, ssh:// or git@host:org/repo.git"
            )
        return url.split("@", 1)[1].split(":", 1)[0].strip("[]")

    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise GitRepoUrlInvalidError(
            f"scheme {parsed.scheme!r} is not allowed; use https or ssh"
        )
    return (parsed.hostname or "").strip("[]")


def _assert_host_is_public(host: str) -> None:
    """Reject hosts that point at loopback / internal address space."""
    for address in _resolve_host(host):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise GitRepoUrlInvalidError(f"host {host!r} resolved to an invalid address")

        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise GitRepoUrlInvalidError(
                f"host {host!r} points to a non-public address"
            )
        if ip.version == 4 and address.startswith(_EXTRA_DENIED_IPV4_PREFIXES):
            raise GitRepoUrlInvalidError(
                f"host {host!r} points to a denied network range"
            )


def _resolve_host(host: str) -> t.List[str]:
    """Resolve a hostname to its IP literals (identity for IP literals)."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise GitRepoUrlInvalidError(f"host {host!r} cannot be resolved") from exc
    return [info[4][0] for info in infos]


def extract_git_host(url: str) -> str:
    """Best-effort lowercase host of any Git URL form. Never raises.

    Unlike :func:`_extract_host` this tolerates every scheme (including plain
    http used by self-hosted dev instances) because it feeds credential
    matching, not the SSRF policy.
    """
    candidate = (url or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        # scp-like syntax: git@host:org/repo.git
        if "@" in candidate:
            return candidate.split("@", 1)[1].split(":", 1)[0].strip("[]").lower()
        return ""
    return (urlparse(candidate).hostname or "").lower()


def credential_host(provider: str, server_url: str = "") -> str:
    """Host a stored credential authenticates against.

    An explicit ``server_url`` always wins (self-hosted Gitea/GitLab, GitHub
    Enterprise); otherwise the provider's public host is used.
    """
    if server_url:
        host = extract_git_host(
            server_url if "://" in server_url else f"https://{server_url}"
        )
        if host:
            return host
    return PROVIDER_DEFAULT_HOSTS.get((provider or "").lower(), "")


def strip_url_credentials(url: str) -> str:
    """Remove an embedded ``user:password@`` prefix from an HTTP(S) URL.

    Older workspaces stored the token inside the remote URL. Stripping it keeps
    secrets out of ``.git/config``; authentication now flows through
    GIT_ASKPASS instead.
    """
    candidate = (url or "").strip()
    if "://" not in candidate:
        return candidate
    scheme, rest = candidate.split("://", 1)
    if scheme.lower() not in ("http", "https") or "@" not in rest:
        return candidate
    return f"{scheme}://{rest.rsplit('@', 1)[1]}"


def parse_repo_path(url: str) -> t.Tuple[str, str]:
    """Split a Git remote URL into ``(owner, repo)``.

    ``owner`` keeps any intermediate namespace (GitLab subgroups), ``repo`` has
    the ``.git`` suffix removed. Returns ``("", "")`` when the shape is unknown.
    """
    candidate = (url or "").strip()
    if "://" in candidate:
        path = urlparse(candidate).path
    elif "@" in candidate and ":" in candidate:
        path = candidate.split("@", 1)[1].split(":", 1)[1]
    else:
        return "", ""

    segments = [seg for seg in path.strip("/").split("/") if seg]
    if len(segments) < 2:
        return "", ""
    repo = segments[-1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return "/".join(segments[:-1]), repo


def _parse_porcelain_z(raw: str) -> t.List[WorktreeChange]:
    """Parse ``git status --porcelain=v1 -z`` output.

    Record layout is ``XY <path> NUL``, where rename/copy records append the
    source path as an extra NUL-terminated field. Index and worktree states are
    reported independently, so a file staged AND dirty yields two entries.
    """
    fields = raw.split("\0")
    changes: t.List[WorktreeChange] = []
    index = 0

    while index < len(fields):
        entry = fields[index]
        index += 1
        # Records are "XY " + path, so anything shorter is the trailing empty
        # field produced by the final NUL.
        if len(entry) < 4:
            continue

        index_status, worktree_status = entry[0], entry[1]
        path = entry[3:]

        original_path = ""
        if index_status in ("R", "C") or worktree_status in ("R", "C"):
            if index < len(fields):
                original_path = fields[index]
                index += 1

        # Untracked and ignored are reported with both slots identical.
        if index_status == "?" and worktree_status == "?":
            changes.append(WorktreeChange(path, "?", False))
            continue
        if index_status == "!" and worktree_status == "!":
            continue

        # Unmerged paths: a conflict is a single actionable entry, never a
        # staged/unstaged pair.
        if (
            index_status == "U"
            or worktree_status == "U"
            or (index_status == worktree_status and index_status in ("A", "D"))
        ):
            changes.append(WorktreeChange(path, "U", False, original_path))
            continue

        if index_status not in (" ", "?"):
            changes.append(WorktreeChange(path, index_status, True, original_path))
        if worktree_status not in (" ", "?"):
            changes.append(WorktreeChange(path, worktree_status, False, original_path))

    return changes


def classify_remote_error(stderr: str) -> str:
    """Map git/provider stderr onto a :class:`GitRemoteReason` code.

    Ordering matters: the checks run from the most specific, most actionable
    condition ("no credential at all") down to the generic fallback.
    """
    text = (stderr or "").lower()
    if not text:
        return GitRemoteReason.UNKNOWN

    if "could not read username" in text or "terminal prompts disabled" in text:
        return GitRemoteReason.CREDENTIAL_MISSING
    if (
        "invalid username or password" in text
        or "authentication failed" in text
        or "bad credentials" in text
        or "401" in text
    ):
        return GitRemoteReason.INVALID_CREDENTIAL
    if (
        "write access to repository not granted" in text
        or "insufficient permission" in text
        or ("permission" in text and "denied" in text)
        or "protected branch" in text
    ):
        return GitRemoteReason.NO_WRITE_ACCESS
    if "repository not found" in text or "does not appear to be a git repository" in text:
        return GitRemoteReason.REPO_NOT_FOUND
    if "non-fast-forward" in text or "fetch first" in text or "rejected" in text:
        return GitRemoteReason.NON_FAST_FORWARD
    if (
        "couldn't find remote ref" in text
        or "src refspec" in text
        or "does not match any" in text
        or "pathspec" in text
    ):
        return GitRemoteReason.BRANCH_NOT_FOUND
    if (
        "could not resolve host" in text
        or "connection refused" in text
        or "connection timed out" in text
        or "timeout" in text
    ):
        return GitRemoteReason.HOST_UNREACHABLE
    # A 403 that matched none of the above is still an authorization problem.
    if "403" in text:
        return GitRemoteReason.NO_WRITE_ACCESS
    return GitRemoteReason.UNKNOWN


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
    def clone(
        repo_url: str,
        local_path: str,
        branch: str = "main",
        auth: t.Optional[GitAuth] = None,
    ) -> GitCommandResult:
        """Clone a remote repository into ``local_path``.

        The URL is passed verbatim (no credential embedded) so the token never
        reaches the process argument list; authentication flows through the
        GIT_ASKPASS helper instead.
        """
        cmd = ["git"] + _GIT_NO_CREDENTIAL_HELPER + ["clone"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [repo_url, local_path]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=_git_env(auth),
            )
        except subprocess.TimeoutExpired:
            logger.error("git_clone_timeout", repo_url=repo_url, local_path=local_path)
            return GitCommandResult(
                ok=False,
                reason=GitRemoteReason.HOST_UNREACHABLE,
                detail="clone timed out",
            )

        if result.returncode == 0:
            logger.info("git_cloned", repo_url=repo_url, local_path=local_path)
            return GitCommandResult(ok=True)
        return _failed_result("git_clone_failed", result.stderr, auth, repo_url=repo_url)

    @staticmethod
    def ls_remote_branches(
        repo_url: str, auth: t.Optional[GitAuth] = None
    ) -> t.List[str]:
        """List remote branch names via ``git ls-remote --heads``.

        Read-only and does not require a local clone, so it can back the
        branch picker shown before a workspace is opened. Returns an empty
        list on failure (the caller falls back to the default branch).
        """
        try:
            result = subprocess.run(
                ["git"] + _GIT_NO_CREDENTIAL_HELPER + ["ls-remote", "--heads", repo_url],
                capture_output=True,
                text=True,
                timeout=30,
                env=_git_env(auth),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("git_ls_remote_failed", repo_url=repo_url, error=str(exc))
            return []

        if result.returncode != 0:
            _failed_result("git_ls_remote_failed", result.stderr, auth, repo_url=repo_url)
            return []

        branches: t.List[str] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                branches.append(parts[1][len("refs/heads/"):])
        return branches

    @staticmethod
    def fetch_and_checkout(
        workspace_path: str,
        branch: str,
        auth: t.Optional[GitAuth] = None,
        remote: str = "origin",
    ) -> GitCommandResult:
        """Fetch from ``remote`` and check out ``branch`` in an existing clone.

        Used when a member re-opens a workspace they already have locally and
        possibly switches branch.
        """
        _ensure_clean_remote(workspace_path, remote)

        fetch = _run_git(workspace_path, ["fetch", remote, "--prune"], auth)
        if fetch.returncode != 0:
            return _failed_result(
                "git_fetch_failed", fetch.stderr, auth, workspace=workspace_path
            )

        checkout = _run_git(workspace_path, ["checkout", branch])
        if checkout.returncode != 0:
            # Branch not present locally yet: create it tracking the remote.
            checkout = _run_git(
                workspace_path, ["checkout", "-B", branch, f"{remote}/{branch}"]
            )
        if checkout.returncode != 0:
            return _failed_result(
                "git_checkout_failed",
                checkout.stderr,
                auth,
                workspace=workspace_path,
                branch=branch,
            )

        logger.info("git_checked_out", workspace=workspace_path, branch=branch)
        return GitCommandResult(ok=True)

    @staticmethod
    def add_remote(
        workspace_path: str, remote_url: str, remote_name: str = "origin"
    ) -> bool:
        """Point ``remote_name`` at ``remote_url``.

        The URL is stored WITHOUT credentials: tokens are supplied per command
        via GIT_ASKPASS so they never land in ``.git/config``.
        """
        clean_url = strip_url_credentials(remote_url)

        # Remove existing remote if present
        _run_git(workspace_path, ["remote", "remove", remote_name])
        result = _run_git(workspace_path, ["remote", "add", remote_name, clean_url])
        return result.returncode == 0

    @staticmethod
    def push(
        workspace_path: str,
        branch: str = "main",
        remote: str = "origin",
        auth: t.Optional[GitAuth] = None,
    ) -> GitCommandResult:
        """Push local commits to ``remote``."""
        _ensure_clean_remote(workspace_path, remote)

        result = _run_git(workspace_path, ["push", "-u", remote, branch], auth)
        if result.returncode == 0:
            logger.info("git_pushed", workspace=workspace_path, branch=branch)
            return GitCommandResult(ok=True)
        return _failed_result(
            "git_push_failed",
            result.stderr,
            auth,
            workspace=workspace_path,
            branch=branch,
        )

    @staticmethod
    def pull(
        workspace_path: str,
        branch: str = "main",
        remote: str = "origin",
        auth: t.Optional[GitAuth] = None,
    ) -> GitCommandResult:
        """Pull latest from ``remote``."""
        _ensure_clean_remote(workspace_path, remote)

        result = _run_git(workspace_path, ["pull", remote, branch], auth)
        if result.returncode == 0:
            logger.info("git_pulled", workspace=workspace_path, branch=branch)
            return GitCommandResult(ok=True)
        return _failed_result(
            "git_pull_failed",
            result.stderr,
            auth,
            workspace=workspace_path,
            branch=branch,
        )

    @staticmethod
    def fetch(
        workspace_path: str,
        remote: str = "origin",
        prune: bool = True,
        auth: t.Optional[GitAuth] = None,
    ) -> GitCommandResult:
        """Update remote-tracking refs without touching the working tree."""
        _ensure_clean_remote(workspace_path, remote)

        args = ["fetch", remote, "--tags"]
        if prune:
            args.append("--prune")

        result = _run_git(workspace_path, args, auth)
        if result.returncode == 0:
            logger.info("git_fetched", workspace=workspace_path, remote=remote)
            return GitCommandResult(ok=True)
        return _failed_result(
            "git_fetch_failed", result.stderr, auth, workspace=workspace_path
        )

    @staticmethod
    def checkout(
        workspace_path: str,
        branch: str,
        create: bool = False,
        start_point: str = "",
        track: bool = False,
    ) -> GitCommandResult:
        """Check out ``branch``, optionally creating it.

        With ``start_point`` (e.g. ``origin/dev``) and ``track`` the new local
        branch is wired to its remote counterpart, which is what "materialize a
        remote branch locally" means: subsequent pull/push need no arguments.
        """
        if create:
            args = ["checkout", "-b", branch]
            if track and start_point:
                args += ["--track", start_point]
            elif start_point:
                args.append(start_point)
        else:
            args = ["checkout", branch]

        result = _run_git(workspace_path, args)
        if result.returncode == 0:
            logger.info(
                "git_checked_out",
                workspace=workspace_path,
                branch=branch,
                created=create,
                start_point=start_point,
            )
            return GitCommandResult(ok=True)
        return _failed_result(
            "git_checkout_failed", result.stderr, None,
            workspace=workspace_path, branch=branch,
        )

    @staticmethod
    def reset(
        workspace_path: str, commit_hash: str, mode: str = "mixed"
    ) -> GitCommandResult:
        """Move the current branch to ``commit_hash``.

        ``mode`` must be one of :data:`RESET_MODES`; ``hard`` also discards
        working-tree changes, which is why the caller is expected to confirm it
        with the user first.
        """
        if mode not in RESET_MODES:
            return GitCommandResult(
                ok=False, reason=GitRemoteReason.UNKNOWN, detail=f"invalid mode {mode!r}"
            )

        result = _run_git(workspace_path, ["reset", f"--{mode}", commit_hash])
        if result.returncode == 0:
            logger.info(
                "git_reset",
                workspace=workspace_path,
                commit=commit_hash[:8],
                mode=mode,
            )
            return GitCommandResult(ok=True)
        return _failed_result(
            "git_reset_failed", result.stderr, None,
            workspace=workspace_path, mode=mode,
        )

    @staticmethod
    def delete_branch(
        workspace_path: str, branch: str, force: bool = False
    ) -> GitCommandResult:
        """Delete a local branch.

        Without ``force`` git refuses to drop a branch whose commits are not
        merged anywhere, which is the safety net we want by default.
        """
        flag = "-D" if force else "-d"
        result = _run_git(workspace_path, ["branch", flag, branch])
        if result.returncode == 0:
            logger.info("git_branch_deleted", workspace=workspace_path, branch=branch)
            return GitCommandResult(ok=True)
        return _failed_result(
            "git_branch_delete_failed", result.stderr, None,
            workspace=workspace_path, branch=branch,
        )

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
    def list_changes(workspace_path: str) -> t.List[WorktreeChange]:
        """Return the working-tree changes of ``workspace_path``.

        Two flags matter here:

        * ``-uall`` expands untracked DIRECTORIES into their individual files.
          The default (``normal``) collapses them into a single ``dir/`` entry,
          which carries no file name and renders as a blank row.
        * ``-z`` emits NUL-separated records, so paths containing spaces,
          quotes or non-ASCII characters survive verbatim (the default format
          escapes and quotes them).
        """
        result = _run_git(
            workspace_path,
            ["status", "--porcelain=v1", "-z", "-uall", "--find-renames"],
        )
        if result.returncode != 0:
            logger.warning(
                "git_status_failed", workspace=workspace_path, stderr=result.stderr
            )
            return []
        return _parse_porcelain_z(result.stdout)

    @staticmethod
    def current_branch(workspace_path: str) -> str:
        """Name of the checked-out branch (empty on a detached HEAD)."""
        result = _run_git(workspace_path, ["branch", "--show-current"])
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def status(workspace_path: str) -> t.Dict[str, t.Any]:
        """Get workspace Git status."""
        changes = GitOperations.list_changes(workspace_path)
        return {
            "branch": GitOperations.current_branch(workspace_path) or "main",
            "changes": [
                {
                    "path": change.path,
                    "status": change.status,
                    "staged": change.staged,
                    "original_path": change.original_path,
                }
                for change in changes
            ],
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
            return await _create_github_repo(server_url, token, name, org, private)
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

# GIT_ASKPASS helper. It holds no secret itself: the credential is passed per
# command through the environment, which keeps the token out of .git/config,
# out of the process argument list and off disk.
_ASKPASS_SCRIPT_BODY = """#!/bin/sh
case "$1" in
  Username*) printf '%s' "${AGENTICDATA_GIT_USERNAME}" ;;
  *)         printf '%s' "${AGENTICDATA_GIT_PASSWORD}" ;;
esac
"""

_askpass_path: t.Optional[str] = None
_askpass_lock = threading.Lock()

# Clears every configured credential helper for the duration of one command.
# Without this, an ambient helper (macOS keychain, `store`, a CI helper) answers
# the prompt BEFORE GIT_ASKPASS is consulted and git would silently reuse a
# stale token, making credential resolution non-deterministic.
_GIT_NO_CREDENTIAL_HELPER = ["-c", "credential.helper="]


def _askpass_script() -> str:
    """Path to the shared GIT_ASKPASS helper, created once per process."""
    global _askpass_path
    with _askpass_lock:
        if _askpass_path and os.path.exists(_askpass_path):
            return _askpass_path
        fd, path = tempfile.mkstemp(prefix="agenticdata-git-askpass-", suffix=".sh")
        with os.fdopen(fd, "w") as handle:
            handle.write(_ASKPASS_SCRIPT_BODY)
        os.chmod(path, 0o700)
        _askpass_path = path
        logger.info("git_askpass_helper_created", path=path)
        return path


def _git_env(auth: t.Optional[GitAuth] = None) -> t.Dict[str, str]:
    """Environment for a git subprocess that never blocks on a prompt.

    With a token, credentials are answered by the GIT_ASKPASS helper from
    environment variables. Without one, no helper is installed so git fails
    fast with "could not read Username" instead of silently submitting
    garbage credentials and masking the real problem.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    env.pop("AGENTICDATA_GIT_USERNAME", None)
    env.pop("AGENTICDATA_GIT_PASSWORD", None)

    if auth and auth.token:
        env["GIT_ASKPASS"] = _askpass_script()
        env["AGENTICDATA_GIT_USERNAME"] = auth.basic_username()
        env["AGENTICDATA_GIT_PASSWORD"] = auth.token
    return env


def _redact(text: str, token: str) -> str:
    """Strip a token from command output before it reaches the logs."""
    if not text:
        return ""
    return text.replace(token, "***") if token else text


def _failed_result(
    event: str,
    stderr: str,
    auth: t.Optional[GitAuth],
    **log_fields: t.Any,
) -> GitCommandResult:
    """Redact, classify and log one failed git command."""
    detail = _redact(stderr, auth.token if auth else "")
    reason = classify_remote_error(detail)
    logger.error(event, reason=reason, stderr=detail, **log_fields)
    return GitCommandResult(ok=False, reason=reason, detail=detail)


def _ensure_clean_remote(workspace_path: str, remote: str = "origin") -> None:
    """Drop any credential a previous version embedded in the remote URL.

    Best-effort: authentication is expected to come from GIT_ASKPASS, so a
    credential sitting in ``.git/config`` is only a leak, never a requirement.
    """
    current = _run_git(workspace_path, ["remote", "get-url", remote])
    if current.returncode != 0:
        return
    original = current.stdout.strip()
    cleaned = strip_url_credentials(original)
    if cleaned and cleaned != original:
        _run_git(workspace_path, ["remote", "set-url", remote, cleaned])
        logger.info(
            "git_remote_credential_stripped", workspace=workspace_path, remote=remote
        )


def _run_git(
    workspace_path: str,
    args: t.List[str],
    auth: t.Optional[GitAuth] = None,
) -> subprocess.CompletedProcess:
    """Run a git command in the workspace directory."""
    try:
        return subprocess.run(
            ["git"] + _GIT_NO_CREDENTIAL_HELPER + args,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=60,
            env=_git_env(auth),
        )
    except subprocess.TimeoutExpired:
        logger.error("git_command_timeout", args=args, workspace=workspace_path)
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="timeout")
    except FileNotFoundError:
        logger.warning("git_not_installed")
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="git not found")


async def run_git_async(
    workspace_path: str,
    args: t.List[str],
    auth: t.Optional[GitAuth] = None,
) -> subprocess.CompletedProcess:
    """Run git command without blocking the asyncio event loop."""
    return await asyncio.to_thread(_run_git, workspace_path, args, auth)


async def clone_async(
    repo_url: str,
    local_path: str,
    branch: str = "main",
    auth: t.Optional[GitAuth] = None,
) -> GitCommandResult:
    """Off-thread ``git clone`` so the event loop stays responsive."""
    return await asyncio.to_thread(
        GitOperations.clone, repo_url, local_path, branch, auth
    )


async def ls_remote_branches_async(
    repo_url: str, auth: t.Optional[GitAuth] = None
) -> t.List[str]:
    """Off-thread ``git ls-remote --heads``."""
    return await asyncio.to_thread(GitOperations.ls_remote_branches, repo_url, auth)


async def fetch_and_checkout_async(
    workspace_path: str, branch: str, auth: t.Optional[GitAuth] = None
) -> GitCommandResult:
    """Off-thread fetch + checkout."""
    return await asyncio.to_thread(
        GitOperations.fetch_and_checkout, workspace_path, branch, auth
    )


async def push_async(
    workspace_path: str,
    branch: str = "main",
    remote: str = "origin",
    auth: t.Optional[GitAuth] = None,
) -> GitCommandResult:
    """Off-thread ``git push``."""
    return await asyncio.to_thread(
        GitOperations.push, workspace_path, branch, remote, auth
    )


async def pull_async(
    workspace_path: str,
    branch: str = "main",
    remote: str = "origin",
    auth: t.Optional[GitAuth] = None,
) -> GitCommandResult:
    """Off-thread ``git pull``."""
    return await asyncio.to_thread(
        GitOperations.pull, workspace_path, branch, remote, auth
    )


async def fetch_async(
    workspace_path: str,
    remote: str = "origin",
    prune: bool = True,
    auth: t.Optional[GitAuth] = None,
) -> GitCommandResult:
    """Off-thread ``git fetch``."""
    return await asyncio.to_thread(
        GitOperations.fetch, workspace_path, remote, prune, auth
    )


async def validate_git_repo_url_async(url: str) -> str:
    """Off-thread URL validation (hostname resolution may block)."""
    return await asyncio.to_thread(validate_git_repo_url, url)


# ---- Pre-flight write-access probe ----

async def check_remote_write_access(
    repo_url: str, auth: GitAuth, server_url: str = ""
) -> RemoteAccessCheck:
    """Probe the provider API for push permission on ``repo_url``.

    Running this before ``git push`` turns an opaque HTTP 403 buried in git
    stderr into an actionable, localizable reason. Providers we cannot probe
    return ``performed=False`` so the caller still attempts the push.
    """
    owner, repo = parse_repo_path(repo_url)
    provider = (auth.provider or "").lower()
    if not owner or not repo:
        return RemoteAccessCheck(ok=True, performed=False, detail="unrecognized repo URL")
    if not auth.token:
        return RemoteAccessCheck(ok=False, reason=GitRemoteReason.CREDENTIAL_MISSING)

    host = extract_git_host(repo_url)
    try:
        if provider == "github":
            api_base = (
                "https://api.github.com"
                if host == "github.com"
                else f"{(server_url or f'https://{host}').rstrip('/')}/api/v3"
            )
            return await _probe_repo_permission(
                url=f"{api_base}/repos/{owner}/{repo}",
                headers={
                    "Authorization": f"Bearer {auth.token}",
                    "Accept": "application/vnd.github+json",
                },
                permission_reader=lambda data: bool(
                    (data.get("permissions") or {}).get("push")
                ),
            )
        if provider == "gitea":
            base = (server_url or f"https://{host}").rstrip("/")
            return await _probe_repo_permission(
                url=f"{base}/api/v1/repos/{owner}/{repo}",
                headers={"Authorization": f"token {auth.token}"},
                permission_reader=lambda data: bool(
                    (data.get("permissions") or {}).get("push")
                ),
            )
        if provider == "gitlab":
            base = (server_url or f"https://{host}").rstrip("/")
            project = quote(f"{owner}/{repo}", safe="")
            return await _probe_repo_permission(
                url=f"{base}/api/v4/projects/{project}",
                headers={"PRIVATE-TOKEN": auth.token},
                permission_reader=_gitlab_can_push,
            )
    except httpx.HTTPError as exc:
        logger.warning("git_access_check_unavailable", host=host, error=str(exc))
        return RemoteAccessCheck(ok=True, performed=False, detail=str(exc))

    logger.info("git_access_check_skipped", provider=provider, host=host)
    return RemoteAccessCheck(ok=True, performed=False, detail=f"provider {provider} not probeable")


async def _probe_repo_permission(
    url: str,
    headers: t.Dict[str, str],
    permission_reader: t.Callable[[t.Dict[str, t.Any]], bool],
) -> RemoteAccessCheck:
    """Fetch repository metadata and decide whether the token may push."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code in (401, 403):
        return RemoteAccessCheck(ok=False, reason=GitRemoteReason.INVALID_CREDENTIAL)
    if resp.status_code == 404:
        return RemoteAccessCheck(ok=False, reason=GitRemoteReason.REPO_NOT_FOUND)
    if resp.status_code != 200:
        # An unexpected status is not an authorization decision: let git try.
        return RemoteAccessCheck(
            ok=True, performed=False, detail=f"unexpected status {resp.status_code}"
        )

    if not permission_reader(resp.json()):
        return RemoteAccessCheck(ok=False, reason=GitRemoteReason.NO_WRITE_ACCESS)
    return RemoteAccessCheck(ok=True)


def _gitlab_can_push(data: t.Dict[str, t.Any]) -> bool:
    """GitLab grants push from access level 30 (Developer) upwards."""
    permissions = data.get("permissions") or {}
    levels = [
        (permissions.get(scope) or {}).get("access_level", 0)
        for scope in ("project_access", "group_access")
    ]
    return max(levels or [0]) >= 30


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
    server_url: str, token: str, name: str, org: str, private: bool
) -> t.Optional[GitRepoInfo]:
    """Create repository on GitHub (github.com or GitHub Enterprise Server)."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    host = extract_git_host(server_url) if server_url else "github.com"
    api_base = (
        "https://api.github.com"
        if host in ("", "github.com")
        else f"{server_url.rstrip('/')}/api/v3"
    )
    if org:
        url = f"{api_base}/orgs/{org}/repos"
    else:
        url = f"{api_base}/user/repos"

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
