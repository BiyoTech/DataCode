"""Internal (service-to-service) authentication dependency.

Guards the ``/internal/*`` endpoints, which are the control plane's pull channel
for code location registration. These endpoints are NOT user traffic: they carry
no identity and are never reached through the gateway, so the gateway-issued
identity headers used by the public API do not apply here.

Fail-closed by construction: when no ``internal_token`` is configured the
endpoints reject every request rather than serving them openly. A replica's list
of gRPC endpoints is a map of the data plane and must not be readable by anything
that happens to reach the port.
"""

from __future__ import annotations

from typing import Annotated

from datacode_common.security.internal_token import (
    INTERNAL_TOKEN_HEADER,
    extract_token,
    token_matches,
)
from fastapi import Depends, Header

from src.codeloc.errors import PermissionDeniedError
from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


def require_internal_token(
    x_internal_token: str = Header(default="", alias=INTERNAL_TOKEN_HEADER),
    authorization: str = Header(default=""),
) -> None:
    """Authenticate an internal caller by shared secret.

    Raises:
        PermissionDeniedError: no token is configured (endpoint disabled), or the
            presented token does not match.
    """
    if not settings.internal_api_enabled:
        logger.warning("internal_api_disabled_request_rejected")
        raise PermissionDeniedError(
            "The internal API is disabled: no internal token is configured on "
            "this service"
        )

    presented = extract_token(
        internal_header=x_internal_token, authorization=authorization
    )
    if not token_matches(settings.internal_token, presented):
        logger.warning("internal_token_rejected", presented=bool(presented))
        raise PermissionDeniedError("Invalid or missing internal service token")


# FastAPI dependency asserting the caller is a trusted internal service.
InternalCaller = Annotated[None, Depends(require_internal_token)]


__all__ = ["InternalCaller", "require_internal_token"]
