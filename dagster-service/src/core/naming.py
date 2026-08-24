"""Platform naming algebra — the Dagster-side mirror of the registry's rules.

``environment name`` <-> ``git branch`` <-> ``dagster deployment`` are pure,
bidirectional functions owned by the environment registry
(``sqlmesh-service/src/environments/naming.py``). This module mirrors the subset
this service needs so a deployment name can be derived here IDENTICALLY to the
way the registry derives it — the two must agree byte for byte, because one
writes the name into its rows and the other into ``workspace.yaml`` and the
Dagster instance.

Mirroring rather than calling: dagster-service must be able to serve a code
location while the registry is unreachable, and a pure function cannot be a
availability dependency. The contract is frozen by the design document (§3.2)
and covered by tests on both sides.

Also holds the working-copy path convention: a ``workspace_path`` is always the
root of one environment worktree, ``{base}/{tenant}/{workspace}/{user}/envs/{env}``.
That path is the ONLY identity handle most requests carry, so decomposing it is
what lets this service know which workspace and which environment it is serving.

This module is deliberately dependency-free and side-effect-free.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# Lowercase snake_case, 2..40 chars, must start with a letter. Mirrors the
# registry: the character set is the intersection of SQLMesh environment names,
# SQL identifiers, Git refs, Dagster location names and K8s resource names.
ENV_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# Reserved by the working-copy layout: every environment lives under
# ``{user}/envs/{env}``, so an environment called ``envs`` is ambiguous.
RESERVED_ENV_NAMES = frozenset({"envs"})

# The production environment of a workspace.
PRODUCTION_ENV = "prod"

# Deployment types. They drive scheduling policy, not naming.
ENV_TYPE_PRODUCTION = "production"
ENV_TYPE_SHARED = "shared"
ENV_TYPE_SANDBOX = "sandbox"
ENV_TYPES = (ENV_TYPE_PRODUCTION, ENV_TYPE_SHARED, ENV_TYPE_SANDBOX)

# Deployment names double as K8s resource names, which cap at 63 characters.
MAX_DEPLOYMENT_NAME_LENGTH = 63
_DEPLOYMENT_SEPARATOR = "__"
_DEPLOYMENT_DIGEST_LENGTH = 8

# Directory holding a user's environment worktrees, and the number of segments
# between the workspace base path and one working copy:
# {tenant_id}/{workspace_id}/{user_id}/envs/{environment}
ENVS_DIR = "envs"
_LAYOUT_DEPTH = 5


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

        Production is recognized by its environment name; everything else is a
        sandbox, which is the type with the strictest policy (no schedules, TTL).
        Guessing "shared" or "production" instead would hand an unidentified
        working copy the right to drive production data.
        """
        return (
            ENV_TYPE_PRODUCTION
            if self.environment == PRODUCTION_ENV
            else ENV_TYPE_SANDBOX
        )


def is_valid_env_name(name: str) -> bool:
    """Whether ``name`` is a usable environment name."""
    return bool(ENV_NAME_RE.match(name or "")) and name not in RESERVED_ENV_NAMES


def is_valid_env_type(env_type: str) -> bool:
    """Whether ``env_type`` is one of the known deployment types."""
    return env_type in ENV_TYPES


def deployment_name(workspace_id: str, env: str) -> str:
    """Dagster deployment / code location name (GraphQL-safe identifier).

    Bounded by :data:`MAX_DEPLOYMENT_NAME_LENGTH`: the name is also used for K8s
    resources, whose labels cap at 63 characters. The ENVIRONMENT suffix is kept
    intact and the workspace part is truncated with a deterministic digest, so
    two long workspace ids can never collapse onto the same deployment while the
    function stays pure.

    Must stay identical to the registry's ``deployment_name``.
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


def parse_working_copy(base_path: str, path: str) -> WorkingCopy:
    """Decompose a working copy into its platform identity.

    Enforces the exact ``{base}/{tenant}/{workspace}/{user}/envs/{env}`` layout:
    the path must live inside ``base_path`` and expose exactly five segments
    below it. Rejecting instead of guessing is what keeps a caller from reaching
    another tenant's directory, the shared object database or the user root.

    Raises:
        WorkingCopyLayoutError: the path is outside the base directory or does
            not match the layout.
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
