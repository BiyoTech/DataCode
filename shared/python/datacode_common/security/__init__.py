"""Shared security primitives for DataCode Python services.

Two distinct kinds of credential live here, and conflating them would be a
serious mistake:

* ``internal_token`` — a SERVICE proving it is part of the platform. Carries no
  identity; guards internal endpoints between trusted components.
* ``task_token`` — an untrusted SANDBOX proving which tenant, workspace and task
  it is. Signed, self-scoping and expiring with the task. User code may hold one
  of these; it must never hold the former.
"""

from datacode_common.security.internal_token import (
    INTERNAL_TOKEN_HEADER,
    auth_headers,
    extract_token,
    token_matches,
)
from datacode_common.security.task_token import (
    PROXY_AUTH_HEADER,
    TASK_TOKEN_ENV,
    TASK_TOKEN_HEADER,
    TASK_TOKEN_SECRET_ENV,
    TaskTokenClaims,
    TaskTokenError,
    extract_task_token,
    mint_task_token,
    task_token_headers,
    task_token_secret,
    verify_task_token,
)

__all__ = [
    "INTERNAL_TOKEN_HEADER",
    "PROXY_AUTH_HEADER",
    "TASK_TOKEN_ENV",
    "TASK_TOKEN_HEADER",
    "TASK_TOKEN_SECRET_ENV",
    "TaskTokenClaims",
    "TaskTokenError",
    "auth_headers",
    "extract_task_token",
    "extract_token",
    "mint_task_token",
    "task_token_headers",
    "task_token_secret",
    "token_matches",
    "verify_task_token",
]
