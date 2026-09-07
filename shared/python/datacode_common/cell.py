"""Cell identity — a first-class concept from day one (design 03 §2, P1).

A Cell is a complete, self-sufficient platform instance serving a subset of
tenants. Even while only ONE cell exists, every service instance knows its
cell id, puts it in every log line, every metric label and every Redis key —
so scaling from one cell to five (P7) is a deployment action, not a code
change.

Resolution order for the cell id:
    1. ``DATACODE_CELL_ID`` environment variable (set by the deployment).
    2. ``cell-0`` — the bootstrap default for single-cell installations.

The id must be a safe single path/DNS/metric-label token, enforced here so a
misconfigured deployment fails loudly at startup instead of corrupting Redis
keys and log indexes.
"""

from __future__ import annotations

import os
import re

# Same conservatism as a workspace path segment: one token, no separators
# that would break Redis keys / Loki labels / DNS names.
_CELL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

DEFAULT_CELL_ID = "cell-0"
ENV_VAR = "DATACODE_CELL_ID"


class CellIdError(ValueError):
    """Raised when DATACODE_CELL_ID is not a usable cell identifier."""


def cell_id() -> str:
    """Return this instance's cell id (validated).

    Raises CellIdError when the environment variable is set to something
    unusable — a typo like ``cell 0`` or ``Cell/0`` must not silently fan
    out into Redis keys and metric labels.
    """
    value = os.environ.get(ENV_VAR, DEFAULT_CELL_ID)
    if not _CELL_ID_RE.match(value):
        raise CellIdError(
            f"{ENV_VAR} must match {_CELL_ID_RE.pattern!r}, got {value!r}"
        )
    return value
