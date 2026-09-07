"""Per-task credentials for a sandbox (design 04 §3.5, P6b).

What a sandbox is handed, and what it is not:

============================  ===========================================
Handed to the sandbox         Never handed to the sandbox
============================  ===========================================
its own task token            the token SIGNING secret
the egress proxy address      any platform database credential
its own working copy path     any other tenant's anything
============================  ===========================================

The warehouse credential is a special case worth stating plainly. It lives in the
working copy's own ``config.yaml`` — the user put it there — so the sandbox can
read it by reading its own project, and pretending otherwise would be theatre. Q5
accepted this, with two constraints that ARE enforced:

1.  Only the working copy of THIS task is mounted, so only that project's
    credential is reachable (``T1`` is closed by the mount, not by a check).
2.  A writable warehouse account may only appear in an SQL-only profile. L3
    profiles run user code and therefore get a read-only account; the writing half
    of an ``apply`` is split out to L2. This module refuses to assemble an
    environment that violates that, so the constraint cannot be lost in a future
    call site.

Everything here produces a plain environment mapping. Turning it into a Kubernetes
Secret with ``emptyDir: medium: Memory``, or into a subprocess environment, is the
backend's job — this module decides WHAT the sandbox gets, not how it arrives.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Mapping, Optional

from datacode_common.database.url import safe_url
from datacode_common.isolation import IsolationProfile
from datacode_common.security.task_token import (
    TASK_TOKEN_ENV,
    TaskTokenClaims,
    TaskTokenError,
    mint_task_token,
    task_token_secret,
)
from datacode_common.task_protocol import TaskSpec

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Standard proxy variables. Understood by httpx/requests/urllib and by the engine
# adapters SQLMesh uses, so the sandbox needs no DataCode-specific plumbing to
# route its traffic through the only path that is open to it.
ENV_HTTP_PROXY = "HTTP_PROXY"
ENV_HTTPS_PROXY = "HTTPS_PROXY"
ENV_NO_PROXY = "NO_PROXY"

# Address of the egress proxy, exposed so a task's own diagnostics can name it.
ENV_EGRESS_PROXY = "DATACODE_EGRESS_PROXY"

# Credential grades a profile may declare for the warehouse.
WAREHOUSE_NONE = "none"
WAREHOUSE_READONLY = "scoped_readonly"
WAREHOUSE_READWRITE = "scoped_readwrite"


class CredentialPolicyError(RuntimeError):
    """A task's credential grade contradicts its isolation profile.

    Always a programming or configuration error, never user input — which is why it
    is raised rather than reported: the alternative is a writable warehouse account
    inside a profile that runs arbitrary user code.
    """


@dataclass(frozen=True)
class SandboxCredentials:
    """The environment a sandbox receives, plus what was decided about it.

    Attributes:
        env: Variables to inject. Values may be secret; use :meth:`safe_summary`
            for anything that gets logged.
        task_token: The minted token, also present in ``env``. Exposed separately
            so a backend can place it in a Secret rather than a ConfigMap.
        warehouse_grade: Credential grade the profile declared.
        expires_at: Unix seconds (UTC) when the token stops being valid.
    """

    env: Mapping[str, str] = field(default_factory=dict)
    task_token: str = ""
    warehouse_grade: str = WAREHOUSE_NONE
    expires_at: int = 0

    def safe_summary(self) -> dict[str, object]:
        """A loggable description: names of variables, never their values.

        The proxy URL is masked even though it carries no password today, because
        it is a URL and a future authenticated proxy would put one there.
        """
        return {
            "env_keys": sorted(self.env),
            "warehouse_grade": self.warehouse_grade,
            "expires_at": self.expires_at,
            "egress_proxy": safe_url(self.env.get(ENV_EGRESS_PROXY, "")),
            "task_token_present": bool(self.task_token),
        }


def warehouse_grade_of(profile: IsolationProfile) -> str:
    """Credential grade a profile declares for the warehouse.

    Defaults to :data:`WAREHOUSE_NONE`: a profile that says nothing gets nothing.
    The alternative default — inheriting whatever the caller wanted — is how a new
    profile silently acquires write access.
    """
    credentials = profile.settings.get("credentials") or {}
    if not isinstance(credentials, Mapping):
        return WAREHOUSE_NONE
    return str(credentials.get("warehouse", WAREHOUSE_NONE) or WAREHOUSE_NONE)


def assert_write_grade_allowed(profile: IsolationProfile) -> None:
    """Enforce the Q5 constraint on writable warehouse accounts.

    A writable account may only be granted where NO host-language code runs. The
    profile itself declares which task kinds it accepts, and an L3-style profile
    accepting ``agent.shell`` or ``*_with_python`` kinds is exactly the case that
    must not also hold write access.

    Raises:
        CredentialPolicyError: The profile grants write access while accepting a
            kind that executes user code.
    """
    if warehouse_grade_of(profile) != WAREHOUSE_READWRITE:
        return

    code_kinds = sorted(
        kind
        for kind in profile.allowed_task_kinds
        if kind.startswith("agent.")
        or "python" in kind
        or "macro" in kind
        or kind in {"dagster.run", "dagster.codelocation", "sqlmesh.evaluate"}
    )
    if code_kinds:
        raise CredentialPolicyError(
            f"profile {profile.name!r} declares a writable warehouse account "
            f"({WAREHOUSE_READWRITE}) while accepting task kinds that execute "
            f"user code ({', '.join(code_kinds)}). A writable account must stay in "
            "an SQL-only profile (design 04 §3.5, Q5): route the write half of the "
            "work to an SQL-only kind instead."
        )


def build_sandbox_credentials(
    spec: TaskSpec,
    profile: IsolationProfile,
    *,
    secret: str = "",
    now: Optional[int] = None,
) -> SandboxCredentials:
    """Assemble what one sandbox is allowed to hold.

    Args:
        spec: The task. Supplies the scope the token is bound to.
        profile: Applied isolation profile. Supplies the credential grade.
        secret: Token signing secret; defaults to the environment.
        now: Injectable clock for tests.

    Returns:
        The environment plus the decisions made about it.

    Raises:
        CredentialPolicyError: The profile's grade contradicts what it runs, or a
            token is required and cannot be minted. Both prevent the task from
            starting, which is the correct outcome: a sandbox with no token has no
            egress at all, and discovering that as a connection failure later would
            misplace the cause.
    """
    assert_write_grade_allowed(profile)

    issued = int(now if now is not None else time.time())
    # The token dies with the task, plus the same margin the grant uses so a final
    # flush landing on the deadline is not refused.
    expires = issued + spec.timeout_seconds + _TOKEN_MARGIN_SECONDS

    try:
        token = mint_task_token(
            TaskTokenClaims(
                task_id=spec.task_id,
                tenant_id=spec.tenant_id,
                workspace_id=spec.workspace_id,
                workspace_path=spec.workspace_path,
                isolation_profile=profile.name,
                actor_user_id=spec.actor_user_id,
                issued_at=issued,
                expires_at=expires,
            ),
            secret=secret or task_token_secret(),
        )
    except TaskTokenError as exc:
        raise CredentialPolicyError(
            f"cannot issue a task token for {spec.task_id}: {exc}. The sandbox "
            "would have no way to authenticate to the egress proxy and therefore "
            "no network access at all."
        ) from exc

    env: dict[str, str] = {TASK_TOKEN_ENV: token}

    proxy_url = _proxy_url(token)
    if proxy_url:
        env[ENV_EGRESS_PROXY] = settings.egress_proxy_url
        env[ENV_HTTP_PROXY] = proxy_url
        env[ENV_HTTPS_PROXY] = proxy_url
        env[ENV_HTTP_PROXY.lower()] = proxy_url
        env[ENV_HTTPS_PROXY.lower()] = proxy_url
        # Nothing is exempt from the proxy except the loopback a task uses to talk
        # to its own sidecars. Listing a platform host here would create a hole
        # that bypasses every rule the proxy applies.
        env[ENV_NO_PROXY] = "localhost,127.0.0.1"
        env[ENV_NO_PROXY.lower()] = env[ENV_NO_PROXY]

    credentials = SandboxCredentials(
        env=env,
        task_token=token,
        warehouse_grade=warehouse_grade_of(profile),
        expires_at=expires,
    )
    logger.info(
        "sandbox_credentials_built",
        task_id=spec.task_id,
        profile=profile.name,
        **credentials.safe_summary(),
    )
    return credentials


# Grace period on top of the task's timeout, matching the egress grant's margin.
_TOKEN_MARGIN_SECONDS = 120


def _proxy_url(token: str) -> str:
    """Build the proxy URL a sandbox's HTTP clients should use.

    The token is embedded as the userinfo half, because that is the only way a
    library configured through ``HTTPS_PROXY`` can authenticate to a CONNECT proxy.
    NEVER log this value un-masked — it contains the token.
    """
    base = (settings.egress_proxy_url or "").strip()
    if not base:
        return ""
    if "://" not in base:
        base = f"http://{base}"
    scheme, _, authority = base.partition("://")
    if "@" in authority:
        # A pre-authenticated URL in configuration would be a second credential
        # nobody rotates; the task's own token is the only one that belongs here.
        authority = authority.rpartition("@")[2]
    return f"{scheme}://{token}@{authority}"


__all__ = [
    "ENV_EGRESS_PROXY",
    "ENV_HTTPS_PROXY",
    "ENV_HTTP_PROXY",
    "ENV_NO_PROXY",
    "WAREHOUSE_NONE",
    "WAREHOUSE_READONLY",
    "WAREHOUSE_READWRITE",
    "CredentialPolicyError",
    "SandboxCredentials",
    "assert_write_grade_allowed",
    "build_sandbox_credentials",
    "warehouse_grade_of",
]
