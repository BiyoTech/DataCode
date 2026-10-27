#!/usr/bin/env python3
"""End-to-end check of the working-copy dependency plane. No network needed.

Everything below the HTTP surface is production code: the real reload job, the
real dependency service, the real task submitter, the real isolation policy, the
real local sandbox backend, the real task-runner process and the REAL pip. Only
two things are arranged for the test:

* a **local package index** in PEP 503 layout, holding one hand-built pure-Python
  wheel. That is what makes this runnable offline while still exercising the true
  install path — pip resolves, downloads and unpacks for real;
* a temporary working copy with a five-segment layout.

What it proves, in order:

1. ``ensure`` reports drift and installs NOTHING (it is on the page-load path);
2. a reload installs the declared package, writes the manifest and reaches
   ``ready``;
3. the installed tree is importable through the overlay the platform injects —
   the whole point of the feature;
4. a second reload skips the install (the fingerprint fast path);
5. an undeclarable version FAILS the reload, leaves the manifest ``failed``,
   never starts the code location, and archives the reason under
   ``{workspace}/logs/errors/`` where the agent looks;
6. ``pip install`` from a sandboxed command is refused with the alternative.

Usage:  python scripts/verify/verify-workspace-deps.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "workspace-runtime"

_PASS: list[str] = []
_FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (_PASS if ok else _FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not ok:
        line += f"\n       {detail}"
    print(line, flush=True)


# ── re-exec into the runtime's interpreter ────────────────────────────────
#
# By INTERPRETER PATH, not by probing for a package: the host python may happen
# to have fastapi and lack sqlalchemy, which would make a probe conclude the
# wrong thing (the same trap the workspace-lifecycle verifier documents).
def _reexec() -> None:
    venv_python = RUNTIME_ROOT / ".venv" / "bin" / "python"
    if Path(sys.executable).resolve() == venv_python.resolve():
        return
    if not venv_python.is_file():
        print(f"missing {venv_python}; create the runtime venv first", file=sys.stderr)
        raise SystemExit(2)
    os.execv(str(venv_python), [str(venv_python), *sys.argv])


_reexec()
sys.path.insert(0, str(RUNTIME_ROOT))


# ── a local index with one real wheel ─────────────────────────────────────

_WHEEL_NAME = "datacode_probe"
_WHEEL_VERSION = "1.0.0"


def _build_wheel(directory: Path) -> Path:
    """Write a minimal, valid pure-Python wheel. No build backend involved."""
    dist_info = f"{_WHEEL_NAME}-{_WHEEL_VERSION}.dist-info"
    module = f"{_WHEEL_NAME}.py"
    entries = {
        module: 'VALUE = "installed-from-overlay"\n',
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {_WHEEL_NAME}\n"
            f"Version: {_WHEEL_VERSION}\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: datacode-verify\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    records = []
    for name, text in entries.items():
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(text.encode()).digest()
        ).rstrip(b"=").decode()
        records.append(f"{name},sha256={digest},{len(text)}")
    records.append(f"{dist_info}/RECORD,,")
    entries[f"{dist_info}/RECORD"] = "\n".join(records) + "\n"

    path = directory / f"{_WHEEL_NAME}-{_WHEEL_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
    return path


def _build_index(root: Path) -> str:
    """PEP 503 simple index over ``file://``, so pip needs no network."""
    simple = root / "simple"
    package_dir = simple / _WHEEL_NAME.replace("_", "-")
    package_dir.mkdir(parents=True)
    wheel = _build_wheel(package_dir)
    (package_dir / "index.html").write_text(
        f'<a href="{wheel.name}">{wheel.name}</a>\n', encoding="utf-8"
    )
    (simple / "index.html").write_text(
        f'<a href="{package_dir.name}/">{package_dir.name}</a>\n', encoding="utf-8"
    )
    return simple.as_uri()


# ── the working copy ──────────────────────────────────────────────────────


def _make_copy(base: Path, requirements: str) -> str:
    copy = base / "t1" / "ws-t1-probe" / "u1" / "envs" / "dev"
    (copy / "asset-dag").mkdir(parents=True)
    (copy / "asset-dag" / "pyproject.toml").write_text(
        f'[project]\nname = "asset-dag"\ndependencies = [{requirements}]\n',
        encoding="utf-8",
    )
    (copy / "asset-dag" / "definitions.py").write_text("defs = None\n", encoding="utf-8")
    return str(copy)


def _declare(copy: str, requirements: str) -> None:
    Path(copy, "asset-dag", "pyproject.toml").write_text(
        f'[project]\nname = "asset-dag"\ndependencies = [{requirements}]\n',
        encoding="utf-8",
    )


# ── stubs at the two narrowest seams ──────────────────────────────────────


class _FakeRuntime:
    def __init__(self, alive: bool = True) -> None:
        self.host = "127.0.0.1"
        self.port = 4001
        self.definitions = "definitions.py"
        self.alive = alive


class _FakeCodeLocations:
    """A real one needs a Dagster gRPC server; what matters is IF it is asked."""

    def __init__(self) -> None:
        self.reloads = 0

    async def reload(self, spec):
        self.reloads += 1
        return _FakeRuntime()

    async def ensure(self, spec):
        return _FakeRuntime()

    def load_failure(self, name):
        return {}

    def get(self, name):
        return None

    def port_of(self, name):
        return 0


async def _drain(store, job_id: str, timeout: float = 240.0) -> object:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = store.get(job_id)
        if job is not None and job.terminal:
            return job
        await asyncio.sleep(0.2)
    raise TimeoutError("the reload job never finished")


