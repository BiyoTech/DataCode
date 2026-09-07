"""Resolution of a user's Git credential for one concrete remote.

A user may hold several credentials (GitHub PAT, self-hosted Gitea token, ...).
Picking the "default" one is wrong: pushing a github.com remote with a Gitea
token authenticates against the wrong host and fails in a way that is hard to
diagnose. Every remote operation therefore resolves the credential by the
remote's HOST, and reports "no credential for this host" as a first-class,
actionable error instead of falling through to a raw git failure.

Ported from the legacy sqlmesh-service (P3 merge); shared by the env-control
API (P5), the lease materializer and the Git REST endpoints so every layer
behaves identically.
"""

import typing as t

from src.git.ops import GitAuth, extract_git_host, run_git_async
from src.utils.logging import get_logger

logger = get_logger(__name__)


class ResolvedGitAuth(t.NamedTuple):
    """Credential material plus the context needed to build error messages."""

    auth: t.Optional[GitAuth]
    host: str
    server_url: str


async def resolve_auth_for_url(user_id: str, repo_url: str) -> ResolvedGitAuth:
    """Resolve the credential that authenticates against ``repo_url``'s host.

    Never raises and never falls back to an unrelated credential: when nothing
    matches, ``auth`` is None and the caller decides whether the operation can
    still proceed anonymously (public clone) or must fail (push).
    """
    host = extract_git_host(repo_url)
    if not host:
        logger.warning("git_auth_host_unresolved", user_id=user_id)
        return ResolvedGitAuth(None, "", "")

    try:
        from src.workspace.repository import UserGitCredentialRepository

        cred = await UserGitCredentialRepository().get_for_host(user_id, host)
    except Exception as exc:
        logger.warning(
            "git_credential_lookup_failed", user_id=user_id, host=host, error=str(exc)
        )
        return ResolvedGitAuth(None, host, "")

    if cred is None or not cred.token:
        logger.warning("git_credential_not_found_for_host", user_id=user_id, host=host)
        return ResolvedGitAuth(None, host, "")

    logger.info(
        "git_credential_resolved",
        user_id=user_id,
        host=host,
        provider=cred.provider,
        auth_type=cred.auth_type,
    )
    return ResolvedGitAuth(
        GitAuth(
            token=cred.token,
            provider=cred.provider or "",
            username=cred.username or "",
        ),
        host,
        cred.server_url or "",
    )


async def resolve_auth_for_workspace(
    user_id: str, workspace_path: str, remote: str = "origin"
) -> t.Tuple[ResolvedGitAuth, str]:
    """Resolve the credential for a workspace's configured ``remote``.

    Returns ``(resolved, remote_url)``; ``remote_url`` is empty when the
    workspace has no such remote, in which case the operation is purely local.
    """
    result = await run_git_async(workspace_path, ["remote", "get-url", remote])
    remote_url = result.stdout.strip() if result.returncode == 0 else ""
    if not remote_url:
        return ResolvedGitAuth(None, "", ""), ""
    return await resolve_auth_for_url(user_id, remote_url), remote_url
