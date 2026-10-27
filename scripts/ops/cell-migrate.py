#!/usr/bin/env python3
"""Tenant cross-cell migration driver (design 06 P7).

The OPERATOR half of the state machine identity-service's placement package
already owns (draining -> migrating -> verifying -> stable). This tool flips
those states over identity's internal API and moves the tenant's platform
data between the two cells' control planes:

    plan          read-only preflight: placement, cells, membership, counts
    run           the full staged hand-over (see the step list below)
    purge-source  erase the tenant from the source cell after retention

What ``run`` does, in order (design 06 P7 "Cell 迁移工具"):

  1. draining      identity stops issuing NEW tokens for the tenant
                   (login answers 503 while this state holds).
  2. wait          sleep one access-token TTL so every token minted before
                   draining has expired -- no session straddles the move.
  3. migrating     export the tenant's platform rows from the source cell's
                   control plane and import them into the target's. Working
                   copies are NOT migrated: they re-materialise from Git on
                   first lease acquisition, by design.
  4. verifying     the target cell answers /health and its per-table counts
                   equal the export's; the source still holds its copy
                   (retention window).
  5. stable        identity assigns the tenant to the target cell --
                   new tokens carry the new cell_id, the edge routes there.
  6. retention     the source copy is kept (7 days by default) for rollback;
                   ``purge-source`` removes it once that window has passed.

Deployment reconstruction needs no step of its own: the imported environment
rows are exactly what the target cell's reconciler rebuilds orchestrator
deployments from (its missing_deployment repair path) -- by design, the
migration reuses the platform's self-healing rather than duplicating it.

Usage:
    python3 scripts/ops/cell-migrate.py plan  --tenant t1 --target cell-1 ...
    python3 scripts/ops/cell-migrate.py run   --tenant t1 --target cell-1 --yes
    python3 scripts/ops/cell-migrate.py purge-source --tenant t1 --source-cell cell-0

Connections (flags or env):
    --identity-url      IDENTITY_URL       (default http://localhost:8003)
    --source-url        SOURCE_CONTROL_URL (default http://localhost:8004)
    --target-url        TARGET_CONTROL_URL (required for run/plan with target)
    --token             CELL_MIGRATION_TOKEN   guards control /internal/*
    --identity-token    IDENTITY_INTERNAL_TOKEN guards identity /internal/*

Exit code 0 on success; 1 on any failed step; 2 on bad usage/preflight.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from typing import Self

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONTROL_VENV_PYTHON = os.path.join(REPO_ROOT, "control-service", ".venv", "bin", "python")

RETENTION_DAYS = 7
# Tables whose counts are compared during verifying. All migration tables
# participate; the list lives in the control plane, this is just display.
GREEN, RED, YELLOW, CYAN, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m",
)


def _ensure_venv() -> None:
    """Re-exec inside the control-service venv when httpx is missing."""
    if importlib.util.find_spec("httpx") is None:
        if not os.path.exists(CONTROL_VENV_PYTHON):
            print(f"fatal: {CONTROL_VENV_PYTHON} not found", file=sys.stderr)
            sys.exit(2)
        os.execv(
            CONTROL_VENV_PYTHON,
            [CONTROL_VENV_PYTHON, os.path.abspath(__file__)] + sys.argv[1:],
        )


_ensure_venv()

import httpx


class Step:
    """One migration step's outcome, printed uniformly."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> Self:
        print(f"{CYAN}▸ {self.name}{RESET}")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            print(f"  {RED}✗ {self.name} failed: {exc}{RESET}")
            return False  # propagate
        print(f"  {GREEN}✓ {self.name}{RESET}")
        return True


def die(message: str, code: int = 1) -> None:
    print(f"{RED}fatal: {message}{RESET}", file=sys.stderr)
    sys.exit(code)


# ── API client wrappers ─────────────────────────────────────────────────────


