"""Shared-secret authentication for internal service-to-service calls.

The gateway is the platform's authorization center for USER traffic: it verifies
the access token and publishes the outcome on identity headers. Internal traffic
does not pass through it — the control plane pulling code locations from a data
plane replica, or one service calling another over the cluster network — so it
needs its own, much simpler, proof of origin.

This module is that proof: a single shared secret, presented on a header,
compared in constant time. It deliberately carries NO identity and NO
permissions; an internal caller is not a user and must never be treated as one.
Endpoints guarded this way expose infrastructure facts (which code locations a
replica runs), never tenant data.

Design constraints:

* **Env-only secret.** The value never appears in YAML or in a repository; each
  service reads it from its own environment variable.
* **Fail closed.** An unset expected token means the guarded endpoints are
  DISABLED, not open. A misconfigured deployment must lose a feature, never its
  authentication.
* **Constant-time compare.** A byte-by-byte early return leaks the secret one
  character at a time to a caller who can measure it.

Framework-agnostic on purpose (no FastAPI import): the same helpers serve REST
dependencies, gRPC interceptors and outbound clients.
"""

from __future__ import annotations

import hmac

# Preferred header. A dedicated name keeps the internal secret out of
# ``Authorization``, which downstream proxies and log scrubbers treat as user
# credentials and may rewrite.
INTERNAL_TOKEN_HEADER = "X-Internal-Token"

_BEARER_PREFIX = "bearer "


def extract_token(*, internal_header: str = "", authorization: str = "") -> str:
    """Return the token a caller presented, from either accepted location.

    ``X-Internal-Token`` wins. ``Authorization: Bearer <token>`` is accepted as a
    fallback because some service meshes and HTTP clients can only be configured
    to set that header.
    """
    if internal_header.strip():
        return internal_header.strip()
    candidate = authorization.strip()
    if candidate.lower().startswith(_BEARER_PREFIX):
        return candidate[len(_BEARER_PREFIX) :].strip()
    return ""


def token_matches(expected: str, presented: str) -> bool:
    """Constant-time comparison of a presented token against the expected one.

    Returns False when either side is empty: an unconfigured service must not
    authenticate anybody, and an absent credential is never a match.
    """
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


def auth_headers(token: str) -> dict[str, str]:
    """Headers an internal CLIENT must send. Empty when no token is configured.

    Keeps callers from hand-assembling the header name, which is how the two
    sides of a shared secret drift apart.
    """
    return {INTERNAL_TOKEN_HEADER: token} if token else {}


__all__ = [
    "INTERNAL_TOKEN_HEADER",
    "auth_headers",
    "extract_token",
    "token_matches",
]
