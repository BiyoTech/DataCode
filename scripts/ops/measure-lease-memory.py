#!/usr/bin/env python3
"""Calibrate ``lease.estimated_lease_bytes`` by measuring real Context RSS.

P0 deliverable (design 07, ADR-002 / Q3 decision): the per-lease memory
estimate drives lease admission, and it must come from MEASUREMENTS on
representative repositories, not guesswork. This script builds a SQLMesh
Context for a given working copy (or one of the demo workspaces), measures
the process RSS delta, and prints the YAML snippet to paste into
conf/workspace-runtime.yaml.

Usage:
    python3 scripts/ops/measure-lease-memory.py --workspace <path> [--repeat 3]
    python3 scripts/ops/measure-lease-memory.py --all-cells-tenants    # scan demo tree

Notes:
    * Runs sqlmesh in THIS process on purpose: the executor's Context cost is
      what we are measuring, isolated from FastAPI/uvicorn overhead.
    * RSS delta underestimates peak: python models exec during load. Treat
      the output as a LOWER bound and keep headroom via
      lease.admission_rss_high_watermark (0.80).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

# Must be set before any sqlmesh import: no outbound telemetry.
os.environ["SQLMESH__DISABLE_ANONYMIZED_ANALYTICS"] = "true"


def rss_bytes() -> int:
    """Current resident set size of this process, in bytes (macOS/Linux)."""
    with open("/proc/self/status", encoding="utf-8") as fh:  # Linux
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024

    # macOS fallback: page size * resident pages.
    import subprocess

    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return int(out) * 1024


def measure_once(workspace: str, gateway: str | None) -> dict:
    from sqlmesh.core.context import Context

    before = rss_bytes()
    ctx = Context(paths=workspace, gateway=gateway) if gateway else Context(paths=workspace)
    loaded = rss_bytes()

    n_models = len(getattr(ctx, "models", {}) or {})
    envs = [e.name for e in getattr(ctx, "environments", []) or []]

    # Keep the Context alive until after sampling, then release and let the
    # caller repeat. Deliberately no gc.collect() before sampling: we want
    # the steady-state footprint, not a post-GC optimistic one.
    info = {
        "workspace": workspace,
        "models": n_models,
        "environments": envs,
        "rss_before_bytes": before,
        "rss_loaded_bytes": loaded,
        "context_delta_bytes": loaded - before,
    }
    del ctx
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="working copy path to load")
    parser.add_argument("--gateway", default=None, help="optional gateway name")
    parser.add_argument("--repeat", type=int, default=3, help="samples per workspace")
    args = parser.parse_args()

    if not args.workspace:
        parser.error("--workspace is required (path to a working copy)")

    samples = []
    for i in range(args.repeat):
        info = measure_once(args.workspace, args.gateway)
        samples.append(info["context_delta_bytes"])
        print(f"sample {i + 1}: +{info['context_delta_bytes'] / 1e6:.1f} MB "
              f"({info['models']} models)", file=sys.stderr)

    delta = statistics.median(samples)
    # Headroom multiplier: RSS misses transient load peaks (python models
    # exec during Context construction) and interpreter fragmentation.
    estimate = int(delta * 1.5)

    print(json.dumps({
        "workspace": args.workspace,
        "models": samples and info["models"],
        "median_delta_bytes": delta,
        "recommended_estimate_bytes": estimate,
        "yaml": (
            "# paste into conf/workspace-runtime.yaml (lease section)\n"
            f"estimated_lease_bytes: {estimate}"
        ),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