class Identity:
    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {"X-Internal-Token": token}

    def placement(self, tenant_id: str) -> dict:
        response = httpx.get(
            f"{self._url}/internal/tenants/{tenant_id}/placement",
            headers=self._headers,
            timeout=15,
        )
        if response.status_code != 200:
            raise RuntimeError(f"placement read: HTTP {response.status_code}")
        return response.json()

    def set_placement(self, tenant_id: str, body: dict) -> dict:
        response = httpx.post(
            f"{self._url}/internal/tenants/{tenant_id}/placement",
            headers=self._headers,
            json=body,
            timeout=15,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"placement write {body}: HTTP {response.status_code} {response.text}"
            )
        return response.json()

    def cells(self) -> list[dict]:
        response = httpx.get(
            f"{self._url}/internal/cells", headers=self._headers, timeout=15
        )
        if response.status_code != 200:
            raise RuntimeError(f"cells list: HTTP {response.status_code}")
        cells = response.json()
        return cells if isinstance(cells, list) else cells.get("cells", [])

    def members(self, tenant_id: str) -> list[str]:
        response = httpx.get(
            f"{self._url}/internal/tenants/{tenant_id}/members",
            headers=self._headers,
            timeout=15,
        )
        if response.status_code != 200:
            raise RuntimeError(f"tenant members: HTTP {response.status_code}")
        members = response.json().get("members", [])
        user_ids = []
        for member in members:
            # Member objects carry the id under either key depending on the
            # endpoint flavour; both mean "this user".
            user_id = member.get("user_id") or member.get("id") or ""
            if user_id:
                user_ids.append(user_id)
        return user_ids


