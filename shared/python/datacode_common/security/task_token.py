"""Scoped task tokens — one task's proof of identity (design 04 §3.5).

A sandbox needs to prove who it is to the egress proxy (and, later, to the
warehouse proxy sidecar) without holding a platform credential. This module mints
and verifies that proof.

Why not the internal shared token
---------------------------------
``security.internal_token`` authenticates a SERVICE and carries no identity. A
sandbox is the opposite case: it is untrusted, and what matters is exactly which
tenant, workspace and task it is. Handing it the internal secret would give user
code the platform's own service credential — the very thing L3 exists to prevent.

Design
------
A token is ``payload.signature``, where the payload is the claim set and the
signature is HMAC-SHA256 over it with a platform secret. Three consequences, all
deliberate:

* **Offline verification.** The proxy validates a token without a round trip to
  the runtime, so an egress decision does not depend on the runtime being up, and
  a task cannot be slowed down (or stalled) by that dependency.
* **Self-scoping.** Tenant, workspace, task id and the single working copy are
  IN the signed payload. A token stolen from one sandbox authorizes nothing
  outside that sandbox's scope, which is what makes leaking one survivable.
* **Naturally short-lived.** ``expires_at`` is the task's own deadline. There is
  no revocation list because there is nothing to revoke: the token dies with the
  task it was minted for.

The secret is env-only and is NEVER injected into a sandbox. A task receives its
token, never the key that signs tokens — otherwise it could mint one for another
tenant.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Optional

# Environment variable holding the signing secret, on the MINTING side (runtime)
# and the VERIFYING side (egress-proxy) alike. Both must read the same value; a
# mismatch denies every request, which is the correct failure direction.
TASK_TOKEN_SECRET_ENV = "DATACODE_TASK_TOKEN_SECRET"

# Environment variable the token is delivered to a sandbox through.
TASK_TOKEN_ENV = "DATACODE_TASK_TOKEN"

# Header the sandbox presents it on. Not ``Authorization``: that one carries the
# END USER's credential elsewhere in the platform, and reusing it would make a
# log scrubber or proxy treat the two as interchangeable.
TASK_TOKEN_HEADER = "X-Datacode-Task-Token"

# Proxy-Authorization is what an HTTP CONNECT client can actually be configured
# to send (``https_proxy=http://token@proxy:3128``), so the proxy accepts it too.
PROXY_AUTH_HEADER = "Proxy-Authorization"

# Version prefix in the payload. A verifier refuses a version it does not
# implement rather than ignoring unknown fields — an ignored field could be the
# one narrowing the scope.
TOKEN_VERSION = 1

# Clock skew tolerated when checking expiry. Small on purpose: the whole point of
# these tokens is that they die quickly.
CLOCK_SKEW_SECONDS = 30


# Characters a token can consist of: URL-safe base64 plus the single separator.
# Used to reject junk at EXTRACTION time. Not a security control — a forged token
# fails the signature check anyway — but it keeps a mangled header from being
# logged and reported as "invalid signature", which would send an operator
# looking for a key mismatch that does not exist.
_TOKEN_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_."
)


class TaskTokenError(ValueError):
    """A token is malformed, unsigned, expired or out of scope."""


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 without padding, so a token is header- and URL-safe."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise TaskTokenError(f"token payload is not valid base64: {exc}") from exc


@dataclass(frozen=True)
class TaskTokenClaims:
    """What a task token asserts.

    Attributes:
        task_id: The one task this token belongs to.
        tenant_id: Owning tenant. The egress proxy resolves the allow-list from
            this, so it must be signed — a sandbox that could edit it would
            inherit another tenant's destinations.
        workspace_id: Owning workspace.
        workspace_path: The single working copy the task may act on. Present so a
            future warehouse-proxy sidecar can bind a connection to one copy
            without trusting a request parameter.
        isolation_profile: Profile actually applied. Lets a verifier apply
            profile-specific rules (an L3 token must not be granted the writable
            warehouse account) without looking anything up.
        actor_user_id: The user the task is attributed to, for audit.
        issued_at: Unix seconds (UTC) when minted.
        expires_at: Unix seconds (UTC) when it stops being valid — the task's own
            deadline.
    """

    task_id: str
    tenant_id: str
    workspace_id: str = ""
    workspace_path: str = ""
    isolation_profile: str = ""
    actor_user_id: str = ""
    issued_at: int = 0
    expires_at: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "v": TOKEN_VERSION,
            "tid": self.task_id,
            "ten": self.tenant_id,
            "ws": self.workspace_id,
            "wsp": self.workspace_path,
            "prof": self.isolation_profile,
            "usr": self.actor_user_id,
            "iat": self.issued_at,
            "exp": self.expires_at,
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "TaskTokenClaims":
        version = data.get("v")
        if version != TOKEN_VERSION:
            raise TaskTokenError(
                f"unsupported task token version {version!r}; "
                f"this build implements {TOKEN_VERSION}"
            )
        task_id = str(data.get("tid") or "")
        tenant_id = str(data.get("ten") or "")
        if not task_id or not tenant_id:
            # A token without a scope authorizes nothing, so it must be rejected
            # rather than treated as a wildcard.
            raise TaskTokenError("task token must carry both a task id and a tenant id")
        return cls(
            task_id=task_id,
            tenant_id=tenant_id,
            workspace_id=str(data.get("ws") or ""),
            workspace_path=str(data.get("wsp") or ""),
            isolation_profile=str(data.get("prof") or ""),
            actor_user_id=str(data.get("usr") or ""),
            issued_at=int(data.get("iat") or 0),
            expires_at=int(data.get("exp") or 0),
        )

    def audit_fields(self) -> dict[str, str]:
        """Identity fields to attach to an audit line. Never the token itself."""
        return {
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "isolation_profile": self.isolation_profile,
            "actor_user_id": self.actor_user_id,
        }


def task_token_secret(secret: str = "") -> str:
    """Resolve the signing secret: explicit argument, else the environment.

    Returns an empty string when unconfigured. Callers must treat that as "this
    feature is off" — :func:`mint_task_token` refuses to sign and
    :func:`verify_task_token` refuses to verify, so an unset secret can never
    silently produce an unauthenticated allow.
    """
    return secret or os.getenv(TASK_TOKEN_SECRET_ENV, "")


def _sign(payload: str, secret: str) -> str:
    return _b64encode(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), sha256).digest())


def mint_task_token(
    claims: TaskTokenClaims,
    *,
    secret: str = "",
    ttl_seconds: int = 0,
    now: Optional[int] = None,
) -> str:
    """Sign a token for one task.

    Args:
        claims: The scope to assert. ``issued_at`` / ``expires_at`` are filled in
            when unset.
        secret: Signing secret; defaults to the environment.
        ttl_seconds: Lifetime, used only when ``claims.expires_at`` is unset.
            Should be the task's own timeout: a token outliving its task is a
            credential nothing is watching any more.
        now: Injectable clock for tests.

    Raises:
        TaskTokenError: No secret configured, or the claims cannot be signed.
    """
    key = task_token_secret(secret)
    if not key:
        raise TaskTokenError(
            f"cannot mint a task token: {TASK_TOKEN_SECRET_ENV} is not set"
        )
    if ttl_seconds < 0:
        raise TaskTokenError("ttl_seconds must not be negative")

    issued = claims.issued_at or int(now if now is not None else time.time())
    expires = claims.expires_at or (issued + ttl_seconds if ttl_seconds else 0)
    if not expires:
        # A non-expiring sandbox credential is exactly what this design avoids.
        raise TaskTokenError(
            "a task token must expire: pass ttl_seconds or set claims.expires_at"
        )

    scoped = TaskTokenClaims(
        task_id=claims.task_id,
        tenant_id=claims.tenant_id,
        workspace_id=claims.workspace_id,
        workspace_path=claims.workspace_path,
        isolation_profile=claims.isolation_profile,
        actor_user_id=claims.actor_user_id,
        issued_at=issued,
        expires_at=expires,
    )
    if not scoped.task_id or not scoped.tenant_id:
        raise TaskTokenError("task token requires both task_id and tenant_id")

    payload = _b64encode(
        json.dumps(scoped.to_payload(), separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    return f"{payload}.{_sign(payload, key)}"


def verify_task_token(
    token: str,
    *,
    secret: str = "",
    now: Optional[int] = None,
) -> TaskTokenClaims:
    """Verify a token and return its claims.

    Signature is checked BEFORE the payload is interpreted: parsing attacker-
    controlled JSON and then deciding whether to trust it inverts the order that
    makes the check meaningful.

    Raises:
        TaskTokenError: Missing secret, malformed token, bad signature, or expired.
    """
    key = task_token_secret(secret)
    if not key:
        raise TaskTokenError(
            f"cannot verify a task token: {TASK_TOKEN_SECRET_ENV} is not set"
        )

    payload, separator, signature = (token or "").strip().partition(".")
    if not separator or not payload or not signature:
        raise TaskTokenError("task token must look like '<payload>.<signature>'")

    if not hmac.compare_digest(_sign(payload, key), signature):
        # One message for every signature failure: distinguishing "wrong key"
        # from "tampered payload" tells an attacker which half to work on.
        raise TaskTokenError("task token signature is invalid")

    try:
        data = json.loads(_b64decode(payload))
    except ValueError as exc:
        raise TaskTokenError(f"task token payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TaskTokenError("task token payload must be a JSON object")

    claims = TaskTokenClaims.from_payload(data)
    current = int(now if now is not None else time.time())
    if claims.expires_at and current > claims.expires_at + CLOCK_SKEW_SECONDS:
        raise TaskTokenError(
            f"task token for {claims.task_id} expired at {claims.expires_at}"
        )
    if claims.issued_at and current + CLOCK_SKEW_SECONDS < claims.issued_at:
        # A future-dated token means the two clocks disagree by more than the
        # tolerance, or the token was crafted; either way it must not be honoured.
        raise TaskTokenError(
            f"task token for {claims.task_id} is not valid yet "
            f"(issued at {claims.issued_at})"
        )
    return claims


def extract_task_token(
    *, task_header: str = "", proxy_authorization: str = ""
) -> str:
    """Pull the token out of whichever header carried it.

    ``Proxy-Authorization`` is accepted because that is what a client configured
    via ``https_proxy=http://<token>@host:port`` actually sends, and SQLMesh's
    engine adapters are configured exactly that way. Both ``Basic`` (token as the
    username) and ``Bearer`` forms are understood.

    Returns an empty string for anything that cannot be a token, so the caller
    reports "no credential presented" instead of "invalid signature" — the two
    send an operator to different places.
    """
    if task_header.strip():
        return _clean(task_header.strip())

    candidate = (proxy_authorization or "").strip()
    if not candidate:
        return ""
    scheme, _, value = candidate.partition(" ")
    scheme = scheme.lower()
    value = value.strip()
    if scheme == "bearer":
        return _clean(value)
    if scheme == "basic":
        try:
            decoded = base64.b64decode(
                value + "=" * (-len(value) % 4), validate=True
            ).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""
        # "token:" or "token:ignored" — the password half is unused.
        return _clean(decoded.split(":", 1)[0].strip())
    return ""


def _clean(value: str) -> str:
    """Return ``value`` if it could be a token at all, else an empty string."""
    if value and set(value) <= _TOKEN_ALPHABET:
        return value
    return ""


def task_token_headers(token: str) -> dict[str, str]:
    """Headers a sandbox must send. Empty when it holds no token."""
    return {TASK_TOKEN_HEADER: token} if token else {}


__all__ = [
    "CLOCK_SKEW_SECONDS",
    "PROXY_AUTH_HEADER",
    "TASK_TOKEN_ENV",
    "TASK_TOKEN_HEADER",
    "TASK_TOKEN_SECRET_ENV",
    "TOKEN_VERSION",
    "TaskTokenClaims",
    "TaskTokenError",
    "extract_task_token",
    "mint_task_token",
    "task_token_headers",
    "task_token_secret",
    "verify_task_token",
]
