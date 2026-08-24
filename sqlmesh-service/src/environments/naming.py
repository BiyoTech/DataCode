"""Environment name algebra — the single source of truth for naming.

``environment name`` <-> ``git branch`` <-> ``dagster deployment`` are pure,
bidirectional functions. Every service that needs one of those names derives it
here (the Dagster side mirrors this module, the Go gateway only re-validates
:data:`ENV_NAME_RE`) so a name can never be re-invented inconsistently.

The character set is the INTERSECTION of the constraints imposed by:
SQLMesh environment names (they are suffixed into schema names,
``{schema}__{env}``), SQL identifiers, Git refs, Dagster location names and
K8s resource names. That intersection is lowercase snake_case, which is why
``-`` and ``/`` are rejected.

This module is deliberately dependency-free and side-effect-free.
"""

from __future__ import annotations

import hashlib
import re

# Lowercase snake_case, 2..40 chars, must start with a letter.
ENV_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# Names reserved by the working-copy layout. Every environment lives under
# ``{user}/envs/{env}``, so an environment named ``envs`` makes the path
# ambiguous for any consumer that locates the environment segment by value
# instead of by position (the backend parses positionally; this keeps every
# other consumer — gateway, scripts, UI — out of that trap).
RESERVED_ENV_NAMES = frozenset({"envs"})

# The production environment of a workspace: SQLMesh's `prod` and Git's `main`.
PRODUCTION_ENV = "prod"
PRODUCTION_BRANCH = "main"

# Non-production environments live in their own branch namespace so they never
# collide with branches a repository already carries (``release/1.0``, ...).
# A branch outside this namespace is "platform-external": read-only, not
# applicable, and cannot back an environment.
BRANCH_NAMESPACE = "env/"

# Environment types. They drive scheduling and reclamation policy, not naming.
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
# Code merge between two environment branches, in either direction: promoting a
# source branch into a target, or syncing a target's commits back into the
# source to resolve a conflict. Kept distinct from ``promote`` because a merge
# lands CODE only — the data still has to be applied on the target.
ACTION_MERGE = "merge"
ACTION_DESTROY = "destroy"
ACTION_RECONCILE = "reconcile"
# TTL lifecycle of a sandbox: the warning that precedes reclamation and the
# reclamation itself. Kept distinct from ``destroy`` so the timeline explains
# WHY an environment disappeared without the reader parsing event details.
ACTION_EXPIRE = "expire"

# Statuses of an environment whose TTL may be reclaimed. ``applying`` and
# ``promoting`` are excluded on purpose: an operation is in flight, and the
# environment lock would refuse the destroy anyway.
RECLAIMABLE_STATUSES = (STATUS_ACTIVE, STATUS_DRIFTED, STATUS_FAILED)

# Separator of the registry primary key ``{workspace_id}:{name}``. ``:`` cannot
# appear in a workspace id nor in an environment name, so the key is unambiguous.
_ID_SEPARATOR = ":"

# Deployment names double as K8s resource names, which are capped at 63
# characters. Names longer than that are truncated around a digest so they stay
# unique and reproducible (see :func:`deployment_name`).
MAX_DEPLOYMENT_NAME_LENGTH = 63
_DEPLOYMENT_SEPARATOR = "__"
_DEPLOYMENT_DIGEST_LENGTH = 8


def is_valid_env_name(name: str) -> bool:
    """Whether ``name`` is a usable environment name."""
    return bool(ENV_NAME_RE.match(name or "")) and name not in RESERVED_ENV_NAMES


def branch_of(env: str) -> str:
    """Map an environment name to its Git branch name."""
    return PRODUCTION_BRANCH if env == PRODUCTION_ENV else f"{BRANCH_NAMESPACE}{env}"


def env_of(branch: str) -> str | None:
    """Inverse of :func:`branch_of`.

    Returns ``None`` for branches outside the platform namespace, which is how
    callers detect a platform-external branch.
    """
    if branch == PRODUCTION_BRANCH:
        return PRODUCTION_ENV
    if branch.startswith(BRANCH_NAMESPACE):
        candidate = branch[len(BRANCH_NAMESPACE):]
        return candidate if is_valid_env_name(candidate) else None
    return None


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


def deployment_name(workspace_id: str, env: str) -> str:
    """Dagster deployment / code location name (GraphQL-safe identifier).

    Bounded by :data:`MAX_DEPLOYMENT_NAME_LENGTH`: the name is also used for K8s
    resources, whose labels cap at 63 characters. The ENVIRONMENT suffix is kept
    intact and the workspace part is truncated with a deterministic digest, so
    two long workspace ids can never collapse onto the same deployment while the
    function stays pure (the same inputs always yield the same name).
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


def _safe(value: str) -> str:
    """Reduce an arbitrary identifier to ``[a-z0-9_]``."""
    return re.sub(r"[^0-9a-zA-Z_]", "_", value or "").lower()