class ControlPlane:
    """One cell's control-service, speaking the migration surface."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {"X-Internal-Token": token}

    def health(self) -> bool:
        try:
            return httpx.get(f"{self._url}/health", timeout=10).status_code == 200
        except httpx.HTTPError:
            return False

    def export(self, tenant_id: str, user_ids: list[str]) -> dict:
        response = httpx.get(
            f"{self._url}/internal/cell-migration/{tenant_id}/export",
            headers=self._headers,
            params={"user_ids": ",".join(user_ids)},
            timeout=300,
        )
        if response.status_code != 200:
            raise RuntimeError(f"export: HTTP {response.status_code} {response.text}")
        return response.json()

    def import_(self, tenant_id: str, payload: dict) -> dict:
        response = httpx.post(
            f"{self._url}/internal/cell-migration/{tenant_id}/import",
            headers=self._headers,
            json=payload,
            timeout=300,
        )
        if response.status_code != 200:
            raise RuntimeError(f"import: HTTP {response.status_code} {response.text}")
        return response.json()

    def verify(self, tenant_id: str, user_ids: list[str]) -> dict:
        response = httpx.get(
            f"{self._url}/internal/cell-migration/{tenant_id}/verify",
            headers=self._headers,
            params={"user_ids": ",".join(user_ids)},
            timeout=60,
        )
        if response.status_code != 200:
            raise RuntimeError(f"verify: HTTP {response.status_code} {response.text}")
        return response.json()["counts"]

    def purge(self, tenant_id: str, user_ids: list[str]) -> dict:
        response = httpx.post(
            f"{self._url}/internal/cell-migration/{tenant_id}/purge",
            headers=self._headers,
            params={"user_ids": ",".join(user_ids)},
            timeout=120,
        )
        if response.status_code != 200:
            raise RuntimeError(f"purge: HTTP {response.status_code} {response.text}")
        return response.json()["deleted"]


# ── preflight ───────────────────────────────────────────────────────────────


def _preflight(args, identity: Identity) -> tuple[dict, list[str]]:
    """Shared checks for plan/run: tenant exists, target cell is real."""
    placement = identity.placement(args.tenant)
    print(f"  placement : cell={placement['cell_id']} state={placement['migration_state']}")
    if placement.get("target_cell_id"):
        print(f"              target={placement['target_cell_id']}")

    user_ids = identity.members(args.tenant)
    print(f"  members   : {len(user_ids)} user(s)")

    if args.command in ("plan", "run"):
        cells = identity.cells()
        target = next((c for c in cells if c["cell_id"] == args.target), None)
        if target is None:
            die(f"target cell {args.target!r} is not registered with identity", 2)
        print(
            f"  target    : {target['cell_id']} status={target['status']} "
            f"load={target['assigned_tenants']}/{target['capacity_tenants']}"
        )
        if target["status"] != "active":
            die(f"target cell {args.target!r} is not active", 2)
        if placement["cell_id"] == args.target:
            die(f"tenant already lives on {args.target}", 2)
        if placement["migration_state"] not in ("", "stable"):
            die(
                f"tenant is mid-migration (state={placement['migration_state']}); "
                "finish or roll back that move first",
                2,
            )
    return placement, user_ids


# ── commands ────────────────────────────────────────────────────────────────


def cmd_plan(args) -> int:
    identity = Identity(args.identity_url, args.identity_token)
    source = ControlPlane(args.source_url, args.token)

    with Step("preflight"):
        _preflight(args, identity)

    with Step("source data inventory"):
        if not source.health():
            die(f"source control plane {args.source_url} is not healthy", 2)
        # A full export is the only honest inventory; it is read-only.
        user_ids = identity.members(args.tenant)
        export = source.export(args.tenant, user_ids)
        counts = {name: len(rows) for name, rows in export["tables"].items()}
        print(f"  counts    : {json.dumps(counts)}")

    print(f"\n{GREEN}plan: migration {args.tenant} -> {args.target} is viable{RESET}")
    print("  run with: cell-migrate.py run --tenant ... --target ... --yes")
    return 0


def cmd_run(args) -> int:
    identity = Identity(args.identity_url, args.identity_token)
    source = ControlPlane(args.source_url, args.token)
    target = ControlPlane(args.target_url, args.token)

    with Step("preflight"):
        placement, user_ids = _preflight(args, identity)
        if not source.health():
            die(f"source control plane {args.source_url} is not healthy", 2)
        if not target.health():
            die(f"target control plane {args.target_url} is not healthy", 2)

    if not args.yes:
        answer = input(
            f"\nMigrate tenant {args.tenant!r} from {placement['cell_id']} to "
            f"{args.target}? Logins will answer 503 during the window. [yes/N] "
        )
        if answer.strip().lower() != "yes":
            print("aborted.")
            return 1

    # 1. draining — identity stops minting tokens for this tenant.
    with Step("draining (new logins will be refused)"):
        identity.set_placement(
            args.tenant,
            {"migration_state": "draining", "target_cell_id": args.target},
        )

    # 2. wait out the access-token TTL so no live session straddles the move.
    with Step(f"waiting out token TTL ({args.token_ttl}s)"):
        if args.token_ttl > 0:
            time.sleep(args.token_ttl)

    # 3. migrating — the data half, between the two control planes.
    export = None
    with Step("migrating data (export -> import)"):
        export = source.export(args.tenant, user_ids)
        expected = {name: len(rows) for name, rows in export["tables"].items()}
        print(f"  exported  : {json.dumps(expected)}")
        imported = target.import_(args.tenant, export)
        if imported["counts"] != expected:
            raise RuntimeError(
                f"import counts diverge: {imported['counts']} != {expected}"
            )
        print(
            "  note      : orchestrator deployments are rebuilt by the target "
            "cell's reconciler (missing_deployment repair) -- no action needed"
        )

    # 4. verifying — the target answers, holds the same rows, source kept.
    with Step("verifying (target health + counts + source retention)"):
        if not target.health():
            raise RuntimeError("target control plane stopped answering /health")
        actual = target.verify(args.tenant, user_ids)
        expected = {name: len(rows) for name, rows in export["tables"].items()}
        if actual != expected:
            raise RuntimeError(f"verify counts diverge: {actual} != {expected}")
        print(f"  target    : {json.dumps(actual)}")
        still_there = source.verify(args.tenant, user_ids)
        if still_there != expected:
            print(
                f"  {YELLOW}warning: source counts changed during the window "
                f"({still_there}); retention copy may be stale{RESET}"
            )
        else:
            print(f"  source    : retention copy intact ({RETENTION_DAYS} days)")

    # 5. stable — the switch: new tokens carry the target cell.
    with Step("stabilizing on the target cell"):
        placement = identity.set_placement(args.tenant, {"cell_id": args.target})
        if placement["cell_id"] != args.target or placement["migration_state"] != "stable":
            raise RuntimeError(f"unexpected final placement: {placement}")
        print(f"  placement : cell={placement['cell_id']} state={placement['migration_state']}")

    print(
        f"\n{GREEN}migration complete: {args.tenant} now lives on {args.target}.{RESET}\n"
        f"Working copies re-materialise from Git on first use. The source copy\n"
        f"is retained for {RETENTION_DAYS} days; remove it with:\n"
        f"  cell-migrate.py purge-source --tenant {args.tenant} "
        f"--source-url {args.source_url}"
    )
    return 0


def cmd_purge_source(args) -> int:
    identity = Identity(args.identity_url, args.identity_token)
    source = ControlPlane(args.source_url, args.token)

    with Step("preflight"):
        placement = identity.placement(args.tenant)
        if placement["migration_state"] not in ("", "stable"):
            die("tenant is mid-migration; purging the source now would lose data", 2)
        # The tenant must NOT still be served by the cell we are erasing.
        if placement["cell_id"] == args.source_cell:
            die(
                f"tenant still lives on {args.source_cell}; refusing to erase "
                "its serving cell",
                2,
            )
        user_ids = identity.members(args.tenant)

    if not args.yes:
        answer = input(
            f"\nPERMANENTLY erase tenant {args.tenant!r} from cell "
            f"{args.source_cell}? This cannot be undone. [yes/N] "
        )
        if answer.strip().lower() != "yes":
            print("aborted.")
            return 1

    with Step("purging the source cell"):
        deleted = source.purge(args.tenant, user_ids)
        print(f"  deleted   : {json.dumps(deleted)}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cell-migrate",
        description="Tenant cross-cell migration driver (design 06 P7)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, needs_target: bool) -> None:
        p.add_argument("--tenant", required=True)
        if needs_target:
            p.add_argument("--target", required=True, help="target cell id")
        p.add_argument(
            "--identity-url",
            default=os.getenv("IDENTITY_URL", "http://localhost:8003"),
        )
        p.add_argument(
            "--source-url",
            default=os.getenv("SOURCE_CONTROL_URL", "http://localhost:8004"),
        )
        p.add_argument(
            "--target-url",
            default=os.getenv("TARGET_CONTROL_URL", ""),
            help="target cell's control-service URL",
        )
        p.add_argument("--token", default=os.getenv("CELL_MIGRATION_TOKEN", ""))
        p.add_argument(
            "--identity-token",
            default=os.getenv("IDENTITY_INTERNAL_TOKEN", ""),
        )
        p.add_argument("--yes", action="store_true", help="skip confirmations")

    p_plan = sub.add_parser("plan", help="read-only preflight")
    common(p_plan, needs_target=True)

    p_run = sub.add_parser("run", help="run the full migration")
    common(p_run, needs_target=True)
    p_run.add_argument(
        "--token-ttl",
        type=int,
        default=900,
        help="access-token TTL to wait out (s); 0 skips the wait",
    )

    p_purge = sub.add_parser("purge-source", help="erase the source copy after retention")
    common(p_purge, needs_target=False)
    p_purge.add_argument(
        "--source-cell", required=True, help="the cell being erased FROM"
    )

    args = parser.parse_args()

    if not args.token:
        die("cell migration token is required (--token / CELL_MIGRATION_TOKEN)", 2)
    if not args.identity_token:
        die("identity internal token is required (--identity-token)", 2)
    if args.command in ("plan", "run") and not args.target_url:
        die("target control URL is required for plan/run (--target-url)", 2)

    handlers = {"plan": cmd_plan, "run": cmd_run, "purge-source": cmd_purge_source}
    try:
        return handlers[args.command](args)
    except RuntimeError as exc:
        die(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
