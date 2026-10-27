"""Tenant scoping and row serialization for cross-cell migration.

A tenant moving between cells has its rows copied by TWO services: the control
plane moves the tables it owns (registry, credentials, quotas) and the runtime
moves the tables it owns (knowledge metadata, drafts, apply history). Each owns
its tables end to end, because whoever creates a table is the only one allowed to
create it — concurrent ``CREATE TABLE IF NOT EXISTS`` can fail inside
PostgreSQL's catalog, and a mirrored column definition drifts silently (a large
text column that is 64 KiB on one side and 16 MiB on the other truncates data
during the migration it was meant to preserve).

What must NOT differ between the two halves is the answer to "which rows belong
to this tenant". Export, import-replacement, verification and purge all rest on
it: if purge selected more rows than export did, the retention cleanup would
delete data that was never copied. That definition therefore lives here, in one
place, used by both services.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = [
    "InvalidTenantIdError",
    "validate_tenant_id",
    "tenant_path_prefix",
    "serialize_row",
    "deserialize_row",
]


class InvalidTenantIdError(ValueError):
    """A tenant id that must never reach a path-prefix filter."""


def validate_tenant_id(tenant_id: str) -> str:
    """Reject ids that could escape the workspace-path prefix filter.

    ``/`` would let a crafted id match OTHER tenants' paths, and ``..`` would
    escape the base directory. The prefix filter's safety rests entirely on this
    check, so it is applied where the id enters rather than trusted from callers.
    """
    if not tenant_id or "/" in tenant_id or ".." in tenant_id:
        raise InvalidTenantIdError(f"invalid tenant id: {tenant_id!r}")
    return tenant_id


def tenant_path_prefix(base_path: str, tenant_id: str) -> str:
    """Working-copy path prefix for one tenant (``{base}/{tenant}/``).

    The TRAILING SLASH is load-bearing: without it, tenant ``t1``'s prefix also
    matches every path of tenant ``t10``, so one tenant's migration would carry
    off (and later purge) another's rows.
    """
    validate_tenant_id(tenant_id)
    return f"{base_path.rstrip('/')}/{tenant_id}/"


def serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe row: datetime becomes an ISO 8601 string.

    Values are stored and compared in UTC throughout the platform; the string
    keeps the offset, so the round trip is lossless regardless of the driver's
    timezone handling.
    """
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def deserialize_row(row: dict[str, Any], datetime_columns: tuple[str, ...]) -> dict[str, Any]:
    """Inverse of :func:`serialize_row` for insertion.

    Only the declared columns are parsed: guessing "does this string look like a
    timestamp" would eventually convert a text value that merely resembles one.
    """
    out = dict(row)
    for column in datetime_columns:
        value = out.get(column)
        if isinstance(value, str) and value:
            out[column] = datetime.fromisoformat(value)
    return out