async def main() -> int:
    from src.codeloc import deployments as domain
    from src.codeloc.reload_jobs import get_reload_job_store
    from src.config import settings
    from src.tools.command_router import classify, get_command_router

    work = Path(tempfile.mkdtemp(prefix="datacode-verify-deps-"))
    try:
        index_url = _build_index(work / "index")
        base = work / "workspaces"
        copy = _make_copy(base, f'"{_WHEEL_NAME}=={_WHEEL_VERSION}"')

        # Real execution plane: one hardened child process per task, the dev
        # vehicle. The isolation policy routes deps.sync here because the
        # container profiles are unavailable on this machine.
        settings.workspace_base_path = str(base)
        settings.task_backend = "local"
        settings.deps_index_url = index_url
        settings.deps_max_bytes = 512 * 1024 * 1024
        settings.deps_install_timeout_seconds = 180
        # No egress proxy in dev, so no grant is published and pip talks to the
        # file:// index directly — the same path a dev install takes.
        settings.egress_proxy_url = ""

        locations = _FakeCodeLocations()
        manager = domain.DeploymentManager(code_locations=locations)
        store = get_reload_job_store()
        spec = manager.resolve(copy)

        # 1 ── ensure reports drift and installs nothing.
        info = await manager.ensure(spec)
        deps = info.deps or {}
        check(
            "ensure reports the declared package as missing",
            deps.get("state") == "missing" and deps.get("declared") == 1,
            f"deps={deps}",
        )
        check(
            "ensure installed nothing",
            not (Path(copy) / ".datacode" / "deps" / "site-packages").exists(),
        )

        # 2 ── a reload installs it for real.
        job = await manager.start_reload_job(spec, source="cli", actor_user_id="u1")
        final = await _drain(store, job.job_id)
        check(
            "the reload succeeded",
            final.state == "succeeded" and final.stage == "ready",
            f"state={final.state} stage={final.stage} "
            f"reason={final.failure_code} {final.message}\n{final.output_tail[-1500:]}",
        )
        manifest_path = Path(copy) / ".datacode" / "deps" / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        check(
            "the manifest records the installed package",
            [entry["name"] for entry in manifest.get("packages", [])]
            == [_WHEEL_NAME.replace("_", "-")],
            f"manifest={manifest}",
        )
        check("the code location was started once", locations.reloads == 1)

        # 3 ── the tree is importable through the overlay the platform injects.
        overlay = Path(copy) / ".datacode" / "deps" / "site-packages"
        probe = subprocess.run(
            [sys.executable, "-c", f"import {_WHEEL_NAME} as m; print(m.VALUE)"],
            env={**os.environ, "PYTHONPATH": str(overlay)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        check(
            "the installed package imports through the overlay",
            probe.stdout.strip() == "installed-from-overlay",
            f"stdout={probe.stdout!r} stderr={probe.stderr[-500:]!r}",
        )
        probe_without = subprocess.run(
            [sys.executable, "-c", f"import {_WHEEL_NAME}"],
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        check(
            "and is absent without it (so the overlay is what provided it)",
            probe_without.returncode != 0,
        )

        # 4 ── the fingerprint fast path.
        store._active.clear()
        second = await manager.start_reload_job(spec, source="cli")
        final2 = await _drain(store, second.job_id)
        installed_at = json.loads(manifest_path.read_text())["installed_at"]
        check(
            "an unchanged declaration is not reinstalled",
            final2.state == "succeeded" and installed_at == manifest["installed_at"],
            f"state={final2.state}",
        )

        # 5 ── an impossible declaration fails the reload, visibly.
        _declare(copy, f'"{_WHEEL_NAME}==9.9.9"')
        store._active.clear()
        before = locations.reloads
        third = await manager.start_reload_job(spec, source="ui", actor_user_id="u1")
        final3 = await _drain(store, third.job_id)
        check(
            "an uninstallable declaration fails the reload",
            final3.state == "failed" and final3.failure_code == "deps_install_failed",
            f"state={final3.state} reason={final3.failure_code}",
        )
        check(
            "and the code location was NOT started",
            locations.reloads == before,
        )
        check(
            "the failure names the package and pip's own words",
            "9.9.9" in final3.output_tail or "9.9.9" in final3.message,
            f"tail={final3.output_tail[-800:]!r}",
        )
        failed_manifest = json.loads(manifest_path.read_text())
        check(
            "the manifest is marked failed, so no consumer injects the tree",
            failed_manifest["state"] == "failed",
        )
        archived = sorted((Path(copy) / "logs" / "errors").glob("*.json"))
        record = json.loads(archived[-1].read_text()) if archived else {}
        check(
            "the failure is archived under logs/errors/ for the agent",
            bool(archived) and record.get("error_code") == "DEPS_INSTALL_FAILED",
            f"archived={[p.name for p in archived]}",
        )
        check(
            "and the job envelope points at that record",
            final3.error_log_id == record.get("error_id"),
            f"job={final3.error_log_id!r} record={record.get('error_id')!r}",
        )

        # 6 ── pip install is refused with the alternative named.
        routed = classify("pip install requests")
        refusal = (
            await get_command_router().execute(
                routed, workspace_path=copy, environment="dev"
            )
            if routed is not None
            else {}
        )
        check(
            "pip install is refused and points at the declaration",
            routed is not None
            and refusal.get("exit_code") != 0
            and "pyproject.toml" in refusal.get("stderr", "")
            and "dc deps sync" in refusal.get("stderr", ""),
            f"routed={routed} result={refusal}",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        from src.tasks.submitter import shutdown_task_submitter

        await shutdown_task_submitter()

    print(f"\n{len(_PASS)} PASS / {len(_FAIL)} FAIL")
    if _FAIL:
        print("failed: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
