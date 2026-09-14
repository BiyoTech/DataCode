"""``deps.sync`` — install a working copy's declared Python packages.

Runs inside the sandbox the ``L3_deps_install`` profile describes: writable
working copy, egress to the package index only, no credentials of any kind. The
output is a plain ``site-packages`` tree under ``.datacode/deps/`` which three
other processes later prepend to ``PYTHONPATH``.

What this module deliberately does NOT do
-----------------------------------------
* **Parse ``pyproject.toml``.** The requirement list arrives already normalised
  in the task parameters. Parsing is where the platform-conflict rules live, and
  those must be applied on the side that can refuse the request with an
  actionable message — refusing from in here would surface as a failed task with
  a message nobody asked for.
* **Install the project itself.** ``pip install .`` would need the directory to
  be a buildable package (``asset-dag`` is not even a legal distribution name)
  and a compiler toolchain this image deliberately lacks. Only the declared
  third-party requirements are installed; the user's own ``defs`` package is
  importable because the code location's cwd is its parent.
* **Write the manifest.** It returns the facts; the runtime writes the file. One
  writer means an interrupted install cannot leave a manifest that claims a tree
  which is not there.

Why wheels only
---------------
``--only-binary=:all:``. There is no compiler here, so a source distribution
fails anyway — but late, after a long download, with a build log instead of an
explanation. Refusing up front also removes the "arbitrary ``setup.py`` executes
during install" path, which is the only code execution an install would
otherwise perform before anything else validates it.

Why the platform baseline arrives in the task parameters
--------------------------------------------------------
Because this image is NOT the platform. The overlay is consumed by the
workspace-runtime interpreter (code locations share it), whose dependency set is
a superset of this one — it has dagster, pandas, pyarrow; this image has sqlmesh
and little else. Asking THIS interpreter what the platform provides gets both
possible answers wrong: packages the platform has but this image lacks would be
installed and would shadow the platform's copy, and packages this image has but
the platform lacks would be skipped and then fail to import inside the user's
asset. Neither is visible in development, where the local backend runs pip on
the runtime's own interpreter and the two sets coincide.

So the baseline travels in :attr:`InstallPlan.platform_packages`, and an absent
one is refused rather than guessed.

Why an install may take two attempts
------------------------------------
Pinning the whole baseline gives every one of its several hundred
distributions a veto over every declaration, and most of them do not deserve
one: replacing the platform's ``tenacity`` affects the one working copy that
asked for it, while replacing its ``numpy`` breaks the platform at ABI level.
The runtime decides which pins may yield (``InstallPlan.relaxable_packages``,
derived from the installed tree) and this module spends the retry: full pins
first, and — only if pip refused to resolve and every pin it named is yieldable
— those pins released, once. What yielded is returned so the runtime can record
it; nothing yields silently.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from datacode_common.python_deps import (
    CONSTRAINTS_FILENAME,
    FAILURE_DECLARATION_INVALID,
    FAILURE_INSTALL_FAILED,
    FAILURE_PLATFORM_CONFLICT,
    FAILURE_PLATFORM_TRANSITIVE,
    FAILURE_SHADOW_PLATFORM,
    FAILURE_TOO_LARGE,
    DepsDeclaration,
    DepsDeclarationError,
    InstallPlan,
    PlatformCoreConflict,
    ShadowedPlatformPackage,
    TransitiveConflict,
    baseline_constraints,
    blockers_from_resolution,
    cache_path,
    constraints_path,
    detect_shadowed,
    drop_baseline_equivalents,
    install_log_path,
    interpreter_platform_tag,
    interpreter_python_version,
    pins_implicated_in_conflict,
    relax_upper_bounds,
    requirement_name,
    resolution_report_path,
    site_packages_path,
    split_platform_provided,
)

from src.sqlmesh_ops import SqlMeshOpError

# Stable failure codes. The reload job turns each into its own sentence, so they
# are part of the contract with the UI and the CLI rather than log strings.
#
# Aliased from the shared module rather than re-typed: these strings cross four
# boundaries (here -> task result -> reload job -> UI translation key), and the
# one time a conflict was reported under the DECLARATION code, users were told to
# fix a file whose syntax was perfectly valid.
ERR_DEPS_DECLARATION = FAILURE_DECLARATION_INVALID
ERR_DEPS_PLATFORM_CONFLICT = FAILURE_PLATFORM_CONFLICT
# A declared package whose own dependencies fight the platform baseline. Its own
# code because the next action is not "accept the platform's version" but "move
# to another release of what you declared" — or ask an operator when none fits.
ERR_DEPS_PLATFORM_TRANSITIVE = FAILURE_PLATFORM_TRANSITIVE
ERR_DEPS_INSTALL = FAILURE_INSTALL_FAILED
ERR_DEPS_TOO_LARGE = FAILURE_TOO_LARGE
ERR_DEPS_SHADOWED = FAILURE_SHADOW_PLATFORM

# Kept with a failure. Long enough for pip's resolver conflict report, which is
# the one message that actually explains an unsatisfiable declaration.
_OUTPUT_TAIL_CHARS = 16384

# Budget for ONE diagnostic resolve (see :func:`_probe_resolution`). Bounded
# because a diagnosis is worth less than the install it explains: if the index
# is slow here, the answer is the plain pip output, not a longer wait.
_PROBE_TIMEOUT_SECONDS = 240

# Guardrail on the transcript file: pip's per-file progress is chatty and the
# file lives on the user's own volume.
_MAX_LOG_BYTES = 4 * 1024 * 1024


class _Transcript:
    """Append-only text log of one install, inside the working copy.

    Doubles as the live progress channel: the runtime tails this file while the
    task runs. A file rather than the task protocol's stdout because only one of
    the two vehicles could stream stdout, and a channel that works in dev but
    not in production is worse than no channel.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._written = 0
        self._truncated = False
        self._lines: list[str] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        # Truncated per run: this is the transcript of THE LAST install, and a
        # growing file would eventually be neither readable nor bounded.
        with open(path, "w", encoding="utf-8"):
            pass

    def write(self, line: str) -> None:
        text = _translate_pip_voice(line.rstrip("\n"))
        self._lines.append(text)
        if self._truncated:
            return
        payload = (text + "\n").encode("utf-8", errors="replace")
        if self._written + len(payload) > _MAX_LOG_BYTES:
            self._truncated = True
            payload = b"... [transcript truncated]\n"
        try:
            with open(self._path, "ab") as handle:
                handle.write(payload)
        except OSError:
            # A transcript is diagnostics; losing it must not fail the install.
            self._truncated = True
            return
        self._written += len(payload)

    def tail(self) -> str:
        return "\n".join(self._lines)[-_OUTPUT_TAIL_CHARS:]


