"""Git provider facts shared by the control plane and the runtime.

Both services answer the same question — "which host does this stored credential
authenticate against?" — and they must answer it IDENTICALLY: the control plane
computes the host when a credential is created, the runtime recomputes it when a
remote operation looks a credential up. A disagreement does not surface as an
error, it surfaces as a credential that cannot be found for a repository the user
just configured.

That is exactly what happened before this module existed. Two hand-maintained
copies of the provider table drifted: the runtime's lacked ``gitea`` while the
frontend offered Gitea as a choice and the control plane accepted it, so a Gitea
credential created without an explicit server URL was stored against
``gitea.com`` and then matched against ``""`` on push.

The frontend keeps its own copy (no language boundary can be crossed here), and a
test asserts the two agree — the same approach ``shared/lease-vectors.json`` uses
for the lease algebra.
"""

from __future__ import annotations

from urllib.parse import urlparse

__all__ = [
    "PROVIDER_DEFAULT_HOSTS",
    "PROVIDER_TOKEN_USERNAMES",
    "DEFAULT_TOKEN_USERNAME",
    "extract_git_host",
    "credential_host",
    "token_username",
]


# Public host a provider serves when a credential carries no explicit
# ``server_url``. Used to match a stored credential against a remote URL, so a
# GitHub remote is never pushed with a Gitea token.
#
# Adding a provider means adding it HERE and in the frontend's picker; the
# contract test fails when only one side is updated.
PROVIDER_DEFAULT_HOSTS: dict[str, str] = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "bitbucket": "bitbucket.org",
    "gitea": "gitea.com",
}

# HTTPS basic-auth username to pair with a token. The password slot always
# carries the token; providers only differ in what they expect (or ignore) in
# the username slot.
PROVIDER_TOKEN_USERNAMES: dict[str, str] = {
    "github": "x-access-token",
    "gitlab": "oauth2",
    "gitea": "oauth2",
    "bitbucket": "x-token-auth",
}

# Providers outside the table above are self-hosted forges reached by an explicit
# server URL; "oauth2" is what the common ones accept.
DEFAULT_TOKEN_USERNAME = "oauth2"


def extract_git_host(url: str) -> str:
    """Best-effort lowercase host of any Git URL form. Never raises.

    Tolerates every scheme (including the plain http of self-hosted dev
    instances) and the scp-like ``git@host:org/repo.git`` syntax, because this
    feeds credential matching rather than the egress policy — a URL this cannot
    parse must yield "no host" instead of an exception, so the caller falls back
    to the provider's public host.
    """
    candidate = (url or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        # scp-like syntax: git@host:org/repo.git
        if "@" in candidate:
            return candidate.split("@", 1)[1].split(":", 1)[0].strip("[]").lower()
        return ""
    try:
        return (urlparse(candidate).hostname or "").lower()
    except ValueError:
        return ""


def credential_host(provider: str, server_url: str = "") -> str:
    """Host a stored credential authenticates against.

    An explicit ``server_url`` always wins (self-hosted Gitea/GitLab, GitHub
    Enterprise); otherwise the provider's public host is used. Returns ``""``
    when neither is known, which callers treat as "matches no remote".
    """
    if server_url:
        host = extract_git_host(server_url)
        if host:
            return host
    return PROVIDER_DEFAULT_HOSTS.get((provider or "").lower(), "")


def token_username(provider: str) -> str:
    """Basic-auth username to pair with a personal access token."""
    return PROVIDER_TOKEN_USERNAMES.get((provider or "").lower(), DEFAULT_TOKEN_USERNAME)
