"""Remote provisioning: create-or-reuse, decided before anything is written.

The contract these tests pin:

* a repository that does NOT exist is created — private, empty — at exactly the
  address the user typed, and the caller is told to scaffold;
* a repository that already carries code is REUSED and the caller is told NOT to
  scaffold (this is the assertion that protects a real project from being
  overwritten by the platform template);
* a repository that exists but has no branch is also reused, with no template;
* an owner the token may not create under is REFUSED with an actionable reason
  instead of silently redirected to the token's personal account.

The last one is the decision that most needs pinning: falling back would hand
the user a workspace bound to a repository they never asked for, while the URL
they typed quietly meant something else.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.git_reasons import GitRemoteReason  # noqa: E402

from src.git.ops import GitAuth, GitCommandResult  # noqa: E402
from src.utils.errors import GitRemoteAccessError  # noqa: E402
from src.workspace.scaffold import (  # noqa: E402
    RemoteProvisionState,
    WorkspaceProvisioner,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for provisioning tests"
)

_USER = "u1"
_URL = "https://github.com/BiyoTech/loganhu-test.git"
_OWN_URL = "https://github.com/loganhu/loganhu-test.git"


@pytest.fixture()
def provisioner():
    return WorkspaceProvisioner()


@pytest.fixture()
def fake_provider(monkeypatch):
    """Drive the provider API and ``git ls-remote`` from one declarative state.

    Both are stubbed at the seam the production code actually calls, so the
    routing decisions (which endpoint, which owner) are observable.
    """

    class Provider:
        def __init__(self) -> None:
            # "absent" | "with_branches" | "empty"
            self.repo = "absent"
            self.login = "loganhu"
            self.create_status = 201
            self.branches: list[str] = []
            self.requests: list[tuple[str, str]] = []
            self.namespaces: list[dict] = []

        def handle(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.requests.append((request.method, url))

            if request.method == "GET" and url.endswith("/user"):
                if not self.login:
                    return httpx.Response(401, json={"message": "Bad credentials"})
                return httpx.Response(200, json={"login": self.login})

            if request.method == "GET" and "/namespaces" in url:
                return httpx.Response(200, json=self.namespaces)

            if request.method == "GET" and "/repos/" in url:
                if self.repo == "absent":
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json={"permissions": {"push": True}})

            if request.method == "POST" and url.endswith("/repos"):
                if self.create_status in (200, 201):
                    return httpx.Response(
                        self.create_status,
                        json={
                            "clone_url": _URL,
                            "html_url": _URL[:-4],
                            "name": "loganhu-test",
                            "default_branch": "main",
                        },
                    )
                return httpx.Response(
                    self.create_status, json={"message": "Resource not accessible"}
                )

            return httpx.Response(500, json={"message": f"unexpected {url}"})

    provider = Provider()

    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(provider.handle)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("src.git.ops.httpx.AsyncClient", _client)

    async def _ls_remote(repo_url, auth=None):
        if provider.repo == "absent":
            return GitCommandResult(
                ok=False,
                reason=GitRemoteReason.REPO_NOT_FOUND,
                detail="remote: Repository not found.",
            )
        return GitCommandResult(ok=True)

    async def _ls_remote_branches(repo_url, auth=None):
        return list(provider.branches)

    monkeypatch.setattr("src.workspace.scaffold.ls_remote_async", _ls_remote)
    monkeypatch.setattr(
        "src.workspace.scaffold.ls_remote_branches_async", _ls_remote_branches
    )
    return provider


@pytest.fixture()
def with_credential(monkeypatch):
    """Resolve a GitHub PAT without touching the credential database."""
    from src.workspace.git_credentials import ResolvedGitAuth

    async def _resolve(user_id: str, repo_url: str):
        return ResolvedGitAuth(
            GitAuth(token="pat-token", provider="github", username=""),
            "github.com",
            "",
        )

    monkeypatch.setattr("src.workspace.git_credentials.resolve_auth_for_url", _resolve)


# ── The repository does not exist: create it ────────────────────────────────


async def test_absent_repository_is_created_private_and_scaffolded(
    provisioner, fake_provider, with_credential
):
    fake_provider.repo = "absent"

    provision = await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)

    assert provision.state is RemoteProvisionState.CREATED_EMPTY
    assert provision.created is True
    # A freshly created repository is empty, so the platform template is what
    # gives the workspace its first commit.
    assert provision.needs_scaffold is True
    # And git needs a root commit before a worktree can be attached.
    assert provision.needs_bootstrap_commit is True

    creates = [u for m, u in fake_provider.requests if m == "POST"]
    assert len(creates) == 1
    # The owner from the URL is an organization here, so the org endpoint is used.
    assert creates[0].endswith("/orgs/BiyoTech/repos")


async def test_creation_under_the_token_owner_uses_the_personal_endpoint(
    provisioner, fake_provider, with_credential
):
    fake_provider.repo = "absent"
    fake_provider.login = "loganhu"

    await provisioner.provision_remote(git_repo_url=_OWN_URL, user_id=_USER)

    creates = [u for m, u in fake_provider.requests if m == "POST"]
    assert creates[0].endswith("/user/repos")


async def test_created_url_is_the_one_the_user_typed(
    provisioner, fake_provider, with_credential
):
    """Swapping in the provider's clone_url would make the stored value differ."""
    fake_provider.repo = "absent"
    provision = await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)
    assert provision.normalized_url == _URL


