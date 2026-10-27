"""Workspace ``config.yaml`` maintenance: the platform-managed SQLMesh state.

Why the platform touches this file at all
-----------------------------------------
SQLMesh keeps its state (snapshots, environments, external models) in a schema
of the gateway's own database, ``sqlmesh`` by default. Two consequences drive
the whole design of this module:

1. workspaces sharing one engine would collide on that default schema — one
   workspace would read the other's snapshots and external models;
2. the state must be shared by ALL environments and ALL members of ONE
   workspace, because promoting a snapshot from ``dev`` to ``prod`` is a pointer
   switch inside a single state store. A per-user or per-environment state would
   silently turn every promotion into a full rebuild.

Hence exactly one correct value per workspace: ``sqlmesh__{workspace_id}``.

Ownership model: generate, then verify — never silently override
---------------------------------------------------------------
``config.yaml`` is a Git-tracked file that users read and edit, so a value that
is written in the file while a different one takes effect at runtime is the
worst possible outcome. This module therefore does only two things:

* a gateway WITHOUT ``state_schema`` gets the managed value written into the
  file (workspace creation, or a gateway the user adds later) — the file stays
  the single truth and remains what the user sees;
* a gateway declaring a DIFFERENT value is REJECTED at load time with
  :class:`~src.utils.errors.StateSchemaMismatchError`, which surfaces the
  divergence immediately instead of letting the environment write its snapshots
  into an orphan state that only explodes at promotion time.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

from src.utils.errors import StateSchemaMismatchError
from src.utils.logging import get_logger

logger = get_logger(__name__)

CONFIG_FILENAME = "config.yaml"

_STATE_SCHEMA_KEY = "state_schema"
_STATE_SCHEMA_PREFIX = "sqlmesh__"

# Most engines cap identifiers at 64 characters (MySQL, Postgres); stay below it
# and keep the value deterministic by appending a digest when truncating.
_MAX_SCHEMA_LENGTH = 63
_DIGEST_LENGTH = 8

# Written above the generated key so the file itself explains the contract to
# whoever opens it. ruamel preserves it across every later rewrite.
_MANAGED_COMMENT = (
    "PLATFORM-MANAGED: locates this workspace's shared SQLMesh state.\n"
    "Every environment and member of the workspace must use this exact value;\n"
    "a deviating value is rejected when the project is loaded."
)


@dataclass(frozen=True)
class StateSchemaReconciliation:
    """Outcome of reconciling a project's declared state schema.

    ``changed`` is True only when the file was actually rewritten, which is the
    signal for the caller to drop the local ``.cache`` (it may hold snapshots
    read from a different state schema).
    """

    expected: str
    generated_for: tuple[str, ...]
    changed: bool


def _sanitize_identifier(name: str) -> str:
    """Reduce an arbitrary name to a safe SQL schema identifier fragment."""
    safe = re.sub(r"[^0-9a-zA-Z_]", "_", name or "").strip("_").lower()
    return safe or "workspace"


def workspace_state_schema(workspace_id: str) -> str:
    """Return the SQLMesh state schema of a workspace.

    Scoped to the WORKSPACE — not to the user, not to the environment. This is
    what makes the environment list a shared team asset and what allows virtual
    promotion between environments to be a pointer switch instead of a rebuild.

    Pure and deterministic: the same workspace id always yields the same schema,
    on every service and at every point in time.
    """
    safe = _sanitize_identifier(workspace_id)
    schema = f"{_STATE_SCHEMA_PREFIX}{safe}"
    if len(schema) <= _MAX_SCHEMA_LENGTH:
        return schema

    # Truncate but stay collision-free and reproducible.
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    keep = _MAX_SCHEMA_LENGTH - len(_STATE_SCHEMA_PREFIX) - _DIGEST_LENGTH - 1
    return f"{_STATE_SCHEMA_PREFIX}{safe[:keep]}_{digest}"


def config_path(project_path: str) -> str:
    """Return the SQLMesh ``config.yaml`` path of a project directory."""
    return str(Path(project_path) / CONFIG_FILENAME)


def _load_config(path: Path) -> dict | None:
    """Load ``config.yaml`` and return the parsed document.

    Returns ``None`` when the file is absent (nothing scaffolded yet, or a
    Python/``config.py`` project) or unparsable — callers decide whether that
    is a no-op (reconcile: SQLMesh will report a malformed config far more
    precisely) or an error (repair: the user asked for an explicit action).
    """
    if not path.exists():
        return None
    yaml = YAML()
    yaml.preserve_quotes = True
    try:
        data = yaml.load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - config parsing must not break loading
        logger.warning(
            "state_schema_config_parse_failed", path=str(path), error=str(exc)
        )
        return None
    return data if isinstance(data, dict) else None


def _gateways_of(data: dict) -> dict | None:
    """The document's ``gateways`` mapping, or ``None`` when there is none."""
    gateways = data.get("gateways") if isinstance(data, dict) else None
    if not isinstance(gateways, dict) or not gateways:
        return None
    return gateways


def _dump_config(path: Path, data: dict) -> bool:
    """Write ``data`` back to ``config.yaml``, preserving comments.

    Returns ``False`` when the write fails (e.g. a read-only working copy);
    callers translate that into their own no-op or error policy.
    """
    try:
        with path.open("w", encoding="utf-8") as handle:
            YAML().dump(data, handle)
    except OSError as exc:
        logger.warning("state_schema_write_failed", path=str(path), error=str(exc))
        return False
    return True