_USER_REQUESTED_RE = re.compile(
    r"\bthe user requested \(constraint\) (?P<pin>\S+)", re.IGNORECASE
)


def _translate_pip_voice(line: str) -> str:
    """Rewrite the one pip sentence that misattributes a platform pin.

    pip calls the constraints file's pins "the user requested". They are not:
    the user wrote a declaration, and this file was generated from the platform
    baseline. Left alone, the transcript says "skipping pandas: provided by the
    platform" and thirty lines later "the user requested (constraint)
    pandas==2.3.3" — the same version, attributed to opposite parties, with
    nothing connecting them.

    The filename is kept in the replacement so the reader can still find the
    file the pin came from.
    """
    return _USER_REQUESTED_RE.sub(
        lambda match: (
            f"the platform pins {match.group('pin')} "
            f"(from {CONSTRAINTS_FILENAME})"
        ),
        line,
    )


def op_deps_sync(
    workspace_path: str,
    params: Mapping[str, Any],
    *,
    gateway: str = "",
    environment: str = "",
    sql_only: bool = False,
    project_subdir: str = "",
    progress: Optional[Callable[..., None]] = None,
) -> dict[str, Any]:
    """Install the declared requirements into the working copy's overlay.

    Args:
        workspace_path: Working copy as the sandbox sees it (the mount point in
            a container, the real path under the local backend). Every target
            path is derived from it, so the same code works under both.
        params: :class:`InstallPlan` parameters — ``requirements`` (normalised
            PEP 508 strings), ``fingerprint``, ``max_bytes``.
        gateway: Unused; accepted for handler-signature uniformity.
        environment: Unused; same.
        sql_only: Unused; same.
        project_subdir: Unused; same.
        progress: Unused. Progress is the transcript file, not the stdout
            channel — see :class:`_Transcript`.

    Returns:
        The facts the runtime needs to write the manifest: ``packages``,
        ``platform_provided``, ``shadowed``, ``python_version``,
        ``platform_tag``, ``size_bytes``, ``fingerprint``, ``output_tail``.

    Raises:
        SqlMeshOpError: Tagged with one of the ``ERR_DEPS_*`` codes above. The
            runtime maps the code to a sentence and to the reload job's failure
            reason.
    """
    del gateway, environment, sql_only, project_subdir, progress

    started = time.monotonic()
    try:
        plan = InstallPlan.from_params(params)
    except DepsDeclarationError as exc:
        raise SqlMeshOpError(ERR_DEPS_DECLARATION, str(exc)) from exc

    target = Path(site_packages_path(workspace_path))
    transcript = _Transcript(Path(install_log_path(workspace_path)))

    baseline = plan.baseline
    if not baseline:
        # Fail closed. An empty baseline reads as "the platform provides
        # nothing", under which every declared package — dagster included —
        # would be installed into an overlay that shadows the real one. The
        # cause is always a version skew between runtime and runner, never
        # anything the user wrote, so the message points at the deployment.
        raise SqlMeshOpError(
            ERR_DEPS_INSTALL,
            "the install plan carries no platform baseline, so the installer "
            "cannot tell which packages the code location already provides. "
            "The workspace-runtime and task-runner versions are out of step; "
            "no packages were installed.",
        )

    try:
        to_install, provided = split_platform_provided(
            DepsDeclaration(
                requirements=plan.requirements, fingerprint=plan.fingerprint
            ),
            baseline,
        )
    except PlatformCoreConflict as exc:
        # The runtime refuses this before submitting, so reaching it here means
        # the declaration changed between the two. Same code either way: what
        # the user must do does not depend on which side noticed.
        transcript.write(f"refused: {exc}")
        raise SqlMeshOpError(ERR_DEPS_PLATFORM_CONFLICT, str(exc)) from exc

    for entry in provided:
        transcript.write(
            f"skipping {entry['name']}: provided by the platform "
            f"({entry['version']})"
        )

    # A tree from a previous fingerprint is not a base to build on: pip would
    # leave the packages that are no longer declared in place, and the manifest
    # would then describe a tree that is a union of two declarations.
    _reset_target(target, transcript)

    constraints = _write_constraints(workspace_path, baseline, transcript)
    yielded: dict[str, str] = {}

    if to_install:
        try:
            yielded = _install_with_yields(
                to_install, target, workspace_path, plan, constraints, transcript
            )
        except SqlMeshOpError:
            # A failed pip leaves whatever it managed to unpack. That half tree
            # must not survive: the runtime writes a FAILED manifest, but a later
            # consumer reading a stale READY one would put a partial overlay on
            # PYTHONPATH — an import error inside user code instead of a clear
            # "dependencies are not installed".
            _reset_target(target, transcript)
            raise
    else:
        transcript.write("nothing to install; every requirement is platform-provided")

    # Everything resolved to a version the platform already has is redundant by
    # construction (the constraints pinned it there) and harmful by position
    # (the overlay comes first on sys.path). Removing it is what keeps the
    # invariant literal: an overlay holds ONLY what the platform lacks, plus the
    # pins the platform deliberately yielded.
    _drop_platform_duplicates(target, baseline, transcript)

    overlay = _overlay_distributions(target)
    shadowed, protected = detect_shadowed(overlay, baseline, allowed=yielded)
    if shadowed:
        error = ShadowedPlatformPackage(protected or [item["name"] for item in shadowed])
        transcript.write(f"refused: {error}")
        # Removed, not kept for inspection: the tree is on PYTHONPATH the moment
        # a manifest exists, and a broken code location is worse than no overlay.
        _reset_target(target, transcript)
        raise SqlMeshOpError(ERR_DEPS_SHADOWED, str(error))

    size = _tree_size(target)
    if plan.max_bytes and size > plan.max_bytes:
        _reset_target(target, transcript)
        raise SqlMeshOpError(
            ERR_DEPS_TOO_LARGE,
            f"the installed dependency tree is {size / 1e9:.1f} GB, above the "
            f"{plan.max_bytes / 1e9:.1f} GB limit for one working copy. Remove "
            f"the heaviest packages from the declaration or ask an operator to "
            f"raise deps.max_bytes.",
        )

    transcript.write(
        f"installed {len(overlay)} packages, {size / 1e6:.1f} MB, in "
        f"{time.monotonic() - started:.1f}s"
    )
    return {
        "fingerprint": plan.fingerprint,
        "packages": [
            {"name": name, "version": version} for name, version in sorted(overlay.items())
        ],
        "platform_provided": [dict(entry) for entry in provided],
        # Always empty by the invariant above; reported so the manifest keeps a
        # field the readers already understand rather than gaining a special
        # case for "this can no longer happen".
        "shadowed": [],
        # The pins the platform yielded, with what replaced them. The only
        # record of why this copy's runtime differs from the platform's own.
        "relaxed": [
            {
                "name": name,
                "platform_version": baseline.get(name, ""),
                "overlay_version": overlay.get(name, ""),
                "required_by": required_by,
            }
            for name, required_by in sorted(yielded.items())
        ],
        "python_version": interpreter_python_version(),
        "platform_tag": interpreter_platform_tag(),
        "size_bytes": size,
        "output_tail": transcript.tail(),
    }


