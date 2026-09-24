"""Commit author identity: where it comes from and that it actually lands.

The failure this guards against: ``git commit`` was run bare, so the author
came from the process user's global gitconfig. That file exists on a dev
machine (the developer's identity silently leaked into every commit) and is
absent in the container image, where the same commit died with
``fatal: unable to auto-detect email address (got 'app@<host>.(none)')`` —
even for a user who HAD configured a username and email in Git credentials.

The contract pinned here:

* the identity resolves from the user's git credential — the one matching the
  remote's host first, then the default credential — and falls back to the
  platform identity, never to a failure;
* the chosen identity is injected into the commit command itself, so the
  ambient gitconfig (present or absent) cannot change who authors a commit.

Git commands run against real repositories in ``tmp_path``; the credential
database is replaced by a stub repository (the runtime never writes that
table, so seeding rows here would be the wrong kind of real).
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from src.git.ops import GitAuthor, GitOperations, GitWorktreeOperations, author_args
from src.workspace.git_credentials import resolve_commit_identity

PLATFORM = GitAuthor(
    GitWorktreeOperations.COMMITTER_NAME, GitWorktreeOperations.COMMITTER_EMAIL
)
_USER = "u1"
_URL = "https://github.com/acme/shop.git"


def _cred(**fields) -> SimpleNamespace:
    base = {
        "provider": "github",
        "server_url": "",
        "email": "",
        "username": "",
        "token": "tok",
    }
    base.update(fields)
    return SimpleNamespace(**base)


@pytest.fixture()
def credential_repo(monkeypatch):
    """Install a stub credential repository; the test sets its rows."""

    class Stub:
        for_host: SimpleNamespace | None = None
        default: SimpleNamespace | None = None

    async def get_for_host(self, user_id, host):
        return Stub.for_host

    async def get_default(self, user_id):
        return Stub.default

    monkeypatch.setattr(
        "src.workspace.repository.UserGitCredentialRepository.get_for_host", get_for_host
    )
    monkeypatch.setattr(
        "src.workspace.repository.UserGitCredentialRepository.get_default", get_default
    )
    return Stub


# ── Resolution order ─────────────────────────────────────────────────────────


async def test_the_credential_matching_the_remote_host_wins(credential_repo):
    credential_repo.for_host = _cred(email="host@x.com", username="hostuser")
    credential_repo.default = _cred(email="default@x.com", username="defaultuser")

    author = await resolve_commit_identity(_USER, _URL)

    assert author == GitAuthor("hostuser", "host@x.com")


async def test_the_default_credential_is_the_fallback(credential_repo):
    """Attribution, not authentication: a credential for ANOTHER host is still
    a better answer than a machine name."""
    credential_repo.for_host = None
    credential_repo.default = _cred(email="default@x.com", username="defaultuser")

    author = await resolve_commit_identity(_USER, _URL)

    assert author == GitAuthor("defaultuser", "default@x.com")


async def test_a_credential_without_an_email_is_skipped(credential_repo):
    """Email is what git needs; a token-only credential cannot author."""
    credential_repo.for_host = _cred(email="", username="hostuser")
    credential_repo.default = _cred(email="default@x.com", username="defaultuser")

    author = await resolve_commit_identity(_USER, _URL)

    assert author == GitAuthor("defaultuser", "default@x.com")


async def test_a_missing_username_falls_back_to_the_email_local_part(credential_repo):
    """The form makes the username optional; the forges derive a display name
    from the email's local part the same way."""
    credential_repo.for_host = _cred(email="huxinglong666@163.com", username="")

    author = await resolve_commit_identity(_USER, _URL)

    assert author == GitAuthor("huxinglong666", "huxinglong666@163.com")


async def test_no_credentials_at_all_means_a_platform_authored_commit(credential_repo):
    """A workspace without any credential must still commit."""
    credential_repo.for_host = None
    credential_repo.default = None

    assert await resolve_commit_identity(_USER, _URL) == PLATFORM


async def test_an_empty_user_id_is_platform_authored(credential_repo):
    assert await resolve_commit_identity("", _URL) == PLATFORM


