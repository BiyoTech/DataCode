"""Isolation profile policy — which sandbox a task kind runs in (design 04 §5).

``conf/isolation-profiles.yaml`` declares WHERE each kind of work may execute.
Until P6 that file was documentation: the code referenced profile names in
comments while the actual level was hard-coded at each call site. This module
makes it executable, which is the point — the security posture of the platform
must be readable from one file and enforced from one place.

Two rules carry the safety of the whole scheme:

1.  **Fail safe, not fail open.** A task kind no profile claims runs under the
    strongest ENABLED profile, never the weakest and never "unrestricted". The
    fallback order is fixed (``L3_strong -> L3_base -> L3_proc``) and
    deliberately excludes L1/L2: those two assume their input cannot execute
    host code, so falling back into them would grant an unknown task exactly
    the assumption it has not earned. When no fail-safe profile is usable at
    all, selection RAISES — an in-process-only deployment keeps serving
    in-process work, but gated work is refused rather than downgraded.

2.  **Strongest wins on overlap.** When several enabled profiles claim the same
    task kind, the strongest one is chosen. Overlap is normal across
    deployments — ``sqlmesh.plan`` is an L2 task in production but runs under
    L3-proc on a single-node dev box — and resolving it downward would silently
    weaken production the moment a dev-only profile got enabled there.

Availability vs. configuration are kept apart. The YAML says which profiles a
deployment WANTS; :meth:`IsolationPolicy.restricted_to` says which ones the
running process can actually execute (no Kubernetes on a laptop means no
container profiles). Mixing the two would make a missing backend look like a
policy decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from datacode_common.config.yaml_loader import load_yaml_config

logger = logging.getLogger(__name__)

_PACKAGE_FILE = Path(__file__)

CONFIG_FILENAME = "isolation-profiles.yaml"

# Fallback chain for a task kind nothing claims. Fixed in code rather than
# configurable: it is the last line of defence, and a deployment that could
# edit it could also point it at L1.
FAILSAFE_ORDER = ("L3_strong", "L3_base", "L3_proc")

# Sentinel accepted by `default_profile` meaning "resolve through
# FAILSAFE_ORDER at load time".
STRONGEST_ENABLED = "strongest_enabled"

# Container runtime classes that replace the host kernel with a sandboxed one.
# A profile using one of these outranks a plain-runc container.
_SANDBOXED_RUNTIME_CLASSES = frozenset({"gvisor", "kata", "kata-containers"})


class IsolationConfigError(RuntimeError):
    """The isolation configuration cannot be used as written.

    Always fatal at startup. Every case it reports would otherwise end with
    user code running somewhere it was not meant to run.
    """


class NoIsolationAvailable(IsolationConfigError):
    """A task needs a sandbox and none of the usable profiles can host it.

    Raised at SUBMISSION time, not at load time, because it is a legitimate
    state: a deployment with the task plane switched off still serves in-process
    work correctly, and must keep doing so. What it must not do is quietly run
    gated work in-process instead — hence an error rather than a fallback.
    """


class IsolationLevel(str, Enum):
    """The execution vehicle a profile uses.

    Ordering is meaningful (see :attr:`IsolationProfile.strength`) but is NOT
    taken from the declaration order here — see ``_strength_of``.
    """

    IN_PROCESS = "in_process"
    PROCESS = "process"
    CONTAINER = "container"


# Strength ladder. Kept as data so the comparison lives in one place:
#   0  in-process  — no boundary at all; data-only inputs
#   1  process     — separate uid + rlimits, shared filesystem and netns
#   2  container   — separate mount/net/pid namespaces, shared host kernel
#   3  container   + sandboxed kernel
_LEVEL_STRENGTH = {
    IsolationLevel.IN_PROCESS: 0,
    IsolationLevel.PROCESS: 1,
    IsolationLevel.CONTAINER: 2,
}


@dataclass(frozen=True)
class IsolationProfile:
    """One isolation profile, resolved (inheritance already applied).

    Attributes:
        name: Profile key as written in the YAML (e.g. ``L3_base``).
        level: Execution vehicle.
        enabled: Whether the deployment wants this profile at all. A disabled
            profile is never selected, not even as a fallback.
        allowed_task_kinds: Task kinds this profile explicitly claims.
        strength: Comparable hardening rank; see the ladder above.
        settings: The remaining profile fields verbatim (resources, network,
            credentials, rlimits, ...). Kept as a mapping on purpose: this
            module decides WHICH profile applies, and the execution backends
            decide how to honour it. Typing every field here would put
            Kubernetes vocabulary into a module that must also serve the
            process backend.
    """

    name: str
    level: IsolationLevel
    enabled: bool
    allowed_task_kinds: tuple[str, ...]
    strength: int
    settings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def runtime_class(self) -> str:
        """Container runtime class, empty for non-container profiles."""
        return str(self.settings.get("runtime_class", ""))

    @property
    def timeout_seconds(self) -> int:
        """Hard wall-clock budget for one task, 0 when unset."""
        return int(self.settings.get("timeout_seconds", 0) or 0)

    @property
    def is_persistent(self) -> bool:
        """Whether the vehicle is reused across tasks (a warm executor)."""
        return bool(self.settings.get("persistent", False))

    @property
    def warehouse_credential(self) -> str:
        """Declared warehouse credential scope, e.g. ``scoped_readonly``.

        Empty means the profile grants no warehouse access. The value matters
        to callers: the Q5 constraint (design 04 §3.5) is that only a profile
        which cannot execute host language code may hold a writable account.
        """
        credentials = self.settings.get("credentials") or {}
        return str(credentials.get("warehouse", ""))

    @property
    def grants_writable_warehouse(self) -> bool:
        """Whether this profile may hold a writable warehouse account."""
        return self.warehouse_credential == "scoped_readwrite"


class IsolationPolicy:
    """The resolved set of profiles plus the selection rules.

    Immutable. :meth:`restricted_to` returns a new policy rather than mutating
    this one, so a process cannot accidentally widen its own sandbox set.
    """

    def __init__(
        self,
        profiles: Mapping[str, IsolationProfile],
        default_profile: str = STRONGEST_ENABLED,
    ) -> None:
        self._profiles = dict(profiles)
        self._default_declared = default_profile
        self._default = self._resolve_default(default_profile)
        # Precomputed: task kind -> strongest enabled profile claiming it.
        self._by_task_kind = self._index_task_kinds()

    # ── Introspection ──────────────────────────────────────────────────

    @property
    def profile_names(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    @property
    def enabled_profiles(self) -> tuple[IsolationProfile, ...]:
        """Enabled profiles, strongest first."""
        return tuple(
            sorted(
                (p for p in self._profiles.values() if p.enabled),
                key=lambda p: (-p.strength, p.name),
            )
        )

    @property
    def default_profile(self) -> Optional[IsolationProfile]:
        """The profile an unclaimed task kind falls back to.

        ``None`` when no profile in the fail-safe chain is usable — a deployment
        with the task plane switched off. Callers must then refuse gated work
        (see :meth:`profile_for`) rather than substitute a weaker profile.
        """
        return self._default

    def profile(self, name: str) -> IsolationProfile:
        """Return a profile by name.

        Raises:
            IsolationConfigError: No such profile. Callers name profiles from
                code, so a miss is a programming error, not user input.
        """
        try:
            return self._profiles[name]
        except KeyError:
            raise IsolationConfigError(
                f"unknown isolation profile {name!r}; "
                f"declared: {', '.join(self.profile_names) or '(none)'}"
            ) from None

    # ── Selection ──────────────────────────────────────────────────────

    def profile_for(self, task_kind: str) -> IsolationProfile:
        """Return the profile a task kind must run under.

        Falls back to the strongest enabled profile for a kind nothing claims,
        because refusing to answer would push the decision back to a call site
        with no security context to make it.

        Raises:
            NoIsolationAvailable: The kind is unclaimed AND no fail-safe profile
                is usable. Failing is the only safe answer: the alternative is
                running unknown work under a profile that never claimed it.
        """
        selected = self._by_task_kind.get(task_kind)
        if selected is not None:
            return selected

        if self._default is None:
            raise NoIsolationAvailable(
                f"task kind {task_kind!r} is claimed by no usable profile and "
                f"no fail-safe profile ({', '.join(FAILSAFE_ORDER)}) is enabled; "
                "enable an execution backend or declare the kind explicitly"
            )

        logger.warning(
            "isolation_task_kind_unclaimed",
            extra={
                "task_kind": task_kind,
                "fallback_profile": self._default.name,
                "hint": "declare the kind under allowed_task_kinds, "
                "or accept the strongest enabled profile",
            },
        )
        return self._default

    def restricted_to(
        self, available_levels: frozenset[IsolationLevel], *, reason: str = ""
    ) -> "IsolationPolicy":
        """Return a copy with profiles the process cannot execute disabled.

        Used at startup: a runtime without a container backend (dev laptops,
        single-node installs) keeps the same configuration file but must not
        select L2/L3-base, or every task would fail at submission time.

        Disabling is expressed as ``enabled=False`` so the rest of the module —
        including the fail-safe chain — automatically stops considering them.

        Raises:
            IsolationConfigError: Nothing is left enabled. Silently running
                everything in-process would be the worst possible outcome.
        """
        restricted: dict[str, IsolationProfile] = {}
        dropped: list[str] = []
        for name, profile in self._profiles.items():
            if profile.enabled and profile.level not in available_levels:
                restricted[name] = IsolationProfile(
                    name=profile.name,
                    level=profile.level,
                    enabled=False,
                    allowed_task_kinds=profile.allowed_task_kinds,
                    strength=profile.strength,
                    settings=profile.settings,
                )
                dropped.append(name)
            else:
                restricted[name] = profile

        if dropped:
            logger.warning(
                "isolation_profiles_unavailable",
                extra={
                    "disabled_profiles": dropped,
                    "available_levels": sorted(level.value for level in available_levels),
                    "reason": reason or "execution backend not available",
                },
            )

        if not any(p.enabled for p in restricted.values()):
            raise IsolationConfigError(
                "no isolation profile is both enabled and executable "
                f"(available levels: {sorted(level.value for level in available_levels)}); "
                "refusing to start rather than run user code unisolated"
            )

        # The declared default is re-resolved against the reduced set; it may
        # legitimately become None (see _resolve_default).
        return IsolationPolicy(restricted, self._default_declared)

    # ── Internals ──────────────────────────────────────────────────────

    def _resolve_default(self, declared: str) -> Optional[IsolationProfile]:
        """Resolve ``default_profile`` to a concrete, enabled profile.

        Returns None when the fail-safe chain has no enabled member. That is a
        usable state (an in-process-only deployment), so it is not an error here;
        :meth:`profile_for` is what refuses the work that would need one.

        An EXPLICITLY named default is different: naming a profile and having it
        disabled is a contradiction in the configuration, and is rejected.
        """
        if declared and declared != STRONGEST_ENABLED:
            profile = self.profile(declared)
            if not profile.enabled:
                raise IsolationConfigError(
                    f"default_profile {declared!r} is disabled; the fallback "
                    "profile must be usable"
                )
            return profile

        for name in FAILSAFE_ORDER:
            candidate = self._profiles.get(name)
            if candidate is not None and candidate.enabled:
                return candidate
        return None

    def _index_task_kinds(self) -> dict[str, IsolationProfile]:
        """Map each claimed task kind to the strongest enabled claimant."""
        index: dict[str, IsolationProfile] = {}
        for profile in self._profiles.values():
            if not profile.enabled:
                continue
            for kind in profile.allowed_task_kinds:
                incumbent = index.get(kind)
                if incumbent is None:
                    index[kind] = profile
                    continue
                if profile.strength > incumbent.strength:
                    index[kind] = profile
                elif profile.strength == incumbent.strength:
                    # Deterministic tie-break, plus a warning: two equally
                    # hardened profiles claiming one kind is a config smell
                    # (they differ in credentials or network, and which one
                    # applies should not depend on dict ordering).
                    winner, loser = sorted([profile.name, incumbent.name])
                    index[kind] = self._profiles[winner]
                    logger.warning(
                        "isolation_task_kind_ambiguous",
                        extra={
                            "task_kind": kind,
                            "profiles": [profile.name, incumbent.name],
                            "selected": winner,
                            "ignored": loser,
                        },
                    )
        return index


# ── Loading ────────────────────────────────────────────────────────────


def _strength_of(level: IsolationLevel, settings: Mapping[str, Any]) -> int:
    """Rank a profile's hardening.

    A container on a sandboxed kernel outranks one sharing the host kernel;
    everything else follows the level ladder.
    """
    base = _LEVEL_STRENGTH[level]
    if level is IsolationLevel.CONTAINER:
        runtime_class = str(settings.get("runtime_class", "")).lower()
        if runtime_class in _SANDBOXED_RUNTIME_CLASSES:
            return base + 1
    return base


def _resolve_inheritance(
    name: str, raw: Mapping[str, Any], all_raw: Mapping[str, Any], seen: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Flatten ``inherits`` chains, child keys winning over parent keys."""
    if name in seen:
        raise IsolationConfigError(
            f"circular inherits chain in isolation profiles: "
            f"{' -> '.join((*seen, name))}"
        )

    parent_name = raw.get("inherits")
    if not parent_name:
        return dict(raw)

    parent_raw = all_raw.get(parent_name)
    if parent_raw is None:
        raise IsolationConfigError(
            f"profile {name!r} inherits unknown profile {parent_name!r}"
        )

    merged = _resolve_inheritance(
        parent_name, parent_raw, all_raw, seen=(*seen, name)
    )
    merged.update(raw)
    merged.pop("inherits", None)
    return merged


