"""Git provider table contract.

The provider table has three consumers that must agree: the control plane (which
computes a credential's host when it is created), the runtime (which recomputes
it on every remote operation to find the credential) and the frontend picker
(which previews the host to the user before saving).

A disagreement produces no error anywhere. It produces a credential that was
saved against one host and looked up under another, i.e. a repository the user
just configured for which "no credential is found". That is what happened while
the runtime's copy lacked ``gitea`` and the picker offered Gitea: the credential
was stored against ``gitea.com`` and matched against ``""``.

The frontend cannot import Python, so its copy is asserted here — the same way
``lease-vectors.json`` pins the lease algebra across the two languages.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from datacode_common.git_providers import (
    DEFAULT_TOKEN_USERNAME,
    PROVIDER_DEFAULT_HOSTS,
    PROVIDER_TOKEN_USERNAMES,
    credential_host,
    extract_git_host,
    token_username,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PICKER = (
    REPO_ROOT / "frontend" / "src" / "components" / "settings" / "AddGitCredentialModal.vue"
)


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


def test_every_offered_provider_resolves_to_a_host() -> None:
    """A provider the UI offers must resolve, or its credentials never match."""
    for provider in ("github", "gitlab", "bitbucket", "gitea"):
        assert credential_host(provider) != "", f"{provider} resolves to no host"


def test_provider_lookup_is_case_insensitive() -> None:
    assert credential_host("GitHub") == "github.com"
    assert token_username("GITEA") == "oauth2"


def test_unknown_provider_yields_no_host_rather_than_a_guess() -> None:
    """Self-hosted forges are reached by explicit URL; guessing would mismatch."""
    assert credential_host("my-forge") == ""
    assert credential_host("") == ""


def test_token_username_falls_back_for_self_hosted_providers() -> None:
    assert token_username("my-forge") == DEFAULT_TOKEN_USERNAME
    for provider in PROVIDER_DEFAULT_HOSTS:
        # Every listed provider should have a deliberate username, not the
        # fallback by accident.
        assert provider in PROVIDER_TOKEN_USERNAMES


# ---------------------------------------------------------------------------
# Host extraction: every form a stored server_url can take
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://git.example.com/org/repo.git", "git.example.com"),
        # Self-hosted dev instances are plain http.
        ("http://gitea.internal:3000/org/repo.git", "gitea.internal"),
        # A bare host, as typed into the dialog.
        ("git.example.com", ""),
        # scp-like syntax, which the control plane's old copy could not parse.
        ("git@git.example.com:org/repo.git", "git.example.com"),
        ("ssh://git@git.example.com:22/org/repo.git", "git.example.com"),
        ("", ""),
        ("not a url", ""),
    ],
)
def test_extract_git_host(url: str, expected: str) -> None:
    assert extract_git_host(url) == expected


def test_explicit_server_url_wins_over_the_provider_default() -> None:
    """Self-hosted Gitea/GitLab and GitHub Enterprise depend on this."""
    assert credential_host("github", "https://github.acme.internal") == "github.acme.internal"
    assert credential_host("gitea", "https://gitea.internal:3000") == "gitea.internal"


def test_unparseable_server_url_falls_back_to_the_provider_host() -> None:
    """Better the public host than no host: no host matches no remote at all."""
    assert credential_host("github", "not a url") == "github.com"


# ---------------------------------------------------------------------------
# Cross-language agreement
# ---------------------------------------------------------------------------


def _picker_table() -> dict[str, str]:
    """Parse PROVIDER_DEFAULT_HOSTS out of the Vue component."""
    source = PICKER.read_text(encoding="utf-8")
    block = re.search(
        r"const PROVIDER_DEFAULT_HOSTS:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}",
        source,
        re.DOTALL,
    )
    assert block, "PROVIDER_DEFAULT_HOSTS not found in the credential dialog"
    return {
        key: value
        for key, value in re.findall(r"(\w+):\s*'([^']+)'", block.group(1))
    }


def _picker_offered_providers() -> set[str]:
    """Parse the provider dropdown options out of the Vue component."""
    source = PICKER.read_text(encoding="utf-8")
    block = re.search(
        r"const providers:\s*\{[^}]*\}\[\]\s*=\s*\[(.*?)\]", source, re.DOTALL
    )
    assert block, "provider options not found in the credential dialog"
    return set(re.findall(r"value:\s*'([^']+)'", block.group(1)))


def test_frontend_table_matches_the_backend() -> None:
    assert _picker_table() == PROVIDER_DEFAULT_HOSTS


def test_every_provider_the_ui_offers_is_known_to_the_backend() -> None:
    """The defect this catches: the picker offered Gitea, the backend did not."""
    offered = _picker_offered_providers()
    # "custom" means "I will type a server URL", so it needs no default host.
    resolvable = {name for name in offered if name != "custom"}
    missing = sorted(resolvable - set(PROVIDER_DEFAULT_HOSTS))
    assert not missing, f"offered by the UI but unknown to the backend: {missing}"


def test_single_source_of_truth_no_service_local_copy() -> None:
    """Structural guard: a second copy is how the tables drifted apart."""
    offenders: list[str] = []
    for service in ("control-service", "workspace-runtime", "agent-service"):
        for path in sorted((REPO_ROOT / service / "src").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"^_?PROVIDER_DEFAULT_HOSTS\s*[:=]", source, re.M):
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{service}/{path.relative_to(REPO_ROOT / service)}:{line}")
    assert not offenders, (
        "import the table from datacode_common.git_providers: " + "; ".join(offenders)
    )
