"""Platform naming algebra — the SINGLE SOURCE OF TRUTH (design 07, P1).

``environment name`` <-> ``git branch`` <-> ``dagster deployment`` are pure,
bidirectional functions. Before P1 this algebra existed as two mirrored
modules (``sqlmesh-service/src/environments/naming.py`` and
``dagster-service/src/core/naming.py``) kept in sync by discipline and tests;
P1 merges them here so a name can never be re-invented inconsistently.

The character set is the INTERSECTION of the constraints imposed by:
SQLMesh environment names (they are suffixed into schema names,
``{schema}__{env}``), SQL identifiers, Git refs, Dagster location names and
K8s resource names. That intersection is lowercase snake_case, which is why
``-`` and ``/`` are rejected.

Also owns the working-copy identity decomposition (``WorkingCopy``): a
``workspace_path`` is always the root of one environment worktree,
``{base}/{tenant}/{workspace}/{user}/envs/{env}``. That path is the ONLY
identity handle most requests carry, so decomposing it is what lets a
service know which workspace and which environment it is serving.

This module is deliberately dependency-free and side-effect-free.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Lowercase snake_case, 2..40 chars, must start with a letter.
ENV_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# Names reserved by the working-copy layout. Every environment lives under
# ``{user}/envs/{env}``, so an environment named ``envs`` makes the path
# ambiguous for any consumer that locates the environment segment by value
# instead of by position (backends parse positionally; this keeps every
# other consumer — gateway, scripts, UI — out of that trap).
RESERVED_ENV_NAMES = frozenset({"envs"})

# The production environment of a workspace: SQLMesh's `prod`, Git's `main`.
PRODUCTION_ENV = "prod"
PRODUCTION_BRANCH = "main"

# Non-production environments live in their own branch namespace so they
# never collide with branches a repository already carries (``release/1.0``).
# A branch outside this namespace is "platform-external": read-only, not
# applicable, and cannot back an environment.
BRANCH_NAMESPACE = "env/"

# Directory holding a user's environment worktrees, and the number of
# segments between the workspace base path and one working copy:
# {tenant_id}/{workspace_id}/{user_id}/envs/{environment}
ENVS_DIR = "envs"
_LAYOUT_DEPTH = 5

# Environment / deployment types. They drive scheduling and reclamation
# policy, not naming.
ENV_TYPE_PRODUCTION = "production"
ENV_TYPE_SHARED = "shared"
ENV_TYPE_SANDBOX = "sandbox"
ENV_TYPES = (ENV_TYPE_PRODUCTION, ENV_TYPE_SHARED, ENV_TYPE_SANDBOX)

# Registry lifecycle states (see the state machine in the design document).
STATUS_CREATING = "creating"
STATUS_ACTIVE = "active"
STATUS_APPLYING = "applying"
STATUS_PROMOTING = "promoting"
STATUS_DRIFTED = "drifted"
STATUS_ARCHIVING = "archiving"
STATUS_ARCHIVED = "archived"
STATUS_FAILED = "failed"

# Audit actions recorded in ``environment_events``.
ACTION_CREATE = "create"
ACTION_OPEN = "open"
ACTION_APPLY = "apply"
ACTION_PROMOTE = "promote"
# Code merge between two environment branches, in either direction: promoting
# a source branch into a target, or syncing a target's commits back into the
# source to resolve a conflict. Kept distinct from ``promote`` because a
# merge lands CODE only — the data still has to be applied on the target.
ACTION_MERGE = "merge"
ACTION_DESTROY = "destroy"
ACTION_RECONCILE = "reconcile"
# TTL lifecycle of a sandbox: the warning that precedes reclamation and the
# reclamation itself, so the timeline explains WHY an environment
# disappeared without the reader parsing event details.
ACTION_EXPIRE = "expire"

# Statuses of an environment whose TTL may be reclaimed. ``applying`` and
# ``promoting`` are excluded on purpose: an operation is in flight, and the
# environment lock would refuse the destroy anyway.
RECLAIMABLE_STATUSES = (STATUS_ACTIVE, STATUS_DRIFTED, STATUS_FAILED)

# Separator of the registry primary key ``{workspace_id}:{name}``. ``:``
# cannot appear in a workspace id nor in an environment name, so the key is
# unambiguous.
_ID_SEPARATOR = ":"

# Deployment names double as K8s resource names, which are capped at 63
# characters. Names longer than that are truncated around a digest so they
# stay unique and reproducible (see :func:`deployment_name`).
MAX_DEPLOYMENT_NAME_LENGTH = 63
_DEPLOYMENT_SEPARATOR = "__"
_DEPLOYMENT_DIGEST_LENGTH = 8


# ── Validation ────────────────────────────────────────────────────────────


def is_valid_env_name(name: str) -> bool:
    """Whether ``name`` is a usable environment name."""
    return bool(ENV_NAME_RE.match(name or "")) and name not in RESERVED_ENV_NAMES


def is_valid_env_type(env_type: str) -> bool:
    """Whether ``env_type`` is one of the known deployment types."""
    return env_type in ENV_TYPES


# ── env <-> branch algebra ────────────────────────────────────────────────


def branch_of(env: str) -> str:
    """Map an environment name to its Git branch name."""
    return PRODUCTION_BRANCH if env == PRODUCTION_ENV else f"{BRANCH_NAMESPACE}{env}"


def env_of(branch: str) -> str | None:
    """Inverse of :func:`branch_of`.

    Returns ``None`` for branches outside the platform namespace, which is
    how callers detect a platform-external branch.
    """
    if branch == PRODUCTION_BRANCH:
        return PRODUCTION_ENV
    if branch.startswith(BRANCH_NAMESPACE):
        candidate = branch[len(BRANCH_NAMESPACE):]
        return candidate if is_valid_env_name(candidate) else None
    return None


# ── registry id algebra ───────────────────────────────────────────────────


def environment_id(workspace_id: str, env: str) -> str:
    """Registry primary key of an environment."""
    return f"{workspace_id}{_ID_SEPARATOR}{env}"


def split_environment_id(value: str) -> tuple[str, str]:
    """Split a registry key back into ``(workspace_id, env)``.

    Returns ``("", "")`` when the value does not carry the expected shape, so
    callers answer 404 instead of running a query with garbage.
    """
    workspace_id, separator, env = (value or "").rpartition(_ID_SEPARATOR)
    if not separator or not workspace_id or not is_valid_env_name(env):
        return "", ""
    return workspace_id, env


# ── deployment name algebra ───────────────────────────────────────────────


def deployment_name(workspace_id: str, env: str) -> str:
    """Dagster deployment / code location name (GraphQL-safe identifier).

    Bounded by :data:`MAX_DEPLOYMENT_NAME_LENGTH`: the name is also used for
    K8s resources, whose labels cap at 63 characters. The ENVIRONMENT suffix
    is kept intact and the workspace part is truncated with a deterministic
    digest, so two long workspace ids can never collapse onto the same
    deployment while the function stays pure.
    """
    workspace_part = _safe(workspace_id)
    budget = MAX_DEPLOYMENT_NAME_LENGTH - len(env) - len(_DEPLOYMENT_SEPARATOR)
    if len(workspace_part) > budget:
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[
            :_DEPLOYMENT_DIGEST_LENGTH
        ]
        keep = max(0, budget - _DEPLOYMENT_DIGEST_LENGTH - 1)
        workspace_part = f"{workspace_part[:keep]}_{digest}"
    return f"{workspace_part}{_DEPLOYMENT_SEPARATOR}{env}"


def suggest_sandbox_name(user_id: str) -> str:
    """Suggested personal sandbox name for a user, e.g. ``dev_u42``.

    Only a suggestion: the caller may override it. Sanitizing here keeps the
    suggestion valid even for opaque identity provider subjects.
    """
    slug = _safe(user_id).strip("_") or "user"
    return f"dev_{slug}"[:40].rstrip("_")


# ── working-copy identity ─────────────────────────────────────────────────


class WorkingCopyLayoutError(ValueError):
    """Raised when a path does not match the environment working-copy layout."""


@dataclass(frozen=True)
class WorkingCopy:
    """The platform identity encoded in one ``workspace_path``."""

    tenant_id: str
    workspace_id: str
    user_id: str
    environment: str
    workspace_path: str

    @property
    def deployment_name(self) -> str:
        """Dagster deployment / code location name of this working copy."""
        return deployment_name(self.workspace_id, self.environment)

    @property
    def default_env_type(self) -> str:
        """Deployment type to assume when the registry did not state one.

        Production is recognized by its environment name; everything else is
        a sandbox, which is the type with the strictest policy (no schedules,
        TTL). Guessing "shared" or "production" instead would hand an
        unidentified working copy the right to drive production data.
        """
        return (
            ENV_TYPE_PRODUCTION
            if self.environment == PRODUCTION_ENV
            else ENV_TYPE_SANDBOX
        )

    @property
    def lease_key(self) -> str:
        """The unit of single-writer ownership (design 03 §3.3, P4).

        One lease per working copy, so the key is exactly the working-copy
        identity minus the base path — see :func:`lease_key` for why it is
        this tuple and not the workspace or the user.
        """
        return lease_key(
            self.tenant_id, self.workspace_id, self.user_id, self.environment
        )


def parse_working_copy(base_path: str, path: str) -> WorkingCopy:
    """Decompose a working copy into its platform identity.

    Enforces the exact ``{base}/{tenant}/{workspace}/{user}/envs/{env}``
    layout: the path must live inside ``base_path`` and expose exactly five
    segments below it. Rejecting instead of guessing is what keeps a caller
    from reaching another tenant's directory, the shared object database or
    the user root.

    Raises:
        WorkingCopyLayoutError: the path is outside the base directory or
            does not match the layout.
    """
    base = Path(base_path).expanduser().resolve()
    target = Path(path).expanduser().resolve()

    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise WorkingCopyLayoutError(
            f"path is outside the workspace base directory: {path}"
        ) from exc

    parts = relative.parts
    if len(parts) != _LAYOUT_DEPTH:
        raise WorkingCopyLayoutError(
            "path must match {base}/{tenant_id}/{workspace_id}/{user_id}/envs/"
            f"{{environment}}, got {len(parts)} segment(s) below base: {path}"
        )

    tenant_id, workspace_id, user_id, envs_dir, environment = parts
    if envs_dir != ENVS_DIR:
        raise WorkingCopyLayoutError(
            f"environment working copies must live under {ENVS_DIR!r}: {path}"
        )
    if not is_valid_env_name(environment):
        raise WorkingCopyLayoutError(f"invalid environment: {environment!r}")

    return WorkingCopy(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
        environment=environment,
        workspace_path=str(target),
    )


def _safe(value: str) -> str:
    """Reduce an arbitrary identifier to ``[a-z0-9_]``."""
    return re.sub(r"[^0-9a-zA-Z_]", "_", value or "").lower()


# ── workspace lease identity (design 03 §3.3, P4) ─────────────────────────

# Segments of a lease key. ``/`` is used because no segment may contain it
# (tenant/workspace/user are path segments and environment names are
# ``[a-z][a-z0-9_]*``), which makes the key both unambiguous and readable in
# logs, Redis keys and trace attributes.
_LEASE_SEPARATOR = "/"
_LEASE_ARITY = 4


class LeaseKeyError(ValueError):
    """Raised when a lease key is malformed."""


def lease_key(
    tenant_id: str, workspace_id: str, user_id: str, environment: str
) -> str:
    """Build the lease key of one working copy.

    Why this exact tuple is the unit of ownership:

    * it is the granularity of a Git worktree, and a worktree is what must
      have a single writer (design 05 §5.2 I1). A coarser key (per workspace
      or per user) would serialize unrelated environments onto one instance
      and destroy the parallel-plan property the layout was chosen for;
    * a finer key does not exist — nothing below an environment has
      independent on-disk state.

    The environment is included even though it maps 1:1 to a Git branch: the
    branch name is a Git concept, while the lease is a routing concept, and
    coupling them would make branch renames a routing event.
    """
    parts = (tenant_id, workspace_id, user_id, environment)
    for field, value in zip(
        ("tenant_id", "workspace_id", "user_id", "environment"), parts, strict=True
    ):
        if not value:
            raise LeaseKeyError(f"lease key requires a non-empty {field}")
        if _LEASE_SEPARATOR in value:
            raise LeaseKeyError(
                f"{field} must not contain {_LEASE_SEPARATOR!r}: {value!r}"
            )
    if not is_valid_env_name(environment):
        raise LeaseKeyError(f"invalid environment: {environment!r}")
    return _LEASE_SEPARATOR.join(parts)


def lease_key_from_working_copy_path(workspace_path: str) -> str:
    """Derive a lease key from a ``workspace_path`` WITHOUT knowing the base.

    :func:`parse_working_copy` is the strict decomposition: it resolves against
    the materialization root and touches the filesystem, which only the service
    that owns the working copies can do. Routing callers are in a different
    position — the gateway and agent-service never see that root and have no
    filesystem to resolve against — yet they need the same key, because routing
    to the wrong replica is exactly what the lease prevents.

    So segments are read from the END, and the ``envs`` level is asserted. That
    assertion is what rejects the shared object database (``.repo``), the user
    root and any arbitrary nested folder: none of them is a leasable unit, and
    coercing one into a key would let two different things share a lease.

    Mirrors ``lease.KeyFromWorkingCopyPath`` in shared/go/lease/key.go, verified
    against the same vectors (shared/lease-vectors.json). A divergence between
    the two would not raise: it would produce a Redis miss, a second lease
    request, and a working copy mounted twice.

    Raises:
        LeaseKeyError: the path is not an environment working copy.
    """
    if not workspace_path:
        raise LeaseKeyError("lease key requires a non-empty workspace path")

    # Normalize separators and collapse "..", so a traversal cannot smuggle in
    # another tenant's directory: the EFFECTIVE segments are what matter.
    cleaned = PurePosixPath(workspace_path.replace("\\", "/"))
    segments = [part for part in cleaned.parts if part not in ("/", "")]
    # Resolve "." / ".." on the segment list rather than on disk: these callers
    # have no filesystem, and Path.resolve() would consult the local one.
    effective: list[str] = []
    for segment in segments:
        if segment == ".":
            continue
        if segment == "..":
            if effective:
                effective.pop()
            continue
        effective.append(segment)

    if len(effective) < _LAYOUT_DEPTH:
        raise LeaseKeyError(
            f"{workspace_path!r} is not an environment working copy: expected "
            "{tenant_id}/{workspace_id}/{user_id}/envs/{environment} below a base"
        )

    tenant_id, workspace_id, user_id, envs_dir, environment = effective[-_LAYOUT_DEPTH:]
    if envs_dir != ENVS_DIR:
        raise LeaseKeyError(
            f"working copies must live under {ENVS_DIR!r}: {workspace_path!r}"
        )
    return lease_key(tenant_id, workspace_id, user_id, environment)


def split_lease_key(value: str) -> tuple[str, str, str, str]:
    """Decompose a lease key into ``(tenant, workspace, user, environment)``.

    The gateway routes on this without touching the filesystem, so parsing
    must reject anything malformed rather than fall back to a default — an
    unparseable key that silently became a valid one would route two
    different working copies to the same lease.
    """
    parts = (value or "").split(_LEASE_SEPARATOR)
    if len(parts) != _LEASE_ARITY or not all(parts):
        raise LeaseKeyError(
            "lease key must be "
            "{tenant_id}/{workspace_id}/{user_id}/{environment}, got: "
            f"{value!r}"
        )
    tenant_id, workspace_id, user_id, environment = parts
    if not is_valid_env_name(environment):
        raise LeaseKeyError(f"invalid environment in lease key: {environment!r}")
    return tenant_id, workspace_id, user_id, environment