def reconcile_state_schema(
    project_path: str, workspace_id: str
) -> StateSchemaReconciliation:
    """Make ``config.yaml`` declare this workspace's managed ``state_schema``.

    Generates the value for gateways that do not declare one (writing it to
    disk, so the file remains the single truth) and rejects any gateway that
    declares a different one.

    Parsing/IO problems are non-fatal: SQLMesh itself will report a malformed
    config far more precisely than we could, so a warning is logged and the
    reconciliation is reported as a no-op.

    Raises:
        StateSchemaMismatchError: a gateway declares a non-managed value.
    """
    expected = workspace_state_schema(workspace_id)
    path = Path(config_path(project_path))
    noop = StateSchemaReconciliation(expected=expected, generated_for=(), changed=False)

    data = _load_config(path)
    gateways = _gateways_of(data) if data is not None else None
    if gateways is None:
        return noop

    generated: list[str] = []
    mismatches: list[tuple[str, str]] = []
    for gateway_name, gateway in gateways.items():
        if not isinstance(gateway, dict):
            continue
        declared = gateway.get(_STATE_SCHEMA_KEY)
        if not declared:
            gateway[_STATE_SCHEMA_KEY] = expected
            _annotate_managed_key(gateway)
            generated.append(str(gateway_name))
        elif str(declared) != expected:
            mismatches.append((str(gateway_name), str(declared)))

    if mismatches:
        logger.error(
            "state_schema_mismatch",
            path=str(path),
            workspace_id=workspace_id,
            expected=expected,
            gateways=[name for name, _ in mismatches],
            declared=[value for _, value in mismatches],
        )
        gateway_name, declared = mismatches[0]
        raise StateSchemaMismatchError(
            gateway=gateway_name,
            expected=expected,
            actual=declared,
            config_path=str(path),
        )

    if not generated:
        return noop

    if not _dump_config(path, data):
        return noop

    logger.info(
        "state_schema_generated",
        path=str(path),
        workspace_id=workspace_id,
        state_schema=expected,
        gateways=generated,
    )
    return StateSchemaReconciliation(
        expected=expected, generated_for=tuple(generated), changed=True
    )


def repair_state_schema(
    project_path: str, workspace_id: str
) -> StateSchemaReconciliation:
    """Rewrite EVERY gateway's ``state_schema`` to the managed value.

    The explicit, user-initiated counterpart to :func:`reconcile_state_schema`:
    the 409 has already told the user the file disagrees with the platform, so
    confirming the repair is informed consent — writing the managed value back
    restores the "the file is the single truth" invariant in one action
    instead of forcing a manual YAML edit.

    Unlike reconcile, an unreadable/unwritable file is an ERROR here: the user
    asked for a concrete action and a silent no-op would leave them staring at
    the same 409.

    Returns:
        The reconciliation outcome; ``generated_for`` lists the gateways whose
        value was actually rewritten (missing and mismatched alike).

    Raises:
        ServiceError: ``config.yaml`` is missing, unparsable, has no gateways,
            or could not be written.
    """
    from src.utils.errors import ErrorCode, ServiceError

    expected = workspace_state_schema(workspace_id)
    path = Path(config_path(project_path))

    data = _load_config(path)
    if data is None or _gateways_of(data) is None:
        raise ServiceError(
            code=ErrorCode.CONTEXT_CONFIG_INVALID,
            message=(
                f"config.yaml was not found or has no gateways at {path}; "
                f"nothing to repair."
            ),
            status_code=400,
        )

    repaired: list[str] = []
    for gateway_name, gateway in _gateways_of(data).items():
        if not isinstance(gateway, dict):
            continue
        if str(gateway.get(_STATE_SCHEMA_KEY) or "") != expected:
            gateway[_STATE_SCHEMA_KEY] = expected
            _annotate_managed_key(gateway)
            repaired.append(str(gateway_name))

    if not repaired:
        return StateSchemaReconciliation(
            expected=expected, generated_for=(), changed=False
        )

    if not _dump_config(path, data):
        raise ServiceError(
            code=ErrorCode.CONTEXT_CONFIG_INVALID,
            message=(
                f"config.yaml at {path} could not be written; check the "
                f"working copy is writable and retry."
            ),
            status_code=500,
        )

    logger.info(
        "state_schema_repaired",
        path=str(path),
        workspace_id=workspace_id,
        state_schema=expected,
        gateways=repaired,
    )
    return StateSchemaReconciliation(
        expected=expected, generated_for=tuple(repaired), changed=True
    )


def _annotate_managed_key(gateway: object) -> None:
    """Attach the managed-value comment above ``state_schema`` (best-effort).

    Only ruamel's ``CommentedMap`` carries comments; a plain dict silently skips
    the annotation, which is cosmetic and must never fail the write.
    """
    setter = getattr(gateway, "yaml_set_comment_before_after_key", None)
    if setter is None:
        return
    try:
        setter(_STATE_SCHEMA_KEY, before=_MANAGED_COMMENT, indent=4)
    except Exception:  # noqa: BLE001 - comments are cosmetic
        logger.debug("state_schema_comment_skipped")