# ── platform baseline ─────────────────────────────────────────────────────


def _write_constraints(
    workspace_path: str,
    baseline: Mapping[str, str],
    transcript: _Transcript,
    *,
    exclude: Mapping[str, str] | None = None,
) -> Path:
    """Render the baseline as a pip constraints file inside the working copy.

    Next to the overlay rather than in a temporary directory: a user asking
    "why did it resolve to that version" can read the exact file pip was given,
    and the two vehicles need no different path handling for it.

    Args:
        exclude: Pins the platform yielded for this attempt, as
            ``name -> requiring package``. Rewritten in place rather than
            written to a second file: the file is the record of what pip was
            actually given, and two files would leave the reader guessing which
            one produced the tree they have.
    """
    path = Path(constraints_path(workspace_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    dropped = tuple(exclude or ())
    path.write_text(
        baseline_constraints(baseline, exclude=dropped), encoding="utf-8"
    )
    if dropped:
        transcript.write(
            f"releasing {len(dropped)} platform pin(s) so the declaration can "
            f"resolve: " + ", ".join(sorted(dropped))
        )
    else:
        transcript.write(
            f"constraining resolution to the platform baseline "
            f"({len(baseline)} packages, see {path.name})"
        )
    return path


def _drop_platform_duplicates(
    target: Path,
    baseline: Mapping[str, str],
    transcript: _Transcript,
) -> None:
    """Delete overlay entries that duplicate the platform at the same version.

    Under the constraints file this is what pip does with every transitive
    dependency the platform already has: it resolves to the pinned version and
    unpacks a byte-equivalent copy. Equivalent or not, that copy shadows the
    platform's — and it is the copy a future platform upgrade would leave
    stale, silently, since nothing reinstalls an overlay whose fingerprint did
    not change. Removing it is cheaper than reasoning about it.
    """
    overlay = _overlay_distributions(target)
    redundant = drop_baseline_equivalents(overlay, baseline)
    if not redundant:
        return
    for name in redundant:
        _remove_distribution(target, name, transcript)
    transcript.write(
        f"removed {len(redundant)} package(s) already provided by the platform "
        f"at the same version: " + ", ".join(redundant)
    )


def _remove_distribution(
    target: Path,
    name: str,
    transcript: _Transcript,
) -> None:
    """Remove one installed distribution from the overlay tree.

    Driven by the distribution's own ``RECORD``, which is the only authoritative
    list of what it installed — a package's import name need not resemble its
    distribution name, so deleting by guess either misses files or takes a
    neighbour's. Paths outside the overlay are ignored: ``RECORD`` is data that
    arrived with a downloaded archive, and a ``../`` entry in it must not turn
    this cleanup into an arbitrary delete.
    """
    for info in _distribution_dirs(target, name):
        for relative in _record_entries(info):
            candidate = (target / relative).resolve()
            try:
                candidate.relative_to(target.resolve())
            except ValueError:
                transcript.write(
                    f"ignoring out-of-tree RECORD entry for {name}: {relative}"
                )
                continue
            try:
                if candidate.is_dir() and not candidate.is_symlink():
                    shutil.rmtree(candidate, ignore_errors=True)
                elif candidate.exists() or candidate.is_symlink():
                    candidate.unlink()
            except OSError:
                # Best-effort: a leftover file is inert, while failing the
                # install here would discard a tree that is otherwise correct.
                continue
        shutil.rmtree(info, ignore_errors=True)
    _prune_empty_dirs(target)


def _distribution_dirs(target: Path, name: str) -> list[Path]:
    """Metadata directories of one distribution in the overlay."""
    from packaging.utils import canonicalize_name

    found: list[Path] = []
    for entry in target.iterdir():
        if entry.suffix not in (".dist-info", ".egg-info") or not entry.is_dir():
            continue
        stem = entry.name[: -len(entry.suffix)]
        dist_name, _, _ = stem.partition("-")
        if canonicalize_name(dist_name) == name:
            found.append(entry)
    return found


def _record_entries(info: Path) -> list[str]:
    """Relative paths a distribution installed, per its ``RECORD``."""
    record = info / "RECORD"
    try:
        text = record.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: list[str] = []
    # A CSV, and quoted: filenames legitimately contain commas.
    for row in csv.reader(text.splitlines()):
        if row and row[0]:
            entries.append(row[0])
    return entries


def _prune_empty_dirs(target: Path) -> None:
    """Remove directories a deletion emptied, deepest first."""
    for root, dirs, files in os.walk(target, topdown=False):
        if root == str(target):
            continue
        if not dirs and not files:
            try:
                Path(root).rmdir()
            except OSError:
                continue


# ── pip ───────────────────────────────────────────────────────────────────


def _run_pip(
    requirements: tuple[str, ...],
    target: Path,
    workspace_path: str,
    plan: InstallPlan,
    constraints: Path,
    transcript: _Transcript,
) -> int:
    """Run one pip install, streaming its output into the transcript.

    Returns pip's exit code. Deciding what a non-zero one MEANS is the caller's
    job (:func:`_install_with_yields`), because the answer depends on state this
    function does not have: whether a pin may still be released and retried, or
    whether the failure is final and has to be diagnosed.

    Raises:
        SqlMeshOpError: pip could not be started at all.
    """
    argv = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-input",
        # Never touch a user-level or global pip config: this process must
        # behave the same wherever it runs, and a config file arriving with the
        # working copy could redirect the index.
        "--isolated",
        "--disable-pip-version-check",
        # Progress bars are terminal-oriented control sequences; the transcript
        # is read as text by a human and by the agent.
        "--progress-bar",
        "off",
        # See the module docstring: no compiler here, and no setup.py execution.
        "--only-binary=:all:",
        # The platform baseline, as a resolver bound. This is the flag that
        # makes a transitive conflict impossible rather than merely unlikely:
        # without it a declaration of `polars` can resolve numpy 2.x into the
        # overlay and break the platform's pandas at ABI level, inside a code
        # location, with the traceback pointing at the user's asset.
        "--constraint",
        str(constraints),
        "--target",
        str(target),
        "--cache-dir",
        cache_path(workspace_path),
    ]
    if plan.index_url:
        argv += ["--index-url", plan.index_url]
    for extra in plan.extra_index_urls:
        argv += ["--extra-index-url", extra]
    for host in plan.trusted_hosts:
        argv += ["--trusted-host", host]
    argv += list(requirements)

    # The constraint file is named in the echoed command, not left implicit: it
    # is the reason a resolution that looks correct on its own fails here, and a
    # reader who can see the flag can go and read the file.
    transcript.write(
        f"$ pip install --constraint {constraints.name} --target {target} "
        + " ".join(requirements)
    )
    if plan.index_url:
        transcript.write(f"index: {plan.index_url}")

    try:
        process = subprocess.Popen(  # noqa: S603 — the argv is built here
            argv,
            cwd=workspace_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise SqlMeshOpError(
            ERR_DEPS_INSTALL, f"could not start pip: {exc}"
        ) from exc

    assert process.stdout is not None
    for line in process.stdout:
        transcript.write(line)
    return process.wait()


def _install_with_yields(
    requirements: tuple[str, ...],
    target: Path,
    workspace_path: str,
    plan: InstallPlan,
    constraints: Path,
    transcript: _Transcript,
) -> dict[str, str]:
    """Install *requirements*, releasing yieldable platform pins if needed.

    Two attempts at most:

    1. the full baseline pinned. This is the only outcome that leaves the
       platform's runtime and the workspace's identical, so it is always tried
       first and is the normal result;
    2. if — and only if — pip refused to resolve, and EVERY baseline pin it
       named as a party to the conflict is one the platform is willing to yield
       (:attr:`InstallPlan.relaxable_packages`), those pins are dropped and the
       install is retried once.

    A single retry, not a loop: the second attempt is given every yieldable pin
    pip implicated, so a third would only be answering a conflict that involves
    a pin which may NOT move — which is a diagnosis, not another attempt.

    Returns:
        ``name -> requiring package`` for the pins actually released. Empty on
        the normal path, which is also what a caller must see when nothing
        yielded.

    Raises:
        SqlMeshOpError: The install failed for good. Tagged
            ``ERR_DEPS_PLATFORM_TRANSITIVE`` when the cause is a baseline pin
            that cannot move, otherwise ``ERR_DEPS_INSTALL``.
    """
    exit_code = _run_pip(
        requirements, target, workspace_path, plan, constraints, transcript
    ) or 0
    if exit_code == 0:
        return {}

    tail = transcript.tail()
    yielded: dict[str, str] = {}
    if _is_resolution_conflict(tail):
        implicated = pins_implicated_in_conflict(tail, plan.baseline)
        blocked = sorted(set(implicated) - plan.relaxable)
        if implicated and not blocked:
            # Every pin in the way is one the platform may release: a pure
            # Python leaf whose replacement affects this working copy alone.
            yielded = dict(implicated)
            transcript.write(
                "the conflict involves only platform packages that may be "
                "overridden for one working copy; retrying with those pins "
                "released"
            )
            relaxed_constraints = _write_constraints(
                workspace_path, plan.baseline, transcript, exclude=yielded
            )
            # The half tree from the refused attempt is not a base to build on.
            _reset_target(target, transcript)
            exit_code = _run_pip(
                requirements,
                target,
                workspace_path,
                plan,
                relaxed_constraints,
                transcript,
            ) or 0
            if exit_code == 0:
                for name, holder in sorted(yielded.items()):
                    transcript.write(
                        f"overriding the platform's {name}"
                        f"=={plan.baseline.get(name, '?')} in this working "
                        f"copy" + (f" (required by {holder})" if holder else "")
                    )
                return yielded
            tail = transcript.tail()
        elif blocked:
            transcript.write(
                "the conflict involves platform packages that cannot be "
                "overridden: " + ", ".join(blocked)
            )

    _raise_install_failure(
        requirements,
        workspace_path=workspace_path,
        target=target,
        plan=plan,
        constraints=constraints,
        transcript=transcript,
        tail=tail,
        exit_code=exit_code,
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _raise_install_failure(
    requirements: tuple[str, ...],
    *,
    workspace_path: str,
    target: Path,
    plan: InstallPlan,
    constraints: Path,
    transcript: _Transcript,
    tail: str,
    exit_code: int,
) -> None:
    """Turn a final pip failure into the most actionable error available.

    A resolution failure is the one case where pip's own report is not an answer
    but a pointer: it names versions the user never wrote, because the bound
    they cannot move is the platform's. Diagnosed here — where pip, the index
    and the constraints file all are — so what reaches the UI is "declare X
    instead", not a puzzle.
    """
    conflicts = (
        _diagnose_transitive(
            requirements,
            workspace_path=workspace_path,
            target=target,
            plan=plan,
            constraints=_write_probe_constraints(constraints, plan, transcript),
            transcript=transcript,
        )
        if _is_resolution_conflict(tail)
        else ()
    )
    if conflicts:
        raise SqlMeshOpError(
            ERR_DEPS_PLATFORM_TRANSITIVE,
            _transitive_message(conflicts, tail),
            detail={"transitive_conflicts": [item.to_dict() for item in conflicts]},
        )
    raise SqlMeshOpError(
        ERR_DEPS_INSTALL, _classify_failure(tail, exit_code, plan.baseline)
    )


def _write_probe_constraints(
    constraints: Path,
    plan: InstallPlan,
    transcript: _Transcript,
) -> Path:
    """The pins that CANNOT move, as a constraints file for the diagnosis.

    A separate file, and separate from whatever the failed attempt used, because
    the question the diagnosis asks is narrower than the one the install asked:
    not "does this resolve against the platform" but "does any release of it
    resolve against the part of the platform that will never yield". Answering
    the wide question would print "no release of this package fits the platform
    baseline" for a declaration that only needed one pure-Python pin released —
    a verdict that is both wrong and unactionable.

    Falls back to the attempt's own file when nothing may yield, so a
    deployment with no yieldable pins behaves exactly as before.
    """
    yieldable = sorted(plan.relaxable & set(plan.baseline))
    if not yieldable:
        return constraints
    path = constraints.with_name(f"{constraints.stem}.immovable{constraints.suffix}")
    try:
        path.write_text(
            baseline_constraints(plan.baseline, exclude=yieldable), encoding="utf-8"
        )
    except OSError as exc:
        # A diagnosis is best-effort; the failure it explains is already final.
        transcript.write(f"could not write the diagnostic constraints: {exc}")
        return constraints
    return path


# ── diagnosing an unsatisfiable resolution ────────────────────────────────


def _is_resolution_conflict(output: str) -> bool:
    """Whether pip failed to resolve at all, as opposed to anything else.

    Only a resolution conflict is worth probing: a missing wheel, an unreachable
    index or a size overrun cannot be explained by rewriting a version bound, and
    spending two extra resolves on them would only delay the real message.
    """
    lowered = output.lower()
    return "resolutionimpossible" in lowered or "conflict" in lowered


def _transitive_message(
    conflicts: tuple[TransitiveConflict, ...], tail: str
) -> str:
    """One sentence per conflicting requirement, then pip's own output."""
    lead = "\n".join(item.message() for item in conflicts)
    return f"{lead}\n\n{tail}"


def _diagnose_transitive(
    requirements: tuple[str, ...],
    *,
    workspace_path: str,
    target: Path,
    plan: InstallPlan,
    constraints: Path,
    transcript: _Transcript,
) -> tuple[TransitiveConflict, ...]:
    """Find, per declared requirement, WHY the baseline blocks it.

    Two probes per requirement, both ``--dry-run`` so nothing is installed and
    no package code runs:

    1. the requirement as declared, WITHOUT the baseline constraints — the
       report carries each package's own ``requires_dist``, which is where the
       RANGE a dependency states lives. That range, compared against the
       platform's version, is the actual cause and the part pip only implies;
    2. the requirement with its ceilings removed, WITH the constraints that
       cannot yield — if that resolves, the answer is an edit, and the report
       names the version the edit lands on.

    The second probe deliberately uses the IMMOVABLE pins only (see
    :func:`_write_probe_constraints`). Probing against the full baseline would
    declare "no release fits" for a package that merely needed a pure-Python
    pin released — and the installer has already tried and failed that route by
    the time this runs, so the narrower question is the only one left open.

    Returns one :class:`TransitiveConflict` per requirement either probe
    explained. An empty tuple means the failure is not of this shape, and the
    caller falls back to pip's own output rather than guessing.
    """
    baseline = plan.baseline
    found: list[TransitiveConflict] = []
    for requirement in requirements:
        name = requirement_name(requirement)
        blockers: tuple[Mapping[str, str], ...] = ()
        unconstrained = _probe_resolution(
            (requirement,),
            workspace_path=workspace_path,
            target=target,
            plan=plan,
            constraints=None,
            transcript=transcript,
        )
        if unconstrained is not None:
            blockers = blockers_from_resolution(
                _report_entries(unconstrained), baseline
            )

        suggested = ""
        resolved_version = ""
        relaxed = relax_upper_bounds(requirement)
        if relaxed != requirement:
            constrained = _probe_resolution(
                (relaxed,),
                workspace_path=workspace_path,
                target=target,
                plan=plan,
                constraints=constraints,
                transcript=transcript,
            )
            version = _resolved_version(constrained, name)
            if version:
                suggested, resolved_version = relaxed, version

        if not blockers and not suggested:
            # Neither probe explained anything about THIS requirement — the
            # conflict may be between two declared packages, which no
            # single-requirement resolve can see. Reported as such by omission:
            # pip's own output is the honest answer then.
            continue
        found.append(
            TransitiveConflict(
                requirement=requirement,
                name=name,
                blockers=blockers,
                suggested=suggested,
                resolved_version=resolved_version,
                # Which of the blockers the platform will not yield. The rest
                # were already released and retried before this diagnosis ran,
                # so these are what is left standing in the way — and the only
                # ones an operator has to act on.
                protected=tuple(
                    str(item["name"])
                    for item in blockers
                    if str(item["name"]) not in plan.relaxable
                ),
            )
        )
    return tuple(found)


def _probe_resolution(
    requirements: tuple[str, ...],
    *,
    workspace_path: str,
    target: Path,
    plan: InstallPlan,
    constraints: Optional[Path],
    transcript: _Transcript,
) -> Optional[dict[str, Any]]:
    """Resolve *requirements* without installing, and return pip's report.

    ``--dry-run --report`` is the whole point: it is a resolution, nothing more.
    No wheel is unpacked, so no package code executes — which is the property
    that makes it safe to do this with packages the user chose.

    Returns ``None`` on any failure: a diagnosis that cannot be produced must
    not replace the failure it was meant to explain.
    """
    report = Path(resolution_report_path(workspace_path))
    argv = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--report",
        str(report),
        # Nothing is installed, so pip's human output is noise here; the report
        # file carries the answer.
        "--quiet",
        "--no-input",
        "--isolated",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        # ``--report`` demands ``--dry-run`` and one of ``--target`` /
        # ``--ignore-installed``; both are given, so this works across the pip
        # releases that spell the requirement differently. Neither changes what
        # resolves: with a target directory nothing is installed anyway.
        "--target",
        str(target),
        "--ignore-installed",
        "--cache-dir",
        cache_path(workspace_path),
    ]
    if constraints is not None:
        argv += ["--constraint", str(constraints)]
    if plan.index_url:
        argv += ["--index-url", plan.index_url]
    for extra in plan.extra_index_urls:
        argv += ["--extra-index-url", extra]
    for host in plan.trusted_hosts:
        argv += ["--trusted-host", host]
    argv += list(requirements)

    label = "with" if constraints is not None else "without"
    transcript.write(
        f"$ pip install --dry-run ({label} the platform baseline) "
        + " ".join(requirements)
    )
    try:
        process = subprocess.run(  # noqa: S603 — the argv is built here
            argv,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        transcript.write(f"the probe could not run: {exc}")
        return None
    if process.returncode != 0:
        return None
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        transcript.write(f"the probe produced no readable report: {exc}")
        return None
    return data if isinstance(data, dict) else None


def _report_entries(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The ``install`` list of a pip report, whatever shape it arrived in."""
    entries = report.get("install")
    return [item for item in entries if isinstance(item, Mapping)] if isinstance(
        entries, list
    ) else []


def _resolved_version(report: Optional[Mapping[str, Any]], name: str) -> str:
    """Version *name* was resolved to, or ``""`` when it is not in the report."""
    from packaging.utils import canonicalize_name

    if report is None:
        return ""
    for entry in _report_entries(report):
        metadata = entry.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        if canonicalize_name(str(metadata.get("name") or "")) == name:
            return str(metadata.get("version") or "")
    return ""


def _classify_failure(
    output: str,
    exit_code: int,
    baseline: Optional[Mapping[str, str]] = None,
) -> str:
    """Turn pip's exit into the shortest sentence that names the next action.

    pip's own message is always included — it is frequently the only place the
    conflicting version pair appears — but on its own it is a wall of resolver
    output. The prefix states which of the failure shapes this is, because the
    user's next move differs for each.

    Args:
        baseline: The platform baseline, used only to recognise a pin that came
            from it. Needed because pip attributes those pins to "the user" and
            does not name the file they came from in its conflict report — so
            the filename alone is not enough to recognise the case.
    """
    lowered = output.lower()
    tail = output[-_OUTPUT_TAIL_CHARS:]

    if "no matching distribution" in lowered or "could not find a version" in lowered:
        reason = (
            "no version matching the declaration exists on the configured index"
        )
    elif "none of the wheels" in lowered or "no binary" in lowered or (
        "source distribution" in lowered
    ):
        reason = (
            "the package ships no pre-built wheel for this platform, and the "
            "sandbox has no compiler toolchain"
        )
    elif _baseline_is_implicated(output, baseline) and (
        "resolutionimpossible" in lowered or "conflict" in lowered
    ):
        # Named separately from a plain version conflict because the bound the
        # user cannot move is on the other side: something in the dependency
        # tree needs a version of a package the platform pins. Telling them to
        # "relax one of the bounds" would send them looking for a bound they
        # never wrote.
        #
        # Recognised by the pin's VALUE as well as by the constraints filename:
        # pip's conflict report cites a constrained version as "the user
        # requested (constraint) numpy==2.5.2" — by value, with no path — so a
        # check for the filename alone never fires on real output.
        #
        # By the time this is reached the installer has already released every
        # pin it was allowed to release and retried, so the remaining pin is one
        # the platform holds on purpose: an operator can widen that set
        # (deps.relaxable_packages) or upgrade the baseline, and the user can
        # pick another release.
        reason = (
            "a dependency of the declared packages requires a version of a "
            "package the platform itself provides, and that particular package "
            "is one the platform cannot yield (a compiled extension, or part of "
            "its own runtime). Pick a release of the declared package that "
            "works with the platform baseline, or ask an operator to upgrade "
            "the baseline or to allow this package to be overridden "
            "(deps.relaxable_packages)"
        )
    elif "resolutionimpossible" in lowered or "conflict" in lowered:
        reason = (
            "the declared versions cannot be satisfied together; relax one of "
            "the bounds pip names below"
        )
    elif (
        "connection" in lowered
        or "timed out" in lowered
        or "temporary failure" in lowered
        or "proxy" in lowered
    ):
        reason = (
            "the package index was unreachable from the sandbox; check that the "
            "platform's index host is allowed for dependency installs"
        )
    else:
        reason = f"pip exited with code {exit_code}"

    return f"{reason}.\n\n{tail}"


def _baseline_is_implicated(
    output: str, baseline: Optional[Mapping[str, str]]
) -> bool:
    """Whether a platform pin is one of the bounds pip could not satisfy.

    Two signals, and deliberately not the constraints filename: the transcript
    ECHOES the command, so the filename is present in every install and matches
    every failure — a check on it proves nothing.
    """
    lowered = output.lower()
    # pip cites a constrained version as "the user requested (constraint) X".
    if "(constraint)" in lowered:
        return True
    if not baseline:
        return False
    # pip may cite the pin by value only — "numpy==2.5.2" — which is enough:
    # this runs only for a resolution conflict, where a baseline version
    # appearing at all means the baseline is one of the parties.
    return any(
        version and f"{name}=={version}".lower() in lowered
        for name, version in baseline.items()
    )


# ── overlay inspection ────────────────────────────────────────────────────


def _reset_target(target: Path, transcript: _Transcript) -> None:
    """Remove the overlay tree so an install starts from a known state."""
    if not target.exists():
        return
    try:
        shutil.rmtree(target)
        transcript.write("removed the previous dependency tree")
    except OSError as exc:
        raise SqlMeshOpError(
            ERR_DEPS_INSTALL,
            f"could not clear the previous dependency tree at {target}: {exc}",
        ) from exc


def _overlay_distributions(target: Path) -> dict[str, str]:
    """Read ``name -> version`` from the overlay's own metadata directories.

    Read from the TREE rather than from ``importlib.metadata`` on a modified
    ``sys.path``: importing the overlay into this process would execute the
    packages' own top-level code, which is precisely what this task must not do.
    """
    from packaging.utils import canonicalize_name

    found: dict[str, str] = {}
    if not target.is_dir():
        return found
    for entry in sorted(target.iterdir()):
        if entry.suffix not in (".dist-info", ".egg-info") or not entry.is_dir():
            continue
        stem = entry.name[: -len(entry.suffix)]
        name, _, version = stem.partition("-")
        if not name:
            continue
        found[canonicalize_name(name)] = version
    return found


def _tree_size(target: Path) -> int:
    """On-disk size of the overlay, following no symlinks."""
    total = 0
    for root, _, files in os.walk(target):
        for name in files:
            path = Path(root) / name
            try:
                if path.is_symlink():
                    continue
                total += path.stat().st_size
            except OSError:
                continue
    return total


__all__ = [
    "ERR_DEPS_DECLARATION",
    "ERR_DEPS_INSTALL",
    "ERR_DEPS_PLATFORM_CONFLICT",
    "ERR_DEPS_PLATFORM_TRANSITIVE",
    "ERR_DEPS_SHADOWED",
    "ERR_DEPS_TOO_LARGE",
    "op_deps_sync",
]
