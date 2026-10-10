"""Env-control endpoint delivery — how a runtime call must be ADDRESSED.

The control plane owns no filesystem, so every Git / engine / provisioning side
effect is dispatched to workspace-runtime. In a multi-replica cell each replica
has its OWN volume, and exactly one of them holds the lease on a given working
copy. "Which replica answers" is therefore part of the contract, not a load
balancing detail: a call sent to the wrong replica either fails the lease gate
or — far worse — succeeds against an empty volume and reports "no worktree" /
"nothing pending" as if that were the truth.

This module is the single source of truth for that addressing. Each endpoint
declares three things:

``key``       what identifies the target: one working COPY, a WORKSPACE, or
              NOTHING (the answer is the same everywhere).
``fanout``    whether ONE replica answers or EVERY candidate must be visited.
              A union ("which members hold a local root") and a removal ("drop
              this worktree") are both incomplete if a replica is skipped.
``place_lease`` whether resolving may ASK the fleet to place a lease. True only
              for the two endpoints that CREATE a working copy: placing first
              is what makes the files land on the replica that will serve them
              afterwards. Every other endpoint must not — placing a lease to
              answer "does this copy have uncommitted work" would clone a
              repository in order to report that it has none.

Why an unclassified path RAISES
-------------------------------
:func:`delivery_of` has no fallback. Defaulting to "any replica" is exactly how
the whole ``git/*`` family ended up on arbitrary replicas, and the failure mode
is silent: the endpoint answers 200 with an honest report about the wrong disk.
A new endpoint must be classified before it can be dispatched at all, so the
omission surfaces as an error in development instead of as corrupted state in
production.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ENV_CONTROL_DELIVERY",
    "Delivery",
    "Fanout",
    "TargetKey",
    "UnclassifiedEndpointError",
    "delivery_of",
]

# Prefix every env-control path shares. Declared once so the table below reads
# as the endpoint names the runtime router defines.
_PREFIX = "/internal/env-control"


class TargetKey(str, Enum):
    """What identifies the replica(s) that may serve a call."""

    #: One working copy: tenant / workspace / user / environment.
    COPY = "copy"
    #: A workspace: any replica holding any copy of it can answer.
    WORKSPACE = "workspace"
    #: Every live replica, no identifiers: per-replica in-process state that
    #: is not attributable to a workspace before it has been read.
    FLEET = "fleet"
    #: Nothing local is involved (remote inspection, cluster reads, placement).
    NONE = "none"


class Fanout(str, Enum):
    """How many replicas take part."""

    #: The first replica that produces a usable answer wins.
    FIRST = "first"
    #: Every candidate is visited and the results are merged by the caller.
    ALL = "all"


@dataclass(frozen=True)
class Delivery:
    """The delivery contract of one endpoint."""

    key: TargetKey
    fanout: Fanout = Fanout.FIRST
    place_lease: bool = False

    @property
    def routed(self) -> bool:
        """Whether this endpoint needs a caller-supplied target to resolve."""
        return self.key in (TargetKey.COPY, TargetKey.WORKSPACE)


class UnclassifiedEndpointError(RuntimeError):
    """A runtime path is absent from :data:`ENV_CONTROL_DELIVERY`.

    Deliberately not a fallback: see the module docstring. Raised at dispatch
    time, so the first call of a new endpoint fails loudly rather than landing
    on a replica chosen by DNS.
    """

    def __init__(self, path: str) -> None:
        super().__init__(
            f"{path!r} is not classified in ENV_CONTROL_DELIVERY; declare how "
            "it must be delivered before dispatching it"
        )
        self.path = path


# Shorthands, so the table below reads as intent rather than as constructor
# arguments.
_CREATES_COPY = Delivery(TargetKey.COPY, Fanout.FIRST, place_lease=True)
_ON_COPY = Delivery(TargetKey.COPY, Fanout.FIRST)
_ON_EVERY_COPY = Delivery(TargetKey.COPY, Fanout.ALL)
_IN_WORKSPACE = Delivery(TargetKey.WORKSPACE, Fanout.FIRST)
_ACROSS_WORKSPACE = Delivery(TargetKey.WORKSPACE, Fanout.ALL)
_ACROSS_FLEET = Delivery(TargetKey.FLEET, Fanout.ALL)
_ANYWHERE = Delivery(TargetKey.NONE)

# Dagster deployment surface. Not env-control, but the same addressing problem:
# a deployment is IN-PROCESS state of the replica that spawned its code
# location, so "which deployments exist" is a union and "stop this one" must
# reach every replica that might be serving it.
DAGSTER_DEPLOYMENTS_PATH = "/api/v1/dagster/deployments"
DAGSTER_ENSURE_PATH = f"{DAGSTER_DEPLOYMENTS_PATH}/ensure"


# Path -> delivery. Exhaustive over the runtime's env-control router; a test
# compares this table against the runtime's route table AND against the paths
# the control plane references, so neither side can add an endpoint without
# declaring how it travels.
ENV_CONTROL_DELIVERY: t.Mapping[str, Delivery] = {
    # ── Git: operations on one member's copy ──────────────────────────
    #
    # The two ``ensure`` calls are the creation path: they place the lease so
    # the scaffold, the first commit and every later request all land on the
    # same volume. That ordering is the fix for "created on replica A, leased
    # to replica B, and B has no object database to serve it from".
    f"{_PREFIX}/git/ensure-repository": _CREATES_COPY,
    f"{_PREFIX}/git/ensure-worktree": _CREATES_COPY,
    # Probes are FANNED OUT, not walked. A replica without the copy answers
    # 200 with zeros / "" / null rather than an error, so a "first answer
    # wins" walk stops on the first replica asked — and when nobody holds the
    # lease (released, expired) that replica is chosen by stable hash, not by
    # where the files are. "0 uncommitted files" for a copy that had ten is
    # exactly that failure. Every replica is asked and the binder merges.
    f"{_PREFIX}/git/pending-work": _ON_EVERY_COPY,
    f"{_PREFIX}/git/push-branch": _ON_COPY,
    # Removals must be COMPLETE: a worktree or branch left on a replica the
    # release skipped resurrects the environment on that member's next push.
    f"{_PREFIX}/git/remove-worktree": _ON_EVERY_COPY,
    f"{_PREFIX}/git/delete-branch": _ON_EVERY_COPY,
    # ── Git: workspace-level facts read through any clone ─────────────
    #
    # These carry no user segment: the runtime scans the members present on its
    # own disk, so a replica holding nothing answers "absent" with a 200. Same
    # reasoning as the probes above: fan out, merge in the binder.
    f"{_PREFIX}/git/branches": _ACROSS_WORKSPACE,
    f"{_PREFIX}/git/branch-presence": _ACROSS_WORKSPACE,
    f"{_PREFIX}/git/first-existing": _ACROSS_WORKSPACE,
    f"{_PREFIX}/git/worktree-head": _ACROSS_WORKSPACE,
    # ── Git: answers that are unions over replicas ────────────────────
    f"{_PREFIX}/git/members": _ACROSS_WORKSPACE,
    f"{_PREFIX}/git/worktrees": _ACROSS_WORKSPACE,
    # ── SQLMesh: the engine is reached THROUGH a working copy ─────────
    #
    # The two mutations answer 404 on a replica with no copy, so a walk moves
    # on correctly; the read answers ``environments: null`` with a 200 and is
    # therefore fanned out like the Git probes.
    f"{_PREFIX}/sqlmesh/ensure-virtual-environment": _IN_WORKSPACE,
    f"{_PREFIX}/sqlmesh/invalidate-virtual-environment": _IN_WORKSPACE,
    f"{_PREFIX}/sqlmesh/state-environments": _ACROSS_WORKSPACE,
    # ── Workspace provisioning ────────────────────────────────────────
    f"{_PREFIX}/workspace/scaffold": _ON_COPY,
    f"{_PREFIX}/workspace/push-initial": _ON_COPY,
    f"{_PREFIX}/workspace/bind": _ON_COPY,
    f"{_PREFIX}/workspace/sync": _ON_COPY,
    # Inspects a REMOTE and creates nothing locally: no working copy exists yet
    # when it runs (that is the point — it decides whether one may be
    # scaffolded at all), so there is no lease to route by.
    f"{_PREFIX}/workspace/provision-remote": _ANYWHERE,
    # Also purely remote: ``git ls-remote`` with the caller's credential.
    f"{_PREFIX}/workspace/remote-branches": _ANYWHERE,
    # ── Sandbox: cluster-wide state, not per-copy ─────────────────────
    #
    # The Kubernetes API is shared by the whole cell, so every replica sees the
    # same Pods. Routing these by lease would be meaningless (they carry no
    # working copy) and impossible (they carry no identifiers to build a key).
    f"{_PREFIX}/sandbox/inventory": _ANYWHERE,
    f"{_PREFIX}/sandbox/delete-code-location-pod": _ANYWHERE,
    # ── Teardown and cache eviction: every volume must be visited ─────
    #
    # Idempotent by contract precisely so they can be fanned out.
    f"{_PREFIX}/workspace/hard-purge": _ACROSS_WORKSPACE,
    f"{_PREFIX}/workspace/purge-member": _ACROSS_WORKSPACE,
    f"{_PREFIX}/workspace/release-contexts": _ACROSS_WORKSPACE,
    # ── Dagster deployments ───────────────────────────────────────────
    #
    # ``ensure`` spawns the code location from one working copy, so it must
    # run where that copy lives; it never places a lease (a deployment is
    # derived state and must not be the reason a repository gets cloned).
    DAGSTER_DEPLOYMENTS_PATH: _ACROSS_FLEET,
    DAGSTER_ENSURE_PATH: _ON_COPY,
}

# Endpoints with a path parameter: matched by prefix, longest first. Kept apart
# from the exact table so an exact path can never be shadowed by a prefix.
_PREFIX_DELIVERY: t.Tuple[t.Tuple[str, Delivery], ...] = (
    # DELETE /api/v1/dagster/deployments/{name}
    (f"{DAGSTER_DEPLOYMENTS_PATH}/", _ACROSS_FLEET),
)


def delivery_of(path: str) -> Delivery:
    """Classify a runtime path, or raise :class:`UnclassifiedEndpointError`.

    Only the path is consulted (query strings are stripped): the delivery is a
    property of the endpoint, and letting arguments influence it would make
    "where does this go" depend on the caller.
    """
    clean = (path or "").split("?", 1)[0]
    exact = ENV_CONTROL_DELIVERY.get(clean)
    if exact is not None:
        return exact
    for prefix, delivery in _PREFIX_DELIVERY:
        if clean.startswith(prefix) and len(clean) > len(prefix):
            return delivery
    raise UnclassifiedEndpointError(clean)
