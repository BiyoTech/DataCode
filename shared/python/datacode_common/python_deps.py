"""Third-party Python dependencies of a working copy — one contract, three readers.

A working copy is a team-shared project, so its third-party Python packages are
declared ONCE, in ``asset-dag/pyproject.toml`` under ``[project].dependencies``,
and installed ONCE per copy into a platform-owned overlay directory. Three
processes then put that overlay on ``sys.path``: the Dagster code location, the
local sandbox backend, and the Kubernetes sandbox Job.

Everything those processes must agree on lives here — the declaration format,
the fingerprint, the manifest shape, the overlay path, and the rules about
shadowing platform packages. Three copies of these rules would drift silently:
a fingerprint computed differently on the writing side than on the reading side
turns "already installed" into "reinstall on every reload", and a path computed
differently turns a successful install into an unimportable one.

Why an overlay directory and not a virtualenv
---------------------------------------------
The same working copy is ``/data/workspaces/...`` to the runtime and
``/workspace`` inside a sandbox container (the mount point is rewritten by the
container spec). A virtualenv pins absolute paths into ``pyvenv.cfg`` and into
every console-script shebang, so it stops working the moment it is read through
a different mount point. ``pip install --target`` produces a plain
``site-packages`` tree, which is path-independent: prepend it to ``PYTHONPATH``
and it works under any mount point.

Why platform packages cannot be overridden
------------------------------------------
``sys.path`` is ordered ``'' -> PYTHONPATH -> stdlib -> site-packages``, so an
overlay entry ALWAYS shadows the image's own ``dagster`` / ``sqlmesh``. There is
no ordering that both makes user packages importable and protects the platform's
own. A DIRECT declaration of a platform package therefore has to be refused at
build time, with a message that names the version the platform ships — see
:func:`split_platform_provided` and :func:`detect_shadowed`.

The invariant that follows, and the whole point of this module
--------------------------------------------------------------
**An overlay never contains a platform distribution, except the ones the
platform explicitly yields.** Everything here exists to make that true by
construction rather than by inspection afterwards:

* a DIRECT declaration naming a platform package is either satisfied by the
  shipped version or refused with the version named
  (:func:`find_platform_conflicts`) — before any sandbox is started;
* TRANSITIVE resolution is pinned to the platform's versions
  (:func:`baseline_constraints`), so pip either resolves to exactly what the
  platform has — in which case the package is not installed at all — or reports
  an unsatisfiable conflict in its own words;
* what remains is asserted, not tolerated: a post-install overlap that was not
  yielded is a defect in the pipeline above, never something a user can cause
  (:func:`detect_shadowed`).

Why "yield" exists at all, and what may be yielded
--------------------------------------------------
Pinning all several hundred baseline distributions with ``==`` gives every one of
them a veto over every declaration. The platform image carries a whole data
stack, so its transitive closure is wide: ``tenacity``, ``tqdm``, ``tzdata`` are
pinned exactly as hard as ``numpy``, and a declaration of ``feast`` fails on the
first of them it happens to touch. The cost of being overridden is not remotely
the same, though:

* a COMPILED distribution (``numpy``, ``pyarrow``, ``pydantic-core``, ``grpcio``)
  or one the platform's own machinery is built on (:data:`PROTECTED_DISTRIBUTIONS`)
  breaks the platform itself at ABI level, inside a code location, with a
  traceback pointing at the user's asset;
* a pure-Python leaf (``tenacity``) carries API-drift risk only, and only for the
  one working copy whose overlay contains it.

So the pins are LAYERED: :func:`relaxable_baseline` derives, objectively, the
subset that may yield — everything with no compiled extension, minus the
protected names, plus/minus what an operator configured. The installer keeps the
full pin set for its first attempt and releases only the yielded pins that pip
itself named as parties to the conflict. Yielding is recorded
(:attr:`DepsManifest.relaxed`) rather than silent, because six months later "why
does this workspace behave differently" has to have an answer.

The one gap that no declaration check can close
-----------------------------------------------
A declared package can be perfectly compatible with the baseline while its OWN
dependencies are not — ``feast`` needs ``tenacity<9``, and the platform pins
``tenacity==9.1.4``. Nothing in the declaration mentions tenacity, so every
pre-flight above passes and the failure surfaces at the one point where the user
has least context: a pip resolver report, addressed to a "user" who is told they
*requested* a bound they never wrote.

That case is a :class:`TransitiveConflict`: the requirement as declared, the
baseline packages it collides with — each with the RANGE its dependency asks for
and who asks for it (:func:`blockers_from_resolution`) — and, when one exists,
the requirement rewritten so it resolves (:func:`relax_upper_bounds`). It
carries a rewrite rather than an explanation because it is the same shape a
direct :class:`PlatformConflict` is: one line in the file, one button.

Where the baseline comes from
-----------------------------
From the interpreter that CONSUMES the overlay, which is the workspace-runtime
process: Dagster code locations deliberately share it rather than getting a
per-workspace venv (see ``CodeLocationManager._spawn_grpc``). It is emphatically
NOT the interpreter that performs the install — that one lives in the task-runner
image, whose dependency set is a different, much smaller one. Asking the
installing interpreter what the platform provides answers a question nobody
asked, and answers it differently in dev (same process) and in Kubernetes
(different image). The baseline therefore travels with the install plan; see
:attr:`InstallPlan.platform_packages`.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import sysconfig
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from datacode_common.workspace_layout import PLATFORM_STATE_DIR

# ── layout ────────────────────────────────────────────────────────────────
#
# Under the platform state root, which is already hidden from the explorer,
# excluded from the @-mention index and covered by the generated .gitignore.
# The semantics of that root -- "written by the platform, regenerable" -- is
# exactly right for an installed dependency tree.
DEPS_DIR = f"{PLATFORM_STATE_DIR}/deps"
SITE_PACKAGES_DIRNAME = "site-packages"
MANIFEST_FILENAME = "manifest.json"
CACHE_DIRNAME = "cache"

# The file the user edits. Also the file Dagster already reads for
# ``[tool.dagster].module_name``, which is why no second convention is added.
DECLARATION_RELPATH = "asset-dag/pyproject.toml"

# Task kind of an install. Named here rather than at each call site because it
# is one string shared by four places that must agree: the isolation profile
# that claims it, the submitter that routes its egress, the runner's handler
# registry, and the log/progress channel.
TASK_KIND_DEPS_SYNC = "deps.sync"

# Plain-text transcript of the last install, written by the RUNNER and tailed by
# the runtime while the task runs. The progress channel is a file rather than the
# task protocol's stdout because both vehicles share the working copy while only
# one of them (the local backend) could stream stdout: a file gives dev and
# Kubernetes the same live progress, and it outlives the vehicle, so a user who
# reloads the page after a failure can still read what pip said.
INSTALL_LOG_FILENAME = "install.log"

# Bumped when the normalisation below changes. Part of the fingerprint, so a
# platform upgrade that changes normalisation forces one reinstall instead of
# leaving every working copy in a permanently "drifted" state.
FINGERPRINT_VERSION = 1

# An install is a user-triggered, foreground operation with a progress bar. A
# thousand requirements is not that; it is a mistake or an attempt to exhaust
# the copy's volume.
MAX_REQUIREMENTS = 200

# pip constraints derived from the platform baseline, written by the RUNNER next
# to the overlay it builds. A file rather than a pile of ``pkg==x`` arguments
# because pip only accepts constraints that way, and keeping it inside the copy
# means a user who asks "why did it resolve to that version" can read it.
CONSTRAINTS_FILENAME = "platform-constraints.txt"

# Where ``pip install --dry-run --report`` writes the resolution it would have
# installed. Written only while diagnosing a failed install, and read as JSON
# rather than scraped from pip's English: the resolver's own report is the only
# place the exact version a package WOULD have pulled in is stated.
RESOLUTION_REPORT_FILENAME = "resolution-report.json"

# ── manifest states ───────────────────────────────────────────────────────
#
# What the CONSUMER side concludes after reading the manifest. Shared with the
# frontend through the API response, so the strings are part of the contract.
STATE_READY = "ready"                # fingerprint matches, overlay usable
STATE_DRIFTED = "drifted"            # declaration changed since the install
STATE_MISSING = "missing"            # never installed (or overlay removed)
STATE_INCOMPATIBLE = "incompatible"  # built for another interpreter (see D4)
STATE_FAILED = "failed"              # last install failed; overlay unusable
STATE_EMPTY = "empty"                # nothing declared, nothing to install
# The declaration itself cannot be read. Distinct from every state above because
# the user's next action differs: fix the file, not press reload.
STATE_DECLARATION_INVALID = "declaration_invalid"

# ── failure codes ─────────────────────────────────────────────────────────
#
# One string per failure SHAPE, because the next action differs for each. They
# cross three boundaries verbatim — runner -> runtime -> reload job envelope ->
# UI translation key — so they are declared once here rather than re-typed at
# each of those four places, which is how "conflict with the platform" ended up
# rendered as "the file could not be read".
FAILURE_DECLARATION_INVALID = "deps_declaration_invalid"
FAILURE_PLATFORM_CONFLICT = "deps_platform_conflict"
# A declared package whose own dependencies clash with the baseline. Separated
# from FAILURE_PLATFORM_CONFLICT because the next action is different: a direct
# conflict is fixed by accepting the platform's version of the DECLARED package,
# a transitive one by moving to another release of it — or by not at all.
FAILURE_PLATFORM_TRANSITIVE = "deps_platform_transitive"
FAILURE_INSTALL_FAILED = "deps_install_failed"
FAILURE_SHADOW_PLATFORM = "deps_shadow_platform"
FAILURE_TOO_LARGE = "deps_too_large"
FAILURE_INDEX_NOT_CONFIGURED = "deps_index_not_configured"
# The index HAS a version the declaration accepts, but it publishes no wheel this
# interpreter can install. Its own code because every other failure sends the user
# to edit a version bound, and no bound can fix this one: the package ships only a
# source distribution (or wheels for other interpreters), and the install sandbox
# has no compiler and never runs a package's build script.
#
# Without this code the case is reported as FAILURE_INSTALL_FAILED with pip's
# "(from versions: none)" line, which reads as "the version you asked for does not
# exist" — a sentence that sent users to check a version number that was correct.
FAILURE_NO_WHEEL = "deps_no_wheel"

# ── packages that may never be shadowed ───────────────────────────────────
#
# Distributions whose replacement breaks the PLATFORM rather than only the
# workspace: the code-location server, the transformation engine, the contract
# types they exchange, and this library itself. They are never yielded, whatever
# an operator configures, and naming them turns a report into a diagnosis.
#
# This is a floor, not the whole set: :func:`relaxable_baseline` also withholds
# every distribution that ships a compiled extension, which is derived from the
# installed tree rather than listed here so the rule cannot rot as the image
# changes.
PROTECTED_DISTRIBUTIONS: frozenset[str] = frozenset(
    {
        # The code location server and the process that spawns it.
        "dagster",
        "dagster-graphql",
        "dagster-pipes",
        "dagster-webserver",
        "grpcio",
        "grpcio-health-checking",
        "protobuf",
        # The transformation engine and its own contract types.
        "sqlmesh",
        "sqlglot",
        "pydantic",
        "pydantic-core",
        "sqlalchemy",
        # The platform's shared library: shadowing it would replace the very
        # contracts this module defines.
        "datacode-common",
    }
)

# Suffixes of a compiled extension module, on every platform the images target.
# A distribution containing one is bound to the interpreter's ABI, so a version
# other than the platform's own can break the platform's code at import time —
# which is the objective half of the "may this pin yield?" question.
_EXTENSION_SUFFIXES = (".so", ".pyd", ".dylib")


class DepsDeclarationError(ValueError):
    """The declaration cannot be turned into an install plan.

    Raised for malformed TOML, a malformed PEP 508 requirement, a direct URL
    reference, or an implausible number of requirements. The message is shown
    to the user verbatim, so it always names the offending line.
    """


@dataclass(frozen=True)
class PlatformConflict:
    """One declared requirement the platform baseline cannot satisfy.

    A record rather than a sentence because three consumers need the parts:
    the editor marks the line, the UI offers "rewrite to the platform version",
    and the CLI prints it. Only :meth:`message` turns it into prose.

    Attributes:
        requirement: The normalised requirement, exactly as declared.
        name: Canonical distribution name.
        platform_version: What the platform actually ships.
        suggested: The requirement rewritten to accept the shipped version.
            Deliberately ``>=`` rather than ``==``: the platform baseline moves
            with the image, and a pin would turn today's fix into tomorrow's
            conflict.
    """

    requirement: str
    name: str
    platform_version: str

    @property
    def suggested(self) -> str:
        return f"{self.name}>={self.platform_version}"

    def message(self) -> str:
        return (
            f"{self.requirement!r} cannot be installed: the platform provides "
            f"{self.name}=={self.platform_version}, and a workspace overlay "
            f"always shadows platform packages instead of coexisting with "
            f"them. Declare {self.suggested!r} to use the version the platform "
            f"ships, or ask an operator to upgrade the platform baseline."
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "requirement": self.requirement,
            "name": self.name,
            "platform_version": self.platform_version,
            "suggested": self.suggested,
        }


class PlatformCoreConflict(ValueError):
    """A declaration asks for a version of a package the platform itself ships.

    Carries the structured facts the UI and the CLI both render, instead of
    only a sentence: which requirements, what the platform has, what to write
    instead.
    """

    def __init__(self, conflicts: Iterable[PlatformConflict]):
        self.conflicts = tuple(conflicts)
        if not self.conflicts:
            raise ValueError("PlatformCoreConflict needs at least one conflict")
        # Every conflict is reported, not just the first: fixing them one
        # reload at a time is the difference between one edit and four.
        super().__init__("\n".join(item.message() for item in self.conflicts))

    @property
    def first(self) -> PlatformConflict:
        return self.conflicts[0]

    def to_dict(self) -> dict[str, Any]:
        return {"conflicts": [item.to_dict() for item in self.conflicts]}


@dataclass(frozen=True)
class TransitiveConflict:
    """A declared package whose own dependencies the baseline cannot satisfy.

    Distinct from :class:`PlatformConflict` in what the user must change. A
    direct conflict is about the declared package itself and is fixed by
    accepting the platform's version of it; this one is about a package the
    declaration never mentions, and is fixed by moving to another RELEASE of the
    declared package — or not at all, when no release fits the baseline.

    Attributes:
        requirement: The normalised requirement, exactly as declared.
        name: Canonical name of the DECLARED package (the one whose line in
            ``pyproject.toml`` has to change), never the blocking one.
        blockers: ``{name, required, platform_version, required_by}`` per
            baseline package this declaration genuinely cannot accept.
            ``required`` is the RANGE some dependency states (``>=7,<9``), not a
            version — a version would be whatever the index happened to have
            newest, which is how ten packages were once reported as blockers
            when only one was. ``required_by`` names the dependency that states
            it, since the declaration does not mention it anywhere.
        suggested: The requirement rewritten so it resolves against the
            baseline — the declared requirement with its ceilings removed, and
            the only edit that is known to work because pip resolved it while
            the failure was being diagnosed. Empty when no release fits.
        resolved_version: What ``suggested`` resolves to, so the user is told
            which version they are getting rather than asked to trust a range.
        protected: Canonical names among ``blockers`` the platform will never
            yield (compiled extensions and :data:`PROTECTED_DISTRIBUTIONS`).
            Non-empty is what makes "ask an operator" the honest answer rather
            than a dead end the user cannot see the shape of.
    """

    requirement: str
    name: str
    blockers: tuple[Mapping[str, str], ...] = ()
    suggested: str = ""
    resolved_version: str = ""
    protected: tuple[str, ...] = ()

    @property
    def fixable(self) -> bool:
        """Whether an edit exists that is known to resolve.

        False means the honest answer is "no release of this package works with
        this platform baseline", which is a request to an operator, not a
        puzzle for the user.
        """
        return bool(self.suggested)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "name": self.name,
            "blockers": [dict(item) for item in self.blockers],
            "suggested": self.suggested,
            "resolved_version": self.resolved_version,
            "protected": list(self.protected),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TransitiveConflict":
        """Rebuild one from a transported payload, tolerating junk.

        This crosses a version boundary (runner -> runtime -> UI), so a record
        this build cannot fully read is dropped rather than allowed to raise:
        the prose message still reaches the user, which is the part that must
        not be lost.
        """
        blockers: list[dict[str, str]] = []
        raw = data.get("blockers")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, Mapping) and item.get("name"):
                    blockers.append(
                        {
                            "name": str(item["name"]),
                            "required": str(item.get("required") or ""),
                            "platform_version": str(
                                item.get("platform_version") or ""
                            ),
                            "required_by": str(item.get("required_by") or ""),
                        }
                    )
        protected = data.get("protected")
        return cls(
            requirement=str(data.get("requirement") or ""),
            name=str(data.get("name") or ""),
            blockers=tuple(blockers),
            suggested=str(data.get("suggested") or ""),
            resolved_version=str(data.get("resolved_version") or ""),
            protected=tuple(
                str(item) for item in protected if item
            ) if isinstance(protected, list) else (),
        )

    def message(self) -> str:
        parts = [f"{self.requirement!r} cannot be installed: "]
        if self.blockers:
            needed = ", ".join(
                f"{item['name']} {item['required']}".strip()
                for item in self.blockers
            )
            pinned = ", ".join(
                f"{item['name']}=={item['platform_version']}"
                for item in self.blockers
            )
            parts.append(
                f"one of its dependencies needs {needed}, while the platform "
                f"provides {pinned}. A workspace overlay is loaded before the "
                f"platform's own packages, so it would replace them rather "
                f"than coexist."
            )
        else:
            parts.append(
                "no version of it resolves together with the platform baseline."
            )
        if self.fixable:
            version = f" (resolves to {self.resolved_version})" if (
                self.resolved_version
            ) else ""
            parts.append(f" Declare {self.suggested!r}{version} instead.")
        elif self.protected:
            # Named, with the reason they are the one class of pin that cannot
            # move: an operator can act on this, and the user now knows which
            # package to name when asking.
            parts.append(
                " "
                + ", ".join(self.protected)
                + " is part of the platform's own runtime (a compiled "
                "extension or a component the platform is built on), so its "
                "version cannot be yielded to a workspace. The fix is an "
                "upgrade of the platform baseline — ask an operator."
            )
        else:
            parts.append(
                " No release of it fits this platform baseline, so the fix is "
                "an upgrade of the baseline itself — ask an operator."
            )
        return "".join(parts)


@dataclass(frozen=True)
class SourceOnlyRequirement:
    """A requirement the index satisfies, but only with a source distribution.

    The third shape of "cannot be installed", and the only one no edit to the
    declaration can fix. A :class:`PlatformConflict` is fixed by accepting the
    platform's version, a :class:`TransitiveConflict` by another release of the
    declared package — here the version the user asked for exists and is exactly
    the one they want; what is missing is a wheel, and the install sandbox
    installs nothing else (no compiler, and no build script is ever executed).

    Attributes:
        requirement: The requirement as pip reported it unsatisfiable.
        name: Canonical distribution name.
        versions: Versions on the index that DO satisfy the requirement, newest
            last. Carried because "the index has 4.0.1, 3.5.7, ..." is what turns
            "no matching distribution" from a contradiction into a diagnosis.
    """

    requirement: str
    name: str
    versions: tuple[str, ...] = ()

    @property
    def newest(self) -> str:
        return self.versions[-1] if self.versions else ""

    def message(self) -> str:
        have = f" (the index has {self.newest})" if self.newest else ""
        return (
            f"{self.requirement!r} cannot be installed: a matching version "
            f"exists{have}, but it publishes no wheel this platform can install "
            f"— only a source distribution, or wheels for other interpreters. "
            f"Dependencies are installed from wheels only: the install sandbox "
            f"has no compiler toolchain and never runs a package's build "
            f"script. Either declare a package that ships wheels, or ask an "
            f"operator to add {self.name} to the platform image (it is then "
            f"importable without being declared) or to allow source builds for "
            f"it (deps.allow_source_packages)."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "name": self.name,
            "versions": list(self.versions),
        }


class ShadowedPlatformPackage(ValueError):
    """A finished overlay contains a platform distribution nobody yielded.

    With the baseline pinned as pip constraints this is unreachable through any
    declaration: resolution can only land on the platform's own version, and a
    package resolved to that version is dropped from the overlay rather than
    kept. The one legitimate exception — a pin the platform deliberately
    yielded, see :func:`relaxable_baseline` — is passed to
    :func:`detect_shadowed` and excluded there. So reaching this means either
    the constraints did not travel or a yield was not recorded: a deployment
    defect either way, which fails the install instead of being logged as a
    curiosity.
    """

    def __init__(self, names: Iterable[str]):
        self.names = tuple(sorted(names))
        joined = ", ".join(self.names)
        super().__init__(
            f"the installed tree contains {joined}, which the platform itself "
            f"provides. An overlay always shadows the platform's copy, so this "
            f"must never happen: the platform baseline did not reach the "
            f"installer. Report this — no declaration can cause it."
        )


@dataclass(frozen=True)
class DepsDeclaration:
    """A normalised, ordered set of requirements plus its fingerprint.

    Attributes:
        requirements: Normalised PEP 508 strings, sorted. Normalisation makes
            the fingerprint stable against reordering, case and whitespace, so
            reformatting ``pyproject.toml`` does not trigger a reinstall.
        fingerprint: ``sha256`` over the normalised form. The single value the
            consumer side compares against the manifest.
    """

    requirements: tuple[str, ...]
    fingerprint: str

    @property
    def is_empty(self) -> bool:
        return not self.requirements


@dataclass(frozen=True)
class DepsManifest:
    """The authoritative description of what is actually installed.

    Written to ``.datacode/deps/manifest.json`` by the runtime after an
    install, and read by every consumer before it puts the overlay on
    ``sys.path``.

    Attributes:
        state: :data:`STATE_READY` or :data:`STATE_FAILED`. The other states
            are conclusions a reader draws by comparing this record with the
            current declaration — they are never stored.
        fingerprint: Fingerprint of the declaration this tree was built from.
        packages: ``{name, version}`` actually installed into the overlay.
        platform_provided: ``{name, version}`` skipped because the platform
            already satisfies them.
        shadowed: ``{name, version}`` installed even though the platform also
            has them — allowed, but worth surfacing when something behaves
            oddly.
        relaxed: ``{name, platform_version, overlay_version, required_by}`` per
            baseline pin the platform YIELDED so this declaration could
            install. Recorded rather than silent: this is the only trace of why
            one working copy behaves differently from the platform's own
            processes, and without it that difference is undiagnosable.
        python_version: ``major.minor`` of the interpreter that BUILT the tree.
            Compared before injection: a tree built by 3.13 must not be put on
            a 3.11 interpreter's path, where compiled extensions would fail to
            import with an error nobody can read.
        platform_tag: ``sysconfig.get_platform()`` of the builder, same reason.
        installed_at: UTC ISO-8601. Storage and comparison are always UTC; the
            frontend converts to the user's timezone.
        source: ``ui`` | ``cli`` | ``reload`` — what triggered the install.
        actor_user_id: Who triggered it, for audit.
        size_bytes: On-disk size of the overlay, to make a growing working copy
            explainable.
        error_code / error_detail: Set when ``state`` is failed, so a reader
            can explain the unusable overlay without consulting a log.
    """

    state: str
    fingerprint: str
    packages: tuple[Mapping[str, str], ...] = ()
    platform_provided: tuple[Mapping[str, str], ...] = ()
    shadowed: tuple[Mapping[str, str], ...] = ()
    relaxed: tuple[Mapping[str, str], ...] = ()
    python_version: str = ""
    platform_tag: str = ""
    installed_at: str = ""
    source: str = ""
    actor_user_id: str = ""
    size_bytes: int = 0
    error_code: str = ""
    error_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": FINGERPRINT_VERSION,
            "state": self.state,
            "fingerprint": self.fingerprint,
            "packages": [dict(item) for item in self.packages],
            "platform_provided": [dict(item) for item in self.platform_provided],
            "shadowed": [dict(item) for item in self.shadowed],
            "relaxed": [dict(item) for item in self.relaxed],
            "python_version": self.python_version,
            "platform_tag": self.platform_tag,
            "installed_at": self.installed_at,
            "source": self.source,
            "actor_user_id": self.actor_user_id,
            "size_bytes": self.size_bytes,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DepsManifest":
        """Rebuild a manifest, tolerating anything a future version added.

        Unknown keys are ignored and missing keys default, because a manifest
        is cached state: the correct reaction to one this build cannot fully
        read is to treat it as drifted and reinstall, never to crash a reload.
        """
        return cls(
            state=str(data.get("state") or STATE_FAILED),
            fingerprint=str(data.get("fingerprint") or ""),
            packages=tuple(_name_version_list(data.get("packages"))),
            platform_provided=tuple(_name_version_list(data.get("platform_provided"))),
            shadowed=tuple(_name_version_list(data.get("shadowed"))),
            relaxed=tuple(_relaxed_list(data.get("relaxed"))),
            python_version=str(data.get("python_version") or ""),
            platform_tag=str(data.get("platform_tag") or ""),
            installed_at=str(data.get("installed_at") or ""),
            source=str(data.get("source") or ""),
            actor_user_id=str(data.get("actor_user_id") or ""),
            size_bytes=int(data.get("size_bytes") or 0),
            error_code=str(data.get("error_code") or ""),
            error_detail=str(data.get("error_detail") or ""),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str)

    @classmethod
    def from_json(cls, payload: str) -> "DepsManifest":
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("deps manifest must be a JSON object")
        return cls.from_dict(data)


@dataclass(frozen=True)
class InstallPlan:
    """What the sandbox is asked to install, as task parameters.

    Built on the runtime side from a declaration and handed to the runner in
    ``TaskSpec.params``. The runner never parses ``pyproject.toml`` itself:
    parsing is where the platform-conflict rules live, and refusing an
    operation with an actionable message belongs on the side that owns the API.

    Both sides go through :meth:`to_params` / :meth:`from_params` so the
    parameter names exist once.
    """

    requirements: tuple[str, ...]
    fingerprint: str
    # ``name -> version`` of everything the CODE LOCATION interpreter provides.
    #
    # Travels with the plan because the installer cannot work it out: pip runs
    # in the task-runner image, whose package set is a different and much
    # smaller one, so introspecting it answers a question nobody asked — and
    # answers it identically in dev (same interpreter) and wrongly in
    # Kubernetes (different image), which is the worst possible combination for
    # noticing. Empty is never valid; the runner refuses rather than concluding
    # "the platform provides nothing" and installing a shadow of everything.
    platform_packages: tuple[tuple[str, str], ...] = ()
    # Baseline pins the platform is willing to YIELD when — and only when —
    # keeping them makes the declaration unsatisfiable. Derived on the runtime
    # side by :func:`relaxable_baseline`, which needs the installed tree to tell
    # a compiled distribution from a pure-Python one; the installer cannot
    # compute it for the same reason it cannot compute the baseline.
    #
    # Default empty = nothing may yield, which is the pre-existing behaviour and
    # the fail-closed direction: an installer talking to an older runtime
    # refuses a declaration it might have installed, rather than overwriting a
    # platform package nobody approved.
    relaxable_packages: tuple[str, ...] = ()
    max_bytes: int = 0
    # Where packages may come from. Carried in the spec rather than read from
    # the runner's environment because the vehicles build that environment
    # differently (the local backend clears it entirely), and a setting that
    # only survives in one of them is a setting that silently does not apply.
    #
    # A URL with embedded credentials is REFUSED by the runtime before it gets
    # here: the spec is written to audit records and, on Kubernetes, to a
    # ConfigMap. See ``TaskSpec``'s own credential check for the same rule.
    index_url: str = ""
    extra_index_urls: tuple[str, ...] = ()
    trusted_hosts: tuple[str, ...] = ()
    # Declared packages an OPERATOR allows to be built from a source
    # distribution, as canonical names (``deps.allow_source_packages``). Empty by
    # default, which is the shipped behaviour: wheels only.
    #
    # The escape hatch for a package that publishes no wheel at all — pyspark is
    # the canonical example, it ships one 300 MB sdist that merely unpacks bundled
    # jars. Opting in re-admits the one code path the wheel-only rule exists to
    # close (a build backend running inside the install sandbox), so it is a
    # deployment decision, per package, never something a working copy can ask for.
    allow_source_packages: tuple[str, ...] = ()

    @property
    def source_allowed(self) -> frozenset[str]:
        """Canonical names this deployment allows to build from source."""
        return frozenset(self.allow_source_packages)

    @property
    def baseline(self) -> dict[str, str]:
        """The platform baseline as a lookup."""
        return {name: version for name, version in self.platform_packages}

    @property
    def relaxable(self) -> frozenset[str]:
        """Canonical names whose baseline pin may be released on conflict."""
        return frozenset(self.relaxable_packages)

    def to_params(self) -> dict[str, Any]:
        return {
            "requirements": list(self.requirements),
            "fingerprint": self.fingerprint,
            "platform_packages": [list(pair) for pair in self.platform_packages],
            "relaxable_packages": list(self.relaxable_packages),
            "max_bytes": self.max_bytes,
            "index_url": self.index_url,
            "extra_index_urls": list(self.extra_index_urls),
            "trusted_hosts": list(self.trusted_hosts),
            "allow_source_packages": list(self.allow_source_packages),
        }

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "InstallPlan":
        raw = params.get("requirements")
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise DepsDeclarationError(
                "deps.sync params.requirements must be a list of normalised "
                "requirement strings"
            )
        relaxable = params.get("relaxable_packages")
        return cls(
            requirements=tuple(raw),
            fingerprint=str(params.get("fingerprint") or ""),
            platform_packages=_baseline_pairs(params.get("platform_packages")),
            relaxable_packages=tuple(
                sorted(
                    {
                        canonicalize_name(str(item))
                        for item in relaxable
                        if item
                    }
                )
            ) if isinstance(relaxable, list) else (),
            max_bytes=int(params.get("max_bytes") or 0),
            index_url=str(params.get("index_url") or ""),
            extra_index_urls=tuple(
                str(item) for item in (params.get("extra_index_urls") or ())
            ),
            trusted_hosts=tuple(
                str(item) for item in (params.get("trusted_hosts") or ())
            ),
            allow_source_packages=_canonical_names(
                params.get("allow_source_packages")
            ),
        )


def _canonical_names(value: Any) -> tuple[str, ...]:
    """Coerce a transported list of distribution names, sorted and canonical.

    Anything unreadable becomes the empty tuple rather than a partial list: every
    consumer of this shape treats "no names" as the restrictive default, so a
    malformed payload must not be able to widen a rule.
    """
    if not isinstance(value, list):
        return ()
    return tuple(
        sorted({canonicalize_name(str(item)) for item in value if item})
    )


def _baseline_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    """Coerce the transported baseline, dropping anything malformed.

    Sorted, so a plan round-trips to an equal object and two runs of the same
    declaration produce the same constraints file.
    """
    pairs: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        items: Iterable[Any] = value.items()
    elif isinstance(value, list):
        items = value
    else:
        return ()
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            name, version = item
            if name:
                pairs.append((canonicalize_name(str(name)), str(version or "")))
    return tuple(sorted(set(pairs)))


# ── declaration ───────────────────────────────────────────────────────────


def parse_declaration(pyproject_text: str) -> DepsDeclaration:
    """Parse and normalise ``[project].dependencies`` from a pyproject file.

    Args:
        pyproject_text: Raw contents of ``asset-dag/pyproject.toml``.

    Returns:
        The normalised declaration. A file with no dependencies yields an
        empty declaration (with a stable fingerprint), which is a valid state
        and not an error.

    Raises:
        DepsDeclarationError: The TOML is malformed, ``dependencies`` is not a
            list of strings, a requirement is not valid PEP 508, a requirement
            uses a direct URL, or there are implausibly many of them.
    """
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError as exc:
        raise DepsDeclarationError(
            f"{DECLARATION_RELPATH} is not valid TOML: {exc}"
        ) from exc

    project = data.get("project")
    if project is None:
        return _declaration_from(())
    if not isinstance(project, dict):
        raise DepsDeclarationError(
            f"{DECLARATION_RELPATH}: [project] must be a table"
        )

    raw = project.get("dependencies", [])
    if raw in (None, []):
        return _declaration_from(())
    if not isinstance(raw, list):
        raise DepsDeclarationError(
            f"{DECLARATION_RELPATH}: [project].dependencies must be a list of "
            f"requirement strings, got {type(raw).__name__}"
        )
    if len(raw) > MAX_REQUIREMENTS:
        raise DepsDeclarationError(
            f"{DECLARATION_RELPATH}: {len(raw)} dependencies declared, the "
            f"limit is {MAX_REQUIREMENTS}"
        )

    normalised: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise DepsDeclarationError(
                f"{DECLARATION_RELPATH}: [project].dependencies entries must be "
                f"strings, found {type(entry).__name__}"
            )
        normalised.append(_normalise_requirement(entry))

    return _declaration_from(normalised)


def _declaration_from(requirements: Iterable[str]) -> DepsDeclaration:
    """Sort, de-duplicate and fingerprint a set of normalised requirements."""
    unique = tuple(sorted(set(requirements)))
    payload = f"v{FINGERPRINT_VERSION}\n" + "\n".join(unique)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return DepsDeclaration(requirements=unique, fingerprint=digest)


def _normalise_requirement(raw: str) -> str:
    """Return a canonical PEP 508 string for one declared requirement.

    Canonical means: lowercased/dash-normalised distribution name, sorted
    extras, sorted version specifiers. Two declarations that mean the same
    thing must fingerprint the same, otherwise an innocuous edit reinstalls
    everything.
    """
    text = raw.strip()
    if not text:
        raise DepsDeclarationError(
            f"{DECLARATION_RELPATH}: empty requirement string"
        )
    try:
        req = Requirement(text)
    except InvalidRequirement as exc:
        raise DepsDeclarationError(
            f"{DECLARATION_RELPATH}: {text!r} is not a valid requirement: {exc}"
        ) from exc

    if req.url:
        # A direct reference is a fetch from a host of the user's choosing.
        # The sandbox's egress allow-list is derived from PLATFORM
        # configuration precisely so that a file in the working copy cannot
        # widen it; accepting a URL here would hand that back.
        raise DepsDeclarationError(
            f"{DECLARATION_RELPATH}: {text!r} points at a URL. Only packages "
            f"from the platform's package index can be installed; declare a "
            f"name and version range instead."
        )

    name = canonicalize_name(req.name)
    extras = (
        "[" + ",".join(sorted(canonicalize_name(extra) for extra in req.extras)) + "]"
        if req.extras
        else ""
    )
    specifier = ",".join(sorted(str(spec) for spec in req.specifier))
    marker = f"; {req.marker}" if req.marker else ""
    return f"{name}{extras}{specifier}{marker}"


def requirement_name(requirement: str) -> str:
    """Canonical distribution name of a normalised requirement string."""
    return canonicalize_name(Requirement(requirement).name)


# Specifier operators that cap how new a version may be. Dropping them is the
# only edit that can make an unsatisfiable declaration satisfiable without
# asking the user to guess a version: everything else (``>=``, ``==``, ``!=``)
# states a requirement they actually have.
_UPPER_BOUND_OPERATORS = frozenset({"<", "<=", "~="})


def relax_upper_bounds(requirement: str) -> str:
    """The same requirement with every ceiling removed.

    ``feast[postgres]<0.35.0,>=0.34.0`` becomes ``feast[postgres]>=0.34.0``:
    the lower bound is what the user needs, the upper one is usually a habit or
    a pin copied from a requirements file, and it is the half that collides with
    the platform baseline (an old ceiling asks for old transitive dependencies).

    Returns the normalised form, so the result can be compared with the input
    to tell whether relaxing was even possible — a declaration with no ceiling
    cannot be relaxed, and probing it again would only repeat the failure.
    """
    req = Requirement(requirement)
    kept = [
        spec for spec in req.specifier if spec.operator not in _UPPER_BOUND_OPERATORS
    ]
    name = canonicalize_name(req.name)
    extras = (
        "[" + ",".join(sorted(canonicalize_name(extra) for extra in req.extras)) + "]"
        if req.extras
        else ""
    )
    specifier = ",".join(sorted(str(spec) for spec in kept))
    marker = f"; {req.marker}" if req.marker else ""
    return f"{name}{extras}{specifier}{marker}"


# The two sentences pip uses when it found no installable distribution. BOTH are
# needed: the first is printed by the resolver, the second by the installer, and
# which one appears depends on where in the run pip gave up.
#
#   ERROR: Could not find a version that satisfies the requirement pyspark>=3.5.0
#          (from versions: none)
#   ERROR: No matching distribution found for pyspark>=3.5.0
#
# Note what the message does NOT say: whether the version is absent from the
# index, or present but filtered out by ``--only-binary=:all:``. "from versions:
# none" is printed for both, because the candidates were filtered before the list
# was built — which is why recognising this shape is only the first half of a
# diagnosis (see the runner's index probe).
_MISSING_DISTRIBUTION_RE = re.compile(
    r"(?:could not find a version that satisfies the requirement"
    r"|no matching distribution found for)\s+(?P<requirement>[^\s()]+)",
    re.IGNORECASE,
)


def is_missing_distribution(output: str) -> bool:
    """Whether pip failed because it found nothing installable to download."""
    return bool(_MISSING_DISTRIBUTION_RE.search(output))


def missing_distribution_requirements(output: str) -> tuple[str, ...]:
    """Requirements pip reported as having no installable distribution.

    Read from pip's output rather than from the declaration, because the
    unsatisfiable requirement is frequently NOT one the user wrote: it can be a
    transitive dependency, and its specifier then exists nowhere else.

    Returns normalised requirement strings, first mention first, de-duplicated.
    Tokens that are not valid PEP 508 are skipped: this parses English, so it
    must degrade to "no diagnosis" rather than to a wrong one.
    """
    found: list[str] = []
    for match in _MISSING_DISTRIBUTION_RE.finditer(output):
        try:
            requirement = _normalise_requirement(match.group("requirement"))
        except DepsDeclarationError:
            continue
        if requirement not in found:
            found.append(requirement)
    return tuple(found)


def versions_matching(requirement: str, versions: Iterable[str]) -> tuple[str, ...]:
    """The versions in *versions* that satisfy *requirement*, oldest first.

    Sorted by version rather than by the order the index listed them, so the last
    element is the newest match and can be named in a message. Unparsable
    versions are dropped.
    """
    try:
        req = Requirement(requirement)
    except InvalidRequirement:
        return ()
    parsed: list[Version] = []
    for raw in versions:
        try:
            candidate = Version(str(raw).strip())
        except InvalidVersion:
            continue
        # prereleases=True for the same reason as :func:`_satisfies`: an index
        # that only has a release candidate still HAS the package, and reporting
        # "nothing matches" would hide the actual problem.
        if req.specifier.contains(candidate, prereleases=True) or not str(
            req.specifier
        ):
            parsed.append(candidate)
    return tuple(str(item) for item in sorted(parsed))


def blockers_from_resolution(
    entries: Iterable[Mapping[str, Any]],
    baseline: Mapping[str, str],
) -> tuple[Mapping[str, str], ...]:
    """Baseline packages a resolution genuinely cannot accept.

    A blocker is a baseline package P for which some package in the resolution
    states a dependency RANGE that the platform's version of P falls outside
    of. That is the only definition that matches what pip refused.

    What this is NOT, and why it matters: "every baseline package whose version
    differs from the resolved one". The resolution being read here is the
    UNCONSTRAINED one, where pip floats everything to the newest release on the
    index — so a compare-by-version rule reports ``numpy 2.5.3 vs 2.5.2`` as a
    blocker of a package that asked for ``numpy>=2.0.0``. It once turned a
    single real conflict (``tenacity<9`` against ``tenacity==9.1.4``) into a
    list of ten, nine of which the platform satisfied perfectly, and a verdict
    of "no version of this package fits the platform" that was simply untrue.

    Args:
        entries: pip's own ``install`` entries from ``--report``. Each
            ``metadata`` is PEP 566 core metadata, so it carries
            ``requires_dist`` — which is where the range lives. Read from that
            JSON rather than from pip's English, which differs between
            releases.
        baseline: ``canonical name -> version`` the code-location interpreter
            provides.

    Returns:
        One ``{name, required, platform_version, required_by}`` per blocking
        baseline package, sorted by name. ``required`` is the range;
        ``required_by`` is the package that states it, which the declaration
        never mentions and the user therefore cannot guess.
    """
    resolved: dict[str, str] = {}
    for entry in entries if isinstance(entries, list) else list(entries):
        metadata = entry.get("metadata") if isinstance(entry, Mapping) else None
        if not isinstance(metadata, Mapping):
            continue
        raw_name = metadata.get("name")
        if raw_name:
            resolved[canonicalize_name(str(raw_name))] = str(
                metadata.get("version") or ""
            )

    found: dict[str, dict[str, str]] = {}
    saw_requires = False
    for entry in entries if isinstance(entries, list) else list(entries):
        metadata = entry.get("metadata") if isinstance(entry, Mapping) else None
        if not isinstance(metadata, Mapping):
            continue
        holder = canonicalize_name(str(metadata.get("name") or "")) or "?"
        requires = metadata.get("requires_dist")
        if not isinstance(requires, list):
            continue
        saw_requires = True
        for raw in requires:
            if not isinstance(raw, str):
                continue
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            name = canonicalize_name(req.name)
            # Only dependencies that were ACTUALLY pulled in count. This is how
            # extra- and marker-gated requirements are filtered without
            # evaluating markers whose environment (which extras were asked
            # for) is not in the report: a package the resolver did not install
            # is one whose requirement did not apply.
            if name not in resolved or name not in baseline:
                continue
            platform_version = baseline[name]
            if not str(req.specifier) or _satisfies(req, platform_version):
                continue
            # First stater wins. Several packages can bound the same dependency;
            # naming one is enough to make the conflict concrete, and the range
            # is what the user acts on either way.
            found.setdefault(
                name,
                {
                    "name": name,
                    "required": str(req.specifier),
                    "platform_version": platform_version,
                    "required_by": holder,
                },
            )

    if found or saw_requires:
        return tuple(found[name] for name in sorted(found))

    # No report carried dependency metadata at all (a pip too old to emit it).
    # Fall back to the only version comparison that is evidence rather than
    # noise: a resolution that moved a baseline package BACKWARDS can only have
    # done so because something stated a ceiling.
    for name, version in sorted(resolved.items()):
        platform_version = baseline.get(name)
        if not platform_version or not version or version == platform_version:
            continue
        if not _is_older(version, platform_version):
            continue
        found[name] = {
            "name": name,
            "required": f"<={version}",
            "platform_version": platform_version,
            "required_by": "",
        }
    return tuple(found[name] for name in sorted(found))


def _is_older(candidate: str, reference: str) -> bool:
    """Whether *candidate* is a strictly lower version than *reference*."""
    try:
        return Version(candidate) < Version(reference)
    except InvalidVersion:
        return False


# ── platform packages ─────────────────────────────────────────────────────


def find_platform_conflicts(
    declaration: DepsDeclaration,
    baseline: Mapping[str, str],
) -> tuple[PlatformConflict, ...]:
    """Every declared requirement the baseline cannot satisfy.

    Pure, microsecond-cheap and free of IO, which is what lets the SAME check
    run in three places: the editor's save-time validation, the runtime before
    it starts a sandbox, and the runner before it starts pip. A conflict found
    while typing costs one edit; the same conflict found by a reload costs a
    full install cycle, which is the difference this function exists to close.

    Args:
        declaration: Normalised declaration.
        baseline: ``canonical name -> version`` the code-location interpreter
            provides. See the module docstring on where this comes from.

    Returns:
        The conflicts, in declaration order. Empty means the declaration is
        compatible with the baseline. ALL of them, never just the first.
    """
    conflicts: list[PlatformConflict] = []
    for requirement in declaration.requirements:
        req = Requirement(requirement)
        name = canonicalize_name(req.name)
        platform_version = baseline.get(name)
        if platform_version is None or _satisfies(req, platform_version):
            continue
        conflicts.append(PlatformConflict(requirement, name, platform_version))
    return tuple(conflicts)


def split_platform_provided(
    declaration: DepsDeclaration,
    baseline: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[Mapping[str, str], ...]]:
    """Split a declaration into "must install" and "the platform has it".

    Args:
        declaration: Normalised declaration.
        baseline: ``canonical name -> version`` the code-location interpreter
            provides.

    Returns:
        ``(to_install, platform_provided)``. A platform-provided requirement is
        NOT passed to pip: it is already importable from the interpreter that
        will load the user's code, and installing a second copy would shadow
        the platform's with a byte-identical tree for no benefit.

    Raises:
        PlatformCoreConflict: One or more declared packages exist on the
            platform in a version their specifier rejects. Refused rather than
            installed, because the overlay would shadow the platform's copy.
    """
    conflicts = find_platform_conflicts(declaration, baseline)
    if conflicts:
        raise PlatformCoreConflict(conflicts)

    to_install: list[str] = []
    provided: list[Mapping[str, str]] = []
    for requirement in declaration.requirements:
        name = requirement_name(requirement)
        platform_version = baseline.get(name)
        if platform_version is None:
            to_install.append(requirement)
        else:
            provided.append({"name": name, "version": platform_version})
    return tuple(to_install), tuple(provided)


def _satisfies(req: Requirement, version: str) -> bool:
    """Whether *version* satisfies *req*'s specifier.

    An unparsable platform version (a local build marker, an editable install)
    is treated as satisfying an unconstrained requirement only: guessing would
    either install a shadow or refuse a legitimate declaration.
    """
    if not str(req.specifier):
        return True
    try:
        parsed = Version(version)
    except InvalidVersion:
        return False
    # prereleases=True: a platform baseline pinned to a release candidate must
    # still count as satisfying ``pkg>=1.0``, otherwise every declaration
    # mentioning it is refused for a reason the user cannot act on.
    return req.specifier.contains(parsed, prereleases=True)


def baseline_constraints(
    baseline: Mapping[str, str],
    *,
    exclude: Iterable[str] = (),
) -> str:
    """Render the baseline as a pip constraints file.

    This is what makes the invariant hold for TRANSITIVE resolution, which no
    amount of checking the declaration can reach. pip's ``--constraint``
    semantics are exactly the rule wanted: a constraint never causes an
    install, but any package that IS resolved must satisfy it. So a dependency
    of a declared package either lands on the platform's own version — and is
    then dropped from the overlay as equivalent — or makes pip report an
    unsatisfiable resolution, in its own words, naming both bounds.

    Without this a declaration of ``polars`` alone can put numpy 2.x in the
    overlay, shadow the platform's numpy, and break pandas at ABI level inside
    a code location, with the traceback pointing at the user's asset.

    Args:
        baseline: ``canonical name -> version`` the platform provides.
        exclude: Canonical names to leave UNPINNED — the pins the platform
            yielded for this install (see :func:`relaxable_baseline`). Omitting
            a pin is what lets the resolver pick the version a declaration's
            dependency needs; the omission is deliberate, bounded to names the
            platform approved, and recorded in the manifest afterwards.

    Versions no index can serve are skipped, because a constraint pins the
    resolver to something it must then be able to find:

    * unparsable versions, which match nothing;
    * LOCAL versions (``1.0+local.build``) — the platform's own shared library
      is installed from a directory, never published, so pinning it would make
      every install unsatisfiable the moment anything mentioned it.

    Skipping is safe: a package in the baseline is never installed into the
    overlay anyway (:func:`split_platform_provided` drops it), so the pin only
    ever mattered for a transitive resolution of a package that is not on an
    index in the first place.
    """
    dropped = {canonicalize_name(name) for name in exclude}
    lines = [
        "# Generated from the platform baseline — do not edit.",
        "# These pins do not install anything; they forbid the resolver from",
        "# choosing a version other than the one the code location already has.",
    ]
    if dropped:
        lines.append(
            "# Deliberately NOT pinned for this install, because keeping them "
            "made the"
        )
        lines.append(
            "# declaration unsatisfiable: " + ", ".join(sorted(dropped))
        )
    for name, version in sorted(baseline.items()):
        if not version or name in dropped:
            continue
        try:
            parsed = Version(version)
        except InvalidVersion:
            continue
        if parsed.local:
            continue
        lines.append(f"{name}=={version}")
    return "\n".join(lines) + "\n"


def compiled_distributions(names: Iterable[str]) -> frozenset[str]:
    """Which of *names* ship a compiled extension module.

    The objective half of "may this pin yield?": a distribution containing a
    ``.so`` / ``.pyd`` / ``.dylib`` is bound to the interpreter's ABI, so a
    workspace overlay holding a different version of it can break the
    platform's own code at import time. A pure-Python leaf cannot do that; the
    worst it can do is behave differently inside the one working copy that
    asked for it.

    Derived from the installed tree rather than from a hand-kept list, so the
    rule cannot rot as the image changes — a package that gains a native
    accelerator next release becomes protected without anyone editing a set.

    Must be called on the interpreter that PROVIDES the baseline; a name it
    cannot inspect is reported as compiled, which is the fail-closed direction.
    """
    from importlib import metadata

    found: set[str] = set()
    for raw in names:
        name = canonicalize_name(str(raw))
        try:
            dist = metadata.distribution(name)
        except Exception:  # noqa: BLE001 — unreadable metadata, assume the worst
            found.add(name)
            continue
        try:
            files = dist.files or ()
        except Exception:  # noqa: BLE001 — same
            found.add(name)
            continue
        for item in files:
            text = str(item)
            if text.endswith(_EXTENSION_SUFFIXES) or ".so." in text:
                found.add(name)
                break
    return frozenset(found)


def relaxable_baseline(
    baseline: Mapping[str, str],
    *,
    compiled: Iterable[str],
    protected_extra: Iterable[str] = (),
    relaxable_extra: Iterable[str] = (),
) -> frozenset[str]:
    """Baseline pins the platform is willing to yield, as canonical names.

    The rule, in the order it is applied:

    1. start from the whole baseline;
    2. withhold :data:`PROTECTED_DISTRIBUTIONS` — the platform's own machinery;
    3. withhold everything in *compiled* — ABI-bound, see
       :func:`compiled_distributions`;
    4. withhold operator-configured ``protected_extra``;
    5. re-admit operator-configured ``relaxable_extra`` LAST, so an operator who
       has decided that a specific compiled package may yield can say so
       without editing code. Deliberately the final word: the two rules above
       are defaults derived from the image, and a deployment that knows better
       than a default has to be able to express it.

    Returns names only; the versions stay in the baseline, which is still what
    the constraints are rendered from.
    """
    compiled_set = {canonicalize_name(str(name)) for name in compiled}
    protected = {canonicalize_name(str(name)) for name in protected_extra}
    admitted = {canonicalize_name(str(name)) for name in relaxable_extra}
    return frozenset(
        name
        for name in baseline
        if name in admitted
        or (
            name not in PROTECTED_DISTRIBUTIONS
            and name not in compiled_set
            and name not in protected
        )
    )


# pip cites a constraint it could not satisfy as "The user requested
# (constraint) tenacity==9.1.4" — by value, with no path to the file. The
# transcript rewrites that sentence for the reader (see the runner's
# ``_translate_pip_voice``), so BOTH spellings are matched here: this function
# reads the transcript, which may already have been rewritten.
_CONSTRAINT_PIN_RE = re.compile(
    r"(?:the user requested \(constraint\)|the platform pins)\s+"
    r"(?P<name>[A-Za-z0-9._-]+)\s*==",
    re.IGNORECASE,
)

# "    feast 0.65.0 depends on tenacity<9 and >=7" — the dependency side of the
# same conflict, and the only place the package STATING the bound is named.
# Matched alongside the constraint citation because a report states one, the
# other, or both depending on which party pip blames first.
_DEPENDS_ON_RE = re.compile(
    r"^\s*(?P<holder>[A-Za-z0-9._-]+)\s+\S+\s+depends on\s+"
    r"(?P<name>[A-Za-z0-9._-]+)\s*(?:[<>=!~]|$)",
    re.IGNORECASE | re.MULTILINE,
)


def pins_implicated_in_conflict(
    output: str,
    baseline: Mapping[str, str],
) -> dict[str, str]:
    """Baseline pins pip named as parties to the conflict it refused.

    Read from pip's own conflict report rather than re-derived: pip knows which
    of the several hundred pins actually collided, and a second resolution to
    work it out would cost another full index round trip for an answer already
    on screen.

    Two spellings are recognised — the constraint citation, and the
    ``X 1.2.3 depends on <name><specifier>`` lines that precede it — because a
    report sometimes states only the dependency side while the constraint side
    appears as a bare pin. Anything not in *baseline* is dropped, so a declared
    package named in the same report is never mistaken for a platform pin.

    Returns:
        ``canonical pin name -> the package that stated the bound`` (empty
        string when the report only cited the constraint). Empty mapping means
        the conflict is between declared packages and no pin needs to move.
    """
    found: dict[str, str] = {}
    for match in _CONSTRAINT_PIN_RE.finditer(output):
        name = canonicalize_name(match.group("name"))
        if name in baseline:
            found.setdefault(name, "")
    for match in _DEPENDS_ON_RE.finditer(output):
        name = canonicalize_name(match.group("name"))
        if name not in baseline:
            continue
        holder = canonicalize_name(match.group("holder"))
        # The dependency side is the more informative one, so it overwrites a
        # bare constraint citation for the same pin.
        if holder:
            found[name] = holder
        else:
            found.setdefault(name, "")
    return found


def drop_baseline_equivalents(
    overlay: Mapping[str, str],
    baseline: Mapping[str, str],
) -> tuple[str, ...]:
    """Overlay entries that duplicate the platform at the SAME version.

    With the constraints above in force this is the normal outcome for every
    transitive dependency the platform already has: pip resolves it to the
    platform's version and unpacks a second, identical copy into the overlay.
    Identical or not, that copy shadows the platform's — so it is removed, and
    the invariant "an overlay never contains a platform distribution" stays
    literally true rather than true-except-when-equal.

    Returns:
        Canonical names to delete from the overlay tree.
    """
    return tuple(
        sorted(
            name
            for name, version in overlay.items()
            if name in baseline and version == baseline[name]
        )
    )


def detect_shadowed(
    overlay: Mapping[str, str],
    baseline: Mapping[str, str],
    *,
    allowed: Iterable[str] = (),
) -> tuple[tuple[Mapping[str, str], ...], tuple[str, ...]]:
    """Compare a finished overlay with the platform's own packages.

    Args:
        overlay: ``canonical name -> version`` found in the overlay tree, AFTER
            :func:`drop_baseline_equivalents` has been applied.
        baseline: ``canonical name -> version`` of the code-location
            interpreter.
        allowed: Canonical names the platform YIELDED for this install. Their
            presence in the overlay is the intended outcome, not a violation —
            without this parameter the yield mechanism would install
            successfully and then fail its own post-install assertion.

    Returns:
        ``(shadowed, protected_hits)`` — every remaining overlay distribution
        that also exists on the platform and was not yielded, and the subset of
        those whose shadowing breaks the platform itself rather than only the
        workspace.

    A non-empty result is a HARD failure, both elements alike. Reaching it
    means either the constraints did not travel to pip, or a pin was released
    without being recorded — resolution under the constraints can only produce
    baseline versions, and those are dropped. The second element exists to make
    the report name the consequence, not to grade the severity.
    """
    yielded = {canonicalize_name(str(name)) for name in allowed}
    shadowed: list[Mapping[str, str]] = []
    protected: list[str] = []
    for name, version in sorted(overlay.items()):
        if name not in baseline or name in yielded:
            continue
        shadowed.append({"name": name, "version": version})
        if name in PROTECTED_DISTRIBUTIONS:
            protected.append(name)
    return tuple(shadowed), tuple(protected)


def installed_distributions() -> dict[str, str]:
    """Reverse-lookup of what the CURRENT interpreter provides.

    Called by the workspace-runtime process, whose interpreter is the one code
    locations load user code on — so this IS the platform baseline. Computed by
    reverse lookup rather than from a hand-maintained list, so it stays true
    across image rebuilds and records the versions that are actually there.

    NOT to be called from the installing sandbox: see the module docstring.
    """
    from importlib import metadata

    found: dict[str, str] = {}
    for dist in metadata.distributions():
        raw_name = dist.metadata["Name"] if dist.metadata else None
        if not raw_name:
            continue
        name = canonicalize_name(raw_name)
        # Duplicate names can appear when several path entries hold the same
        # distribution; the first wins, matching import resolution order.
        found.setdefault(name, dist.version or "")
    return found


# ── interpreter fingerprint (decision D4) ─────────────────────────────────


def interpreter_python_version() -> str:
    """``major.minor`` of the current interpreter."""
    import sys

    return f"{sys.version_info.major}.{sys.version_info.minor}"


def interpreter_platform_tag() -> str:
    """Platform tag of the current interpreter, e.g. ``linux-x86_64``."""
    return sysconfig.get_platform()


def interpreter_mismatch(
    manifest: DepsManifest,
    *,
    python_version: Optional[str] = None,
    platform_tag: Optional[str] = None,
) -> str:
    """Explain why an overlay must not be injected here, or return ``""``.

    The overlay may contain compiled extension modules, which are valid only
    for the interpreter version and platform that built them. Injecting a
    mismatched tree produces an ``ImportError`` deep inside a user's asset at
    definition-load time — an error whose text explains nothing. Refusing here
    with a sentence that names both baselines is the only actionable option.

    An empty ``python_version`` in the manifest means it was written before
    this field existed; treated as a mismatch, since the safe reaction is one
    reinstall rather than an unexplainable import failure.
    """
    want_python = python_version or interpreter_python_version()
    want_platform = platform_tag or interpreter_platform_tag()

    if not manifest.python_version:
        return (
            "the installed dependency tree does not record which interpreter "
            "built it; reinstall to rebuild it for this one"
        )
    if manifest.python_version != want_python:
        return (
            f"the installed dependency tree was built for Python "
            f"{manifest.python_version}, but this process runs Python "
            f"{want_python}. Align the task-runner and code-location image "
            f"baselines, then reinstall."
        )
    if manifest.platform_tag and manifest.platform_tag != want_platform:
        return (
            f"the installed dependency tree was built for {manifest.platform_tag}, "
            f"but this process runs on {want_platform}. Reinstall it here."
        )
    return ""


def manifest_state(
    declaration: DepsDeclaration,
    manifest: Optional[DepsManifest],
    *,
    overlay_present: bool = True,
    python_version: Optional[str] = None,
    platform_tag: Optional[str] = None,
) -> str:
    """Conclude the overlay's state for a read-only check (``ensure``).

    Pure comparison, no filesystem work beyond the ``overlay_present`` flag the
    caller already has: this runs on every asset-page open and must stay
    microsecond-cheap. Ordering matters — a failed install is reported as
    failed even if the declaration has since changed, because "the last attempt
    failed" is the more actionable fact.
    """
    if declaration.is_empty:
        return STATE_EMPTY
    if manifest is None or not overlay_present:
        return STATE_MISSING
    if manifest.state == STATE_FAILED:
        return STATE_FAILED
    if manifest.fingerprint != declaration.fingerprint:
        return STATE_DRIFTED
    if interpreter_mismatch(
        manifest, python_version=python_version, platform_tag=platform_tag
    ):
        return STATE_INCOMPATIBLE
    return STATE_READY


# ── paths ─────────────────────────────────────────────────────────────────


def deps_root(workspace_path: str) -> str:
    """Root of the platform-owned dependency state in a working copy."""
    return posixpath.join(workspace_path, DEPS_DIR)


def site_packages_path(workspace_path: str) -> str:
    """The single directory that goes on ``PYTHONPATH``.

    Always derived from a working-copy root, never hard-coded, because the
    root differs per consumer: the real path in the runtime, the container
    mount point in a sandbox.
    """
    return posixpath.join(deps_root(workspace_path), SITE_PACKAGES_DIRNAME)


def manifest_path(workspace_path: str) -> str:
    return posixpath.join(deps_root(workspace_path), MANIFEST_FILENAME)


def cache_path(workspace_path: str) -> str:
    """pip's HTTP/wheel cache, kept inside the copy so a reinstall is fast."""
    return posixpath.join(deps_root(workspace_path), CACHE_DIRNAME)


