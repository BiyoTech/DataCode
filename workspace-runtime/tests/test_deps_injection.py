"""Where the installed dependency tree is put on ``sys.path``, and where it is not.

Three processes consume the overlay and they must agree exactly. The failure mode
of a disagreement is silent: the variable is set, nothing is importable, and the
error surfaces inside the user's own asset as a ``ModuleNotFoundError``.

Asserted per injection point, plus the two cases that must NOT inject:

* the installer itself — importing a half-written overlay into the process that
  builds it would let a failed install poison its own retry;
* a tree built by another interpreter — its compiled extensions cannot load, and
  the resulting ImportError explains nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from datacode_common.python_deps import (
    STATE_READY,
    TASK_KIND_DEPS_SYNC,
    DepsManifest,
    interpreter_platform_tag,
    interpreter_python_version,
    site_packages_path,
)
from datacode_common.task_protocol import TaskSpec

from src.deps.manifest import overlay_for_injection

_WORKSPACE_SEGMENTS = ("t1", "ws-t1-shop", "u1", "envs", "dev")


def _copy(tmp_path: Path) -> str:
    copy = tmp_path.joinpath("base", *_WORKSPACE_SEGMENTS)
    copy.mkdir(parents=True)
    return str(copy)


def _install(workspace_path: str, **overrides) -> DepsManifest:
    """Write a manifest and create the overlay directory it describes."""
    fields = {
        "state": STATE_READY,
        "fingerprint": "fp",
        "packages": ({"name": "polars", "version": "1.2.0"},),
        "python_version": interpreter_python_version(),
        "platform_tag": interpreter_platform_tag(),
    }
    fields.update(overrides)
    manifest = DepsManifest(**fields)

    root = Path(workspace_path) / ".datacode" / "deps"
    (root / "site-packages").mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
    return manifest


# ── the decision ──────────────────────────────────────────────────────────


def test_a_ready_tree_is_offered_to_every_consumer(tmp_path):
    path = _copy(tmp_path)
    _install(path)

    for consumer in ("code_location", "sandbox_local", "sandbox_job"):
        assert overlay_for_injection(path, consumer=consumer) == site_packages_path(path)


@pytest.mark.parametrize(
    "damage",
    [
        "no-manifest",
        "no-directory",
        "failed-state",
        "other-python",
        "other-platform",
        "unrecorded-interpreter",
    ],
)
def test_an_unusable_tree_is_never_injected(tmp_path, damage):
    """Declining leaves the platform's own packages, which is a working process.

    Injecting instead produces either an unimportable directory or extensions
    built for another interpreter — both failures land inside user code.
    """
    path = _copy(tmp_path)
    if damage == "no-manifest":
        (Path(path) / ".datacode" / "deps" / "site-packages").mkdir(parents=True)
    elif damage == "no-directory":
        _install(path)
        os.rmdir(Path(path) / ".datacode" / "deps" / "site-packages")
    elif damage == "failed-state":
        _install(path, state="failed", error_code="deps_install_failed")
    elif damage == "other-python":
        _install(path, python_version="3.9")
    elif damage == "other-platform":
        _install(path, platform_tag="linux-s390x")
    else:
        _install(path, python_version="", platform_tag="")

    assert overlay_for_injection(path, consumer="code_location") == ""


def test_a_corrupt_manifest_is_treated_as_absent(tmp_path):
    """Never raises: a reload must not become impossible because of cached state."""
    path = _copy(tmp_path)
    _install(path)
    (Path(path) / ".datacode" / "deps" / "manifest.json").write_text("{not json")

    assert overlay_for_injection(path, consumer="code_location") == ""


# ── injection point 1: the code location ──────────────────────────────────


def _location_entry(workspace_path: str):
    from src.codeloc.manager import CodeLocationSpec, _LocationEntry

    return _LocationEntry(
        CodeLocationSpec(
            location_name="ws_t1_shop__dev",
            workspace_path=workspace_path,
            workspace_name="ws-t1-shop",
            env={"DATACODE_ENVIRONMENT": "dev"},
        ),
        port=4001,
    )


def test_the_process_vehicle_prepends_the_real_path(tmp_path, monkeypatch):
    """PREPENDED: sys.path reaches PYTHONPATH before the image's site-packages,
    so an appended entry would never win for a name the image also has."""
    from src.codeloc.manager import CodeLocationManager

    path = _copy(tmp_path)
    _install(path)
    monkeypatch.setenv("PYTHONPATH", "/opt/platform")
    manager = CodeLocationManager()
    monkeypatch.setattr(type(manager), "vehicle", property(lambda _self: "process"))

    env = manager._code_location_env(_location_entry(path))

    assert env["PYTHONPATH"].split(os.pathsep) == [
        site_packages_path(path),
        "/opt/platform",
    ]


def test_the_pod_vehicle_uses_the_container_mount_point(tmp_path, monkeypatch):
    """The copy is mounted at /workspace inside the Pod.

    A runtime-side path here would name a directory that does not exist there,
    and the runtime's own PYTHONPATH must not travel: those directories belong to
    this image, not to that one.
    """
    from src.codeloc.manager import CodeLocationManager

    path = _copy(tmp_path)
    _install(path)
    monkeypatch.setenv("PYTHONPATH", "/opt/platform")
    manager = CodeLocationManager()
    monkeypatch.setattr(type(manager), "vehicle", property(lambda _self: "pod"))

    env = manager._code_location_env(_location_entry(path))

    assert env["PYTHONPATH"] == "/workspace/.datacode/deps/site-packages"


def test_a_location_without_an_installed_tree_gets_no_variable(tmp_path):
    """Absent, not empty: an empty PYTHONPATH entry means the CWD."""
    from src.codeloc.manager import CodeLocationManager

    path = _copy(tmp_path)
    env = CodeLocationManager()._code_location_env(_location_entry(path))
    assert "PYTHONPATH" not in env


# ── injection point 2: the local sandbox backend ───────────────────────────


def _spec(workspace_path: str, kind: str = "agent.shell") -> TaskSpec:
    return TaskSpec(task_id="t1", kind=kind, workspace_path=workspace_path)


def test_the_local_backend_prepends_the_overlay(tmp_path):
    from src.tasks.backends.local import LocalProcessBackend

    path = _copy(tmp_path)
    _install(path)
    backend = LocalProcessBackend()

    resolved = backend._python_path_for(_spec(path))

    assert resolved[0] == site_packages_path(path)
    # The runner's own roots stay, or the vehicle cannot import the runner.
    assert len(resolved) == len(backend._python_path) + 1


def test_the_installer_task_gets_no_overlay_locally(tmp_path):
    """It is the process that BUILDS the tree."""
    from src.tasks.backends.local import LocalProcessBackend

    path = _copy(tmp_path)
    _install(path)
    backend = LocalProcessBackend()

    resolved = backend._python_path_for(_spec(path, TASK_KIND_DEPS_SYNC))

    assert resolved == backend._python_path


# ── injection point 3: the Kubernetes Job ─────────────────────────────────


def test_the_job_env_uses_the_container_mount_point(tmp_path):
    """Decision from the runtime's view of the copy, VALUE in the container's."""
    from src.tasks.backends.kubernetes import KubernetesBackend

    path = _copy(tmp_path)
    _install(path)

    assert (
        KubernetesBackend._overlay_env(_spec(path))
        == "/workspace/.datacode/deps/site-packages"
    )


def test_the_job_env_is_empty_for_the_installer_and_for_no_tree(tmp_path):
    from src.tasks.backends.kubernetes import KubernetesBackend

    path = _copy(tmp_path)
    assert KubernetesBackend._overlay_env(_spec(path)) == ""

    _install(path)
    assert KubernetesBackend._overlay_env(_spec(path, TASK_KIND_DEPS_SYNC)) == ""


# ── the two views agree ───────────────────────────────────────────────────


def test_both_views_are_derived_from_one_relative_layout(tmp_path):
    """The same manifest serves a host path and a mount point.

    This is why the tree is a `--target` site-packages and not a virtualenv: a
    venv pins absolute paths into pyvenv.cfg and every shebang, so it stops
    working through the second view.
    """
    path = _copy(tmp_path)
    _install(path)
    relative = os.path.join(".datacode", "deps", "site-packages")

    assert overlay_for_injection(path, consumer="code_location").endswith(relative)
    assert site_packages_path("/workspace").endswith(relative)

    payload = json.loads(
        (Path(path) / ".datacode" / "deps" / "manifest.json").read_text()
    )
    # Nothing absolute is recorded: a path in the manifest would be the very
    # coupling this layout avoids.
    assert path not in json.dumps(payload)
