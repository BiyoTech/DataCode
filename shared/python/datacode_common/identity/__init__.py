"""Identity-service integration: cell self-registration.

The gateway answers authorization inline (identity headers it writes itself),
so what remains here is the one identity interaction a NON-gateway process
needs: a control plane registering its cell with the global identity service at
startup (design 03 §2.4, P7).
"""

from datacode_common.identity.cell_registry import (
    INTERNAL_TOKEN_ENV,
    CellRegistration,
    register_cell,
    registration_from_env,
)

__all__ = [
    "INTERNAL_TOKEN_ENV",
    "CellRegistration",
    "register_cell",
    "registration_from_env",
]