async def test_a_lookup_failure_never_fails_the_commit(credential_repo, monkeypatch):
    """A credential DB hiccup degrades attribution; it must not 500 a commit."""

    async def boom(self, user_id, host):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "src.workspace.repository.UserGitCredentialRepository.get_for_host", boom
    )
    monkeypatch.setattr(
        "src.workspace.repository.UserGitCredentialRepository.get_default", boom
    )

    assert await resolve_commit_identity(_USER, _URL) == PLATFORM


# ── The identity actually lands on the commit ────────────────────────────────


def _init_repo(path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "checkout", "-q", "-b", "main"], check=True)


def _last_author(path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "log", "-1", "--format=%an <%ae>"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_commit_all_stamps_the_given_author(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")

    commit = GitOperations.commit_all(
        str(tmp_path), "msg", GitAuthor("Hu Xinglong", "huxinglong666@163.com")
    )

    assert commit
    assert _last_author(tmp_path) == "Hu Xinglong <huxinglong666@163.com>"


def test_commit_all_defaults_to_the_platform_identity(tmp_path):
    """The scaffold bootstrap commit is platform-authored — and in the
    container (no global gitconfig) this is what used to fail silently."""
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")

    commit = GitOperations.commit_all(str(tmp_path), "msg")

    assert commit
    assert _last_author(tmp_path) == "DataCode <datacode@localhost>"


def test_commit_all_reports_no_changes_as_none(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")
    assert GitOperations.commit_all(str(tmp_path), "first")

    assert GitOperations.commit_all(str(tmp_path), "second") is None


def test_the_injected_identity_wins_over_repository_config(tmp_path):
    """A stale identity in the repo's own config must not leak into commits."""
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Stale Name"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "stale@x.com"], check=True
    )
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")

    GitOperations.commit_all(str(tmp_path), "msg", GitAuthor("Real User", "real@x.com"))

    assert _last_author(tmp_path) == "Real User <real@x.com>"


def test_author_args_shape():
    assert author_args(GitAuthor("N", "e@x.com")) == [
        "-c", "user.name=N", "-c", "user.email=e@x.com",
    ]


def test_init_commits_with_the_platform_identity(tmp_path):
    """`git init` also committed bare: in the container the result was IGNORED
    and init reported success while the repository had no commits at all."""
    assert GitOperations.init(str(tmp_path))

    log = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "--format=%an <%ae>%n%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "DataCode <datacode@localhost>\nInitial commit" in log


# ── The endpoint composes the two ────────────────────────────────────────────


async def test_the_commit_endpoint_authors_with_the_resolved_identity(
    tmp_path, monkeypatch
):
    """The panel commit used to run a bare `git commit`; pin that the endpoint
    resolves the identity and injects it into the command."""
    from src.api.rest.git import git_commit

    calls: list[list[str]] = []

    async def fake_run_git(path, args, auth=None):
        calls.append(list(args))
        if args[:3] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout=_URL + "\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")

    async def no_lock(path, operation):
        return None

    async def identity(user_id, repo_url):
        assert user_id == _USER
        assert repo_url == _URL
        return GitAuthor("Real User", "real@x.com")

    monkeypatch.setattr("src.git.ops.run_git_async", fake_run_git)
    monkeypatch.setattr(
        "src.sqlmesh.env_locks.check_environment_lock", no_lock, raising=False
    )
    monkeypatch.setattr(
        "src.workspace.git_credentials.resolve_commit_identity", identity
    )
    monkeypatch.setattr(
        "src.api.deps.validate_workspace_path", lambda value: value, raising=False
    )

    from src.api.models.requests import GitCommitRequest

    result = await git_commit(
        GitCommitRequest(
            workspace_path=str(tmp_path), message="msg", user_id=_USER
        )
    )

    assert result == {"success": True, "commit_hash": "abc123"}
    commit_call = next(c for c in calls if "commit" in c)
    assert commit_call == [
        "-c", "user.name=Real User", "-c", "user.email=real@x.com",
        "commit", "-m", "msg",
    ]