def _build_profile(name: str, resolved: Mapping[str, Any]) -> IsolationProfile:
    """Turn one resolved YAML block into a profile."""
    raw_level = resolved.get("isolation")
    if not raw_level:
        raise IsolationConfigError(
            f"profile {name!r} has no 'isolation' level; cannot rank or execute it"
        )
    try:
        level = IsolationLevel(str(raw_level))
    except ValueError:
        raise IsolationConfigError(
            f"profile {name!r} declares unknown isolation {raw_level!r}; "
            f"expected one of {', '.join(level.value for level in IsolationLevel)}"
        ) from None

    kinds = resolved.get("allowed_task_kinds") or []
    if not isinstance(kinds, list):
        raise IsolationConfigError(
            f"profile {name!r}: allowed_task_kinds must be a list, "
            f"got {type(kinds).__name__}"
        )

    settings = {
        key: value
        for key, value in resolved.items()
        if key not in {"isolation", "enabled", "allowed_task_kinds"}
    }

    return IsolationProfile(
        name=name,
        level=level,
        # Absent `enabled` means enabled: the file lists what a deployment
        # runs, and opting out is the exception (L3_strong).
        enabled=bool(resolved.get("enabled", True)),
        allowed_task_kinds=tuple(str(k) for k in kinds),
        strength=_strength_of(level, settings),
        settings=settings,
    )


