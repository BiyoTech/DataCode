"""Git credential endpoints (P5 ownership move).

Credentials are CONTROL-PLANE data: they are user account material, not
working-copy state. The runtime keeps a READ path for remote operations
(pushing, fetching); every create/update/delete flows through here.

Authorization contract — a credential is a USER-SCOPED resource:

* every endpoint requires a verified identity and nothing more. The gateway is
  the only way in: it rewrites the identity headers from the access token and
  overwrites any ``user_id`` a client puts in the query string or body, so
  ``caller.user_id`` cannot be forged;
* ownership, not a role, is the boundary. Reads list only the caller's rows and
  writes resolve the credential by ``(credential_id, caller.user_id)``, so one
  user can never touch another's token even inside the same tenant;
* the permission boundary sits on USING a credential, not on managing one:
  pushing and publishing require ``workspace:git:push`` and merging into the
  production branch additionally requires ``workspace:env:promote``. Managing
  one's own personal access token is not a team-level decision.

This mirrors the gateway's route map, which classifies ``/git/credentials*`` as
authenticated-only. The two layers previously disagreed: this module asserted
``git:credential:write``, a code absent from the permission catalog, so it could
not be granted through any role, could not be checked in the role editor and was
rejected by identity-service if requested through the API — making every
credential write a permanent 403 for every account including admin.
"""

import uuid

from fastapi import APIRouter, HTTPException

from src.api.deps import Caller
from src.api.models.requests import (
    CreateGitCredentialRequest,
    UpdateGitCredentialRequest,
)
from src.utils.errors import InvalidRequestError
from src.utils.logging import get_logger
from src.workspace.repository import credential_host

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/git", tags=["git-credentials"])

# Providers whose OAuth flow is foreseen but not yet enabled.
OAUTH_CAPABLE_PROVIDERS = {"github"}


@router.get("/credentials")
async def list_git_credentials(caller: Caller):
    """List all Git credentials of the caller."""
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    try:
        creds = await repo.list_by_user(caller.user_id)
        return {
            "credentials": [
                {
                    "id": c.id,
                    "provider": c.provider,
                    "nickname": c.nickname,
                    "auth_type": c.auth_type,
                    "email": c.email or "",
                    "username": c.username or "",
                    "server_url": c.server_url or "",
                    # Host the credential authenticates against. Remote
                    # operations match on this, so the UI surfaces it too.
                    "host": credential_host(c.provider, c.server_url or ""),
                    "is_default": c.is_default == 1,
                    "oauth_account": c.oauth_account or "",
                    "created_at": c.created_at.isoformat() if c.created_at else "",
                }
                for c in creds
            ]
        }
    except Exception as e:
        logger.error("list_git_credentials_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to list credentials: {e}")


@router.post("/credentials")
async def create_git_credential(req: CreateGitCredentialRequest, caller: Caller):
    """Create a Git credential for the caller.

    Only personal access tokens can be created here. Account linking (OAuth) is
    reserved for GitHub and, until that flow is enabled, is rejected instead of
    silently persisting a token-less row that would break every Git operation.

    At most ONE credential per host is allowed: remote operations resolve the
    credential by the remote's host, so two credentials for the same host would
    make the choice ambiguous.
    """
    from src.workspace.repository import UserGitCredentialRepository

    provider = (req.provider or "").strip().lower()
    auth_type = (req.auth_type or "pat").strip().lower()
    if not provider:
        raise InvalidRequestError("Provider is required")

    if auth_type == "oauth":
        if provider not in OAUTH_CAPABLE_PROVIDERS:
            raise InvalidRequestError(
                f"Provider '{provider}' supports personal access tokens only"
            )
        raise HTTPException(
            status_code=501,
            detail=(
                "Linking a GitHub account is not available yet. "
                "Use a personal access token for now."
            ),
        )
    if auth_type != "pat":
        raise InvalidRequestError(f"Unsupported auth_type: {auth_type}")

    if not (req.token or "").strip():
        raise InvalidRequestError("Token is required")
    if not (req.server_url or "").strip():
        raise InvalidRequestError("Server URL is required")

    host = credential_host(provider, req.server_url)
    if not host:
        raise InvalidRequestError("Server URL must contain a valid host")

    repo = UserGitCredentialRepository()
    try:
        existing = await repo.get_for_host(caller.user_id, host)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A credential for {host} already exists "
                    f"('{existing.nickname}'). Edit that one instead."
                ),
            )

        credential_id = str(uuid.uuid4())
        nickname = req.nickname or f"{provider.capitalize()} ({host})"

        cred = await repo.create(
            credential_id=credential_id,
            user_id=caller.user_id,
            provider=provider,
            nickname=nickname,
            auth_type=auth_type,
            token=req.token.strip(),
            email=req.email,
            username=req.username,
            server_url=req.server_url.strip(),
        )
        logger.info(
            "git_credential_created",
            credential_id=cred.id,
            provider=provider,
            host=host,
        )
        return {
            "success": True,
            "credential": {
                "id": cred.id,
                "provider": cred.provider,
                "nickname": cred.nickname,
                "auth_type": cred.auth_type,
                "is_default": cred.is_default == 1,
                "created_at": cred.created_at.isoformat() if cred.created_at else "",
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_git_credential_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to create credential: {e}")


@router.put("/credentials/{credential_id}")
async def update_git_credential(
    credential_id: str,
    req: UpdateGitCredentialRequest,
    caller: Caller,
):
    """Update one of the caller's Git credentials."""
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    current = await repo.get_by_id(credential_id)
    if not current or current.user_id != caller.user_id:
        raise HTTPException(status_code=404, detail="Credential not found")

    updates = {}
    if req.nickname:
        updates["nickname"] = req.nickname
    if req.token:
        updates["token"] = req.token.strip()
    if req.email:
        updates["email"] = req.email
    if req.username:
        updates["username"] = req.username
    if req.server_url:
        # Moving a credential to another host must not create a second
        # credential for that host, which would make resolution ambiguous.
        host = credential_host(current.provider, req.server_url)
        if not host:
            raise InvalidRequestError("Server URL must contain a valid host")
        clash = await repo.get_for_host(current.user_id, host)
        if clash is not None and clash.id != credential_id:
            raise HTTPException(
                status_code=409,
                detail=f"A credential for {host} already exists ('{clash.nickname}').",
            )
        updates["server_url"] = req.server_url.strip()

    if req.is_default is True:
        await repo.set_default(current.user_id, credential_id)

    if not updates and req.is_default is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    if updates:
        success = await repo.update(credential_id, **updates)
        if not success:
            raise HTTPException(status_code=404, detail="Credential not found")
    return {"success": True}


@router.patch("/credentials/{credential_id}/default")
async def set_default_git_credential(credential_id: str, caller: Caller):
    """Set one of the caller's credentials as their default."""
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    success = await repo.set_default(caller.user_id, credential_id)
    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"success": True}


@router.delete("/credentials/{credential_id}")
async def delete_git_credential(credential_id: str, caller: Caller):
    """Delete one of the caller's Git credentials."""
    from src.workspace.repository import UserGitCredentialRepository

    repo = UserGitCredentialRepository()
    success = await repo.delete(credential_id, caller.user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")
    logger.info("git_credential_deleted", credential_id=credential_id)
    return {"success": True}