# ── Strict owner: no silent fallback ───────────────────────────────────────


@pytest.mark.parametrize("status", [403, 404])
async def test_organization_without_permission_is_refused_not_redirected(
    provisioner, fake_provider, with_credential, status
):
    """The decision the user asked for: fail loudly, keep the address intact."""
    fake_provider.repo = "absent"
    fake_provider.create_status = status

    with pytest.raises(GitRemoteAccessError) as excinfo:
        await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)

    assert excinfo.value.detail["git_error_reason"] == (
        GitRemoteReason.REPO_CREATION_DENIED
    )
    # Exactly one attempt: no second try against the personal account.
    assert len([u for m, u in fake_provider.requests if m == "POST"]) == 1


async def test_rejected_token_is_reported_as_a_credential_problem(
    provisioner, fake_provider, with_credential
):
    fake_provider.repo = "absent"
    fake_provider.create_status = 401

    with pytest.raises(GitRemoteAccessError) as excinfo:
        await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)
    assert excinfo.value.detail["git_error_reason"] == (
        GitRemoteReason.INVALID_CREDENTIAL
    )


async def test_unresolvable_token_account_treats_owner_as_organization(
    provisioner, fake_provider, with_credential
):
    """Safer default: the provider adjudicates instead of us guessing."""
    fake_provider.repo = "absent"
    fake_provider.login = ""  # /user answers 401

    await provisioner.provision_remote(git_repo_url=_OWN_URL, user_id=_USER)

    creates = [u for m, u in fake_provider.requests if m == "POST"]
    assert creates[0].endswith("/orgs/loganhu/repos")


async def test_taken_name_is_reported_as_creation_failed(
    provisioner, fake_provider, with_credential
):
    fake_provider.repo = "absent"
    fake_provider.create_status = 422

    with pytest.raises(GitRemoteAccessError) as excinfo:
        await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)
    assert excinfo.value.detail["git_error_reason"] == (
        GitRemoteReason.REPO_CREATION_FAILED
    )
    # The provider's own human-readable sentence reaches the user.
    assert "not accessible" in excinfo.value.detail["git_detail"].lower()


# ── The repository exists: reuse it, never scaffold ────────────────────────


async def test_existing_repository_with_code_is_reused_without_scaffold(
    provisioner, fake_provider, with_credential
):
    """The core of the requirement: do not overwrite a real project."""
    fake_provider.repo = "with_branches"
    fake_provider.branches = ["main", "feature/x"]

    provision = await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)

    assert provision.state is RemoteProvisionState.REUSABLE
    assert provision.needs_scaffold is False
    # The existing history seeds the worktree; inventing a root commit would
    # fork it.
    assert provision.needs_bootstrap_commit is False
    assert provision.default_branch == "main"
    assert provision.created is False
    # Nothing was created.
    assert [u for m, u in fake_provider.requests if m == "POST"] == []


async def test_reuse_falls_back_to_the_first_branch_when_main_is_absent(
    provisioner, fake_provider, with_credential
):
    fake_provider.repo = "with_branches"
    fake_provider.branches = ["develop", "release"]

    provision = await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)
    assert provision.default_branch == "develop"