def build_isolation_policy(document: Mapping[str, Any]) -> IsolationPolicy:
    """Build a policy from an already-parsed configuration document.

    Separate from file loading so tests and callers holding the document (a
    Cell-specific override, say) do not have to touch the filesystem.
    """
    raw_profiles = document.get("profiles") or {}
    if not raw_profiles:
        raise IsolationConfigError(
            f"{CONFIG_FILENAME} declares no profiles; "
            "there would be nowhere to run anything"
        )

    profiles: dict[str, IsolationProfile] = {}
    for name, raw in raw_profiles.items():
        if not isinstance(raw, Mapping):
            raise IsolationConfigError(
                f"profile {name!r} must be a mapping, got {type(raw).__name__}"
            )
        resolved = _resolve_inheritance(name, raw, raw_profiles)
        profiles[name] = _build_profile(name, resolved)

    default_declared = str(document.get("default_profile") or STRONGEST_ENABLED)
    return IsolationPolicy(profiles, default_declared)


_policy: Optional[IsolationPolicy] = None


def load_isolation_policy(*, refresh: bool = False) -> IsolationPolicy:
    """Load (and cache) the isolation policy from ``conf/``.

    Args:
        refresh: Re-read the file. For tests and config-reload paths only.

    Raises:
        IsolationConfigError: The file is missing or unusable. Fatal by
            design — a platform that cannot tell where user code belongs must
            not start.
    """
    global _policy
    if _policy is not None and not refresh:
        return _policy

    document = load_yaml_config(CONFIG_FILENAME, package_file=_PACKAGE_FILE)
    if not document:
        raise IsolationConfigError(
            f"{CONFIG_FILENAME} not found or empty; expected it in conf/ "
            "(the isolation posture of the platform is declared there)"
        )

    policy = build_isolation_policy(document)
    default = policy.default_profile
    logger.info(
        "isolation_policy_loaded",
        extra={
            "enabled_profiles": [p.name for p in policy.enabled_profiles],
            "default_profile": default.name if default else None,
        },
    )
    _policy = policy
    return policy


__all__ = [
    "CONFIG_FILENAME",
    "FAILSAFE_ORDER",
    "STRONGEST_ENABLED",
    "IsolationConfigError",
    "IsolationLevel",
    "IsolationPolicy",
    "IsolationProfile",
    "NoIsolationAvailable",
    "build_isolation_policy",
    "load_isolation_policy",
]