def constraints_path(workspace_path: str) -> str:
    """The generated pip constraints file; see :data:`CONSTRAINTS_FILENAME`."""
    return posixpath.join(deps_root(workspace_path), CONSTRAINTS_FILENAME)


def resolution_report_path(workspace_path: str) -> str:
    """Where a diagnostic ``pip --dry-run --report`` writes its JSON."""
    return posixpath.join(deps_root(workspace_path), RESOLUTION_REPORT_FILENAME)


def install_log_path(workspace_path: str) -> str:
    """Transcript of the last install; see :data:`INSTALL_LOG_FILENAME`."""
    return posixpath.join(deps_root(workspace_path), INSTALL_LOG_FILENAME)


def utc_now_iso() -> str:
    """Timestamp for the manifest. UTC, always — the frontend localises."""
    return datetime.now(timezone.utc).isoformat()


def _name_version_list(value: Any) -> list[dict[str, str]]:
    """Coerce a stored list of ``{name, version}`` records, dropping junk."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, Mapping) and item.get("name"):
            result.append(
                {"name": str(item["name"]), "version": str(item.get("version") or "")}
            )
    return result


def _relaxed_list(value: Any) -> list[dict[str, str]]:
    """Coerce stored yield records, dropping junk.

    Same tolerance as everything else read from the manifest: this is cached
    state written by a possibly different build, and a record it cannot read is
    dropped rather than allowed to fail a reload.
    """
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, Mapping) and item.get("name"):
            result.append(
                {
                    "name": str(item["name"]),
                    "platform_version": str(item.get("platform_version") or ""),
                    "overlay_version": str(item.get("overlay_version") or ""),
                    "required_by": str(item.get("required_by") or ""),
                }
            )
    return result


__all__ = [
    "CACHE_DIRNAME",
    "CONSTRAINTS_FILENAME",
    "DECLARATION_RELPATH",
    "DEPS_DIR",
    "FAILURE_DECLARATION_INVALID",
    "FAILURE_INDEX_NOT_CONFIGURED",
    "FAILURE_INSTALL_FAILED",
    "FAILURE_NO_WHEEL",
    "FAILURE_PLATFORM_CONFLICT",
    "FAILURE_PLATFORM_TRANSITIVE",
    "FAILURE_SHADOW_PLATFORM",
    "FAILURE_TOO_LARGE",
    "FINGERPRINT_VERSION",
    "INSTALL_LOG_FILENAME",
    "MANIFEST_FILENAME",
    "MAX_REQUIREMENTS",
    "PROTECTED_DISTRIBUTIONS",
    "RESOLUTION_REPORT_FILENAME",
    "SITE_PACKAGES_DIRNAME",
    "TASK_KIND_DEPS_SYNC",
    "STATE_DECLARATION_INVALID",
    "STATE_DRIFTED",
    "STATE_EMPTY",
    "STATE_FAILED",
    "STATE_INCOMPATIBLE",
    "STATE_MISSING",
    "STATE_READY",
    "DepsDeclaration",
    "DepsDeclarationError",
    "DepsManifest",
    "InstallPlan",
    "PlatformConflict",
    "PlatformCoreConflict",
    "ShadowedPlatformPackage",
    "SourceOnlyRequirement",
    "TransitiveConflict",
    "baseline_constraints",
    "blockers_from_resolution",
    "cache_path",
    "compiled_distributions",
    "constraints_path",
    "deps_root",
    "detect_shadowed",
    "drop_baseline_equivalents",
    "find_platform_conflicts",
    "install_log_path",
    "installed_distributions",
    "interpreter_mismatch",
    "interpreter_platform_tag",
    "interpreter_python_version",
    "is_missing_distribution",
    "manifest_path",
    "manifest_state",
    "missing_distribution_requirements",
    "parse_declaration",
    "pins_implicated_in_conflict",
    "relax_upper_bounds",
    "relaxable_baseline",
    "requirement_name",
    "resolution_report_path",
    "site_packages_path",
    "split_platform_provided",
    "utc_now_iso",
    "versions_matching",
]
