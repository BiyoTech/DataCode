"""Reading and writing the dependency manifest, and the injection decision.

Two callers with very different needs share this module:

* **consumers** — the code location process, the local sandbox backend, the
  Kubernetes Job builder. Each asks :func:`overlay_for_injection` whether it may
  prepend the overlay to ``PYTHONPATH``. This runs on every launch, so it is
  pure file IO plus comparisons, and it never raises: an unreadable manifest
  means "no overlay", which degrades to the platform's own packages rather than
  to a process that will not start;
* **the installer** — :mod:`src.deps.service`, which calls :func:`write_manifest`
  once per install. One writer, atomically, so an interrupted install can never
  leave a manifest describing a tree that is not there.

Why the injection decision lives here and not at each consumer
--------------------------------------------------------------
There are three injection points and they must agree exactly. A consumer that
skipped the interpreter check would put a tree built by Python 3.13 on a 3.11
path, where a compiled extension fails to import with a message that explains
nothing and points at the user's own asset file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from datacode_common.python_deps import (
    DECLARATION_RELPATH,
    STATE_DECLARATION_INVALID,
    STATE_EMPTY,
    STATE_FAILED,
    STATE_INCOMPATIBLE,
    STATE_MISSING,
    STATE_READY,
    DepsDeclaration,
    DepsDeclarationError,
    DepsManifest,
    PlatformConflict,
    install_log_path,
    interpreter_mismatch,
    manifest_path,
    manifest_state,
    parse_declaration,
    site_packages_path,
    utc_now_iso,
)

from src.deps.baseline import conflicts_for
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class DepsStatus:
    """Read-only answer to "what is the state of this copy's dependencies?".

    Returned by the ``ensure`` endpoint on every asset-page open, so it is built
    from three file reads and nothing else — no install, no subprocess, no lock.

    Attributes:
        state: One of the shared ``STATE_*`` values.
        declared: Number of requirements the working copy declares.
        installed: Number of packages present in the overlay per the manifest.
        fingerprint: Fingerprint of the CURRENT declaration.
        message: Why the state is what it is, when that needs saying (a parse
            error, an interpreter mismatch, a failed install). Rendered verbatim
            in the UI, so it always names the next action.
        conflicts: Declared requirements the platform baseline cannot satisfy.
            Reported by the READ path, not only by an install: a reload that
            cannot possibly succeed should be visible before the user starts
            it, and each entry carries the rewrite that would fix it.
        relaxed: Platform pins this copy's overlay overrides, as recorded by
            the install that yielded them. On the read path too, because the
            difference outlives the install: whoever opens the page next has to
            be able to see that this copy does not run the platform's own
            version of these packages.
        manifest: The stored manifest, when there is one.
    """

    state: str
    declared: int = 0
    installed: int = 0
    fingerprint: str = ""
    message: str = ""
    conflicts: tuple[PlatformConflict, ...] = ()
    relaxed: tuple[Mapping[str, str], ...] = ()
    manifest: Optional[DepsManifest] = None

    @property
    def needs_install(self) -> bool:
        """Whether a reload must run an install before starting the location."""
        return self.state not in (STATE_READY, STATE_EMPTY)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "declared": self.declared,
            "installed": self.installed,
            "fingerprint": self.fingerprint,
            "message": self.message,
            "conflicts": [item.to_dict() for item in self.conflicts],
            "relaxed": [dict(item) for item in self.relaxed],
            "installed_at": self.manifest.installed_at if self.manifest else "",
            "size_bytes": self.manifest.size_bytes if self.manifest else 0,
        }


def read_declaration(workspace_path: str) -> DepsDeclaration:
    """Parse the working copy's declaration.

    Raises:
        DepsDeclarationError: The file is malformed. Propagated on purpose: a
            broken declaration must be reported to the user, not silently read
            as "no dependencies", which would produce a reload that succeeds
            while ignoring everything they wrote.
    """
    path = Path(workspace_path) / DECLARATION_RELPATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # A copy created before the asset scaffold existed, or one whose
        # asset-dag/ was deleted. No declaration is a valid state.
        return parse_declaration("")
    except OSError as exc:
        raise DepsDeclarationError(
            f"cannot read {DECLARATION_RELPATH}: {exc}"
        ) from exc
    return parse_declaration(text)


def read_manifest(workspace_path: str) -> Optional[DepsManifest]:
    """Read the stored manifest, or ``None`` when there is none to read.

    Never raises. A manifest that cannot be parsed is treated as absent, which
    the caller resolves as "install needed" — one wasted install instead of a
    permanently broken reload.
    """
    path = Path(manifest_path(workspace_path))
    try:
        return DepsManifest.from_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning(
            "deps_manifest_unreadable",
            workspace_path=workspace_path,
            error=str(exc),
        )
        return None


def write_manifest(workspace_path: str, manifest: DepsManifest) -> None:
    """Persist the manifest atomically.

    ``.tmp`` sibling then ``os.replace``: a consumer starting a code location
    while an install finishes must see either the old manifest or the new one,
    never half a JSON document.
    """
    path = Path(manifest_path(workspace_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(manifest.to_json(), encoding="utf-8")
    os.replace(tmp, path)
    logger.info(
        "deps_manifest_written",
        workspace_path=workspace_path,
        state=manifest.state,
        fingerprint=manifest.fingerprint,
        packages=len(manifest.packages),
        size_bytes=manifest.size_bytes,
    )


def read_status(workspace_path: str) -> DepsStatus:
    """Compare declaration and manifest. Read-only, and cheap by construction."""
    try:
        declaration = read_declaration(workspace_path)
    except DepsDeclarationError as exc:
        # Reported as a distinct state rather than as "missing": the user's next
        # action is to fix the file, not to press reload.
        return DepsStatus(state=STATE_DECLARATION_INVALID, message=str(exc))

    manifest = read_manifest(workspace_path)
    overlay_present = Path(site_packages_path(workspace_path)).is_dir()
    state = manifest_state(
        declaration, manifest, overlay_present=overlay_present
    )
    # Checked on the READ path too, so a declaration that cannot possibly
    # install is visible before the user spends a reload discovering it.
    conflicts = conflicts_for(declaration)

    message = ""
    if manifest is not None:
        if state == STATE_INCOMPATIBLE:
            message = interpreter_mismatch(manifest)
        elif state == STATE_FAILED:
            message = manifest.error_detail or manifest.error_code
    if state == STATE_MISSING and declaration.requirements:
        message = (
            f"{len(declaration.requirements)} declared package(s) are not "
            f"installed in this working copy yet"
        )
    if conflicts:
        # Wins over every message above: nothing else about this copy matters
        # until the declaration and the platform agree, and this is the only
        # message that names what to write instead.
        message = conflicts[0].message()

    return DepsStatus(
        state=state,
        declared=len(declaration.requirements),
        installed=len(manifest.packages) if manifest else 0,
        fingerprint=declaration.fingerprint,
        message=message,
        conflicts=conflicts,
        # Only meaningful while the tree those pins were yielded for is the one
        # installed: reporting them from a failed or drifted manifest would name
        # overrides that are no longer in effect.
        relaxed=manifest.relaxed if manifest and state == STATE_READY else (),
        manifest=manifest,
    )


def overlay_for_injection(workspace_path: str, *, consumer: str) -> str:
    """The directory to prepend to ``PYTHONPATH``, or ``""``.

    Args:
        workspace_path: Working copy AS THE CONSUMER SEES IT. A container passes
            its mount point; the returned path is derived from the argument, so
            the same manifest serves both views.
        consumer: Who is asking (``code_location``, ``sandbox_local``,
            ``sandbox_job``). Logged, so a missing overlay can be traced to the
            process that declined it.

    Returns:
        The overlay path when it exists AND was built for a compatible
        interpreter; otherwise an empty string.

    Never raises. Refusing to inject leaves the consumer with the platform's own
    packages, which is a working process with a clear "module not found" if the
    user's code needs a declared package — strictly better than a process that
    fails to start, or one that imports extensions built for another interpreter.
    """
    manifest = read_manifest(workspace_path)
    if manifest is None or manifest.state != STATE_READY:
        return ""

    overlay = site_packages_path(workspace_path)
    if not Path(overlay).is_dir():
        logger.warning(
            "deps_overlay_missing",
            workspace_path=workspace_path,
            consumer=consumer,
            hint="the manifest claims an installed tree that is not on disk; "
            "reload to reinstall",
        )
        return ""

    mismatch = interpreter_mismatch(manifest)
    if mismatch:
        # Not injected, and said out loud: this is the failure mode that would
        # otherwise appear as an unreadable ImportError inside a user's asset.
        logger.error(
            "deps_overlay_incompatible",
            workspace_path=workspace_path,
            consumer=consumer,
            reason=mismatch,
        )
        return ""

    logger.debug(
        "deps_overlay_injected",
        workspace_path=workspace_path,
        consumer=consumer,
        packages=len(manifest.packages),
    )
    return overlay


def read_install_transcript(workspace_path: str, *, tail_bytes: int = 16384) -> str:
    """Tail of the last install's transcript, for a progress stream or a failure.

    Reads the file the RUNNER writes (see ``INSTALL_LOG_FILENAME``). Empty when
    there is none — a copy whose dependencies were never installed, or a vehicle
    that died before writing anything.
    """
    path = Path(install_log_path(workspace_path))
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return ""


def manifest_from_task_result(
    result: dict,
    *,
    fingerprint: str,
    source: str,
    actor_user_id: str,
) -> DepsManifest:
    """Build a ready manifest from what the runner reported.

    The runner returns facts and never writes the file, so this is where the
    "installed" claim is minted. It uses the fingerprint the RUNTIME resolved,
    not the one echoed by the task: the echo is only useful for spotting a
    mismatch, and trusting it would let a stale spec label a fresh tree.
    """
    return DepsManifest(
        state=STATE_READY,
        fingerprint=fingerprint,
        packages=tuple(_records(result.get("packages"))),
        platform_provided=tuple(_records(result.get("platform_provided"))),
        shadowed=tuple(_records(result.get("shadowed"))),
        relaxed=tuple(_relaxed(result.get("relaxed"))),
        python_version=str(result.get("python_version") or ""),
        platform_tag=str(result.get("platform_tag") or ""),
        installed_at=utc_now_iso(),
        source=source,
        actor_user_id=actor_user_id,
        size_bytes=int(result.get("size_bytes") or 0),
    )


def failed_manifest(
    *,
    fingerprint: str,
    source: str,
    actor_user_id: str,
    error_code: str,
    error_detail: str,
) -> DepsManifest:
    """A manifest recording that the last install failed.

    Written on purpose instead of leaving the previous one in place: the overlay
    has been cleared by then, so a stale "ready" manifest would have consumers
    inject a directory that no longer exists.
    """
    return DepsManifest(
        state=STATE_FAILED,
        fingerprint=fingerprint,
        installed_at=utc_now_iso(),
        source=source,
        actor_user_id=actor_user_id,
        error_code=error_code,
        error_detail=error_detail[:4096],
    )


def _records(value: object) -> list[dict[str, str]]:
    """Coerce a runner-reported ``[{name, version}]`` list."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict) and item.get("name"):
            out.append(
                {"name": str(item["name"]), "version": str(item.get("version") or "")}
            )
    return out


def _relaxed(value: object) -> list[dict[str, str]]:
    """Coerce the runner-reported list of yielded platform pins.

    Same tolerance as ``_records``: this comes from a task result written by a
    possibly different image, and a record this build cannot read is dropped
    rather than allowed to fail an install that already succeeded.
    """
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict) and item.get("name"):
            out.append(
                {
                    "name": str(item["name"]),
                    "platform_version": str(item.get("platform_version") or ""),
                    "overlay_version": str(item.get("overlay_version") or ""),
                    "required_by": str(item.get("required_by") or ""),
                }
            )
    return out


__all__ = [
    "DepsStatus",
    "failed_manifest",
    "manifest_from_task_result",
    "overlay_for_injection",
    "read_declaration",
    "read_install_transcript",
    "read_manifest",
    "read_status",
    "write_manifest",
]
