"""Ownership contract: this service runs NO control-plane background loop.

A long-running loop in the data plane is only legitimate when it maintains
state only THIS replica can see or defend:

* the heartbeat publishes this process's own lease count and memory figure;
* the lease watchdog renews leases this process holds (renewal presupposes
  holding — renewing from elsewhere keeps a dead holder alive on paper);
* the WIP snapshotter and the disk reclaimer read and free this replica's own
  volume;
* the Dagster supervisor restarts children this process spawned (only a parent
  can reap them).

Everything that reconciles the PLATFORM — expiring sandboxes, sweeping drift,
enforcing retention, deciding replica counts — belongs to control-service.
That split is what keeps the two failure domains apart: a runtime replica can
be killed, drained or scaled at any moment, so a platform-wide loop living
there would either stop when the fleet shrinks or run N times over.

This is not hypothetical tidiness. The runtime shipped a verbatim copy of
control's `environments:` configuration block — the per-workspace ceiling, the
sandbox TTL, the expiry reaper interval — with ZERO readers on this side: five
keys that looked like policy and were decoration. A reader added later would
have silently started a second reaper. The block is gone, and the tests below
are what stop it (or its moral equivalent) from coming back.

The scan is deliberately crude — module names and symbol names, not semantics.
A guard that tried to judge intent would be the thing needing a guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
CONFIG = SRC / "config.py"

# Control-plane concerns, by the name they would carry if they appeared here.
# Each entry names a loop control-service already runs; a second one in the
# data plane would run once per replica.
CONTROL_PLANE_LOOPS = {
    "environment_reaper",
    "reconcile_sweep",
    "record_retention",
    "lease_controller",
    "capacity_controller",
    "quota_publisher",
    "operation_resumer",
}

# Settings fields that would mean this service configures such a loop.
FORBIDDEN_SETTINGS = {
    "environment_reaper_enabled",
    "environment_reaper_interval",
    "sandbox_expiry_grace_hours",
    "sandbox_default_ttl_hours",
    "max_environments_per_workspace",
    "reconcile_sweep_enabled",
    "reconcile_sweep_interval",
    "retention_enabled",
    "capacity_controller_enabled",
}


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_control_plane_loop_is_configured_here() -> None:
    """A Settings field for a platform loop means this service intends to run one.

    The five `environments.*` fields this caught were dead: declared, mapped
    from YAML, and read by nothing. Dead configuration is worse than absent —
    it reads as a promise the service does not keep.
    """
    tree = ast.parse(CONFIG.read_text(), filename=str(CONFIG))
    declared = {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    offenders = sorted(declared & FORBIDDEN_SETTINGS)
    assert not offenders, (
        f"config.py declares {offenders}, which configure control-plane loops. "
        "Those belong to control-service (conf/control-service.yaml); a copy "
        "here is either dead configuration or a second loop running once per "
        "replica."
    )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_module_implements_a_control_plane_loop(path: Path) -> None:
    """Named after a control-plane concern = owned by the control plane."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        name = node.name.lower().lstrip("_")
        for concern in CONTROL_PLANE_LOOPS:
            if name.startswith(concern) or name == concern.replace("_", ""):
                pytest.fail(
                    f"{path.relative_to(SRC)}:{node.lineno} defines {node.name!r}, "
                    f"a {concern.replace('_', ' ')} — a platform-wide loop. It "
                    "belongs to control-service: here it would run once per "
                    "replica and stop when the fleet is scaled down."
                )


def test_background_loops_are_declared_where_they_are_owned() -> None:
    """Every resident loop this service starts must be a self-maintenance one.

    Pinned as an explicit inventory rather than a pattern: adding a loop is a
    decision, and this list is where the reviewer is forced to state which of
    the two kinds it is.
    """
    # name= given to asyncio.create_task, which is also what shows up in a
    # stack dump — the inventory and the runtime evidence stay the same words.
    expected = {
        "lease-heartbeat",         # this replica's own capacity figure
        "wip-snapshot",            # this replica's own working copies
        "disk-reclaimer",          # this replica's own volume
        "controller-drain",        # this replica surrendering its own leases
        "port-segment-heartbeat",  # this replica's own code-location port range
    }
    found: set[str] = set()
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_create_task = (
                isinstance(func, ast.Attribute) and func.attr == "create_task"
            )
            if not is_create_task:
                continue
            for keyword in node.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    found.add(str(keyword.value.value))

    unexpected = found - expected
    assert not unexpected, (
        f"new named background loops: {sorted(unexpected)}. If one maintains "
        "state only this replica can see, add it to the inventory above. If it "
        "reconciles the platform, it belongs to control-service."
    )