async def test_existing_but_branchless_repository_is_reused_empty(
    provisioner, fake_provider, with_credential
):
    """Chosen behaviour: no template, just an empty working copy."""
    fake_provider.repo = "empty"
    fake_provider.branches = []

    provision = await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)

    assert provision.state is RemoteProvisionState.REMOTE_EMPTY
    assert provision.needs_scaffold is False
    # There is no history at all, so a root commit is required to attach a
    # worktree — that commit stays EMPTY.
    assert provision.needs_bootstrap_commit is True
    assert provision.default_branch == ""
    assert [u for m, u in fake_provider.requests if m == "POST"] == []


# ── Never create on an ambiguous answer ───────────────────────────────────


async def test_unreadable_repository_does_not_lead_to_creation(
    provisioner, fake_provider, with_credential, monkeypatch
):
    """A rejected token must not be read as "the repository is missing".

    Creating here would produce a second repository at an address the user
    already owns — the one outcome that is worse than an error message.
    """
    async def _denied(repo_url, auth, server_url=""):
        from src.git.ops import RemoteAccessCheck

        return RemoteAccessCheck(
            ok=False, reason=GitRemoteReason.INVALID_CREDENTIAL, performed=True
        )

    monkeypatch.setattr("src.workspace.scaffold.check_remote_write_access", _denied)

    with pytest.raises(GitRemoteAccessError) as excinfo:
        await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)

    assert excinfo.value.detail["git_error_reason"] == (
        GitRemoteReason.INVALID_CREDENTIAL
    )
    assert [u for m, u in fake_provider.requests if m == "POST"] == []


async def test_unprobeable_provider_falls_back_to_ls_remote(
    provisioner, fake_provider, with_credential, monkeypatch
):
    """Self-hosted forges we cannot probe still get an existence verdict."""
    async def _not_performed(repo_url, auth, server_url=""):
        from src.git.ops import RemoteAccessCheck

        return RemoteAccessCheck(ok=True, performed=False, detail="not probeable")

    monkeypatch.setattr("src.workspace.scaffold.check_remote_write_access", _not_performed)
    fake_provider.repo = "with_branches"
    fake_provider.branches = ["main"]

    provision = await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)
    assert provision.state is RemoteProvisionState.REUSABLE


async def test_missing_credential_is_refused_before_any_probe(
    provisioner, fake_provider, monkeypatch
):
    """An anonymous probe cannot tell "private" from "absent"."""
    from src.workspace.git_credentials import ResolvedGitAuth

    async def _resolve(user_id: str, repo_url: str):
        return ResolvedGitAuth(None, "github.com", "")

    monkeypatch.setattr("src.workspace.git_credentials.resolve_auth_for_url", _resolve)

    with pytest.raises(GitRemoteAccessError) as excinfo:
        await provisioner.provision_remote(git_repo_url=_URL, user_id=_USER)

    assert excinfo.value.detail["git_error_reason"] == (
        GitRemoteReason.CREDENTIAL_MISSING
    )
    assert fake_provider.requests == []


async def test_invalid_url_is_rejected_by_the_egress_policy(
    provisioner, with_credential
):
    """SSRF policy still applies: provisioning does not bypass URL validation."""
    from src.utils.errors import GitRepoUrlInvalidError

    with pytest.raises(GitRepoUrlInvalidError):
        await provisioner.provision_remote(
            git_repo_url="https://10.0.0.1/org/repo.git", user_id=_USER
        )


# ── GitLab: namespace resolution must be exact ────────────────────────────


async def test_gitlab_requires_an_exact_namespace_match(
    provisioner, fake_provider, monkeypatch
):
    """A fuzzy search hit would create the project under the wrong owner."""
    from src.workspace.git_credentials import ResolvedGitAuth

    async def _resolve(user_id: str, repo_url: str):
        return ResolvedGitAuth(
            GitAuth(token="glpat", provider="gitlab", username=""),
            "gitlab.com",
            "https://gitlab.com",
        )

    monkeypatch.setattr("src.workspace.git_credentials.resolve_auth_for_url", _resolve)
    fake_provider.repo = "absent"
    fake_provider.login = "someone-else"
    # Only a near-miss is returned, so no id may be used.
    fake_provider.namespaces = [{"id": 7, "full_path": "acme-staging"}]

    with pytest.raises(GitRemoteAccessError) as excinfo:
        await provisioner.provision_remote(
            git_repo_url="https://gitlab.com/acme/shop.git", user_id=_USER
        )
    assert excinfo.value.detail["git_error_reason"] == (
        GitRemoteReason.REPO_CREATION_DENIED
    )
    assert [u for m, u in fake_provider.requests if m == "POST"] == []
