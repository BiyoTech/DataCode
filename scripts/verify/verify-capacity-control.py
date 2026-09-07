#!/usr/bin/env python3
"""P8 capacity orchestration acceptance — K8s driver + drain timing (design 06).

The executable form of the P8 acceptance items that need a REAL cluster:

  A. ``KubernetesReplicaDriver`` reads and patches the StatefulSet scale
     subresource against the live API server.
  B. Scale-down victim semantics: Kubernetes removes the HIGHEST ordinal, not
     necessarily the drained instance — the documented limitation that makes
     the SIGTERM drain a required backstop rather than an optimisation.
  C. The full drain timing loop: a fake runtime speaking the REAL wire
     contract (heartbeat JSON in Redis, ``POST /internal/leases/drain`` with
     the internal token) is drained by the production ``LeaseController``;
     the replica count shrinks only AFTER the heartbeat disappears.

What this run does NOT prove (kept out of the record on purpose): a real
workspace-runtime image draining real leases inside the cluster (pod-to-pod
HTTP, final snapshots, SIGTERM handling). That remains a production-cluster
exercise; everything testable here is tested through the production code
path — the controller, the driver and the shared wire format are the real
implementations, only the "runtime" is a stub.

Usage:
    python3 scripts/verify/verify-capacity-control.py [--namespace datacode-p8-verify] [--keep]

Requires:
  * a working kube context (e.g. ``kind create cluster --name datacode-p8``)
  * Redis reachable at the platform-configured URL (the dev compose one,
    localhost:6379, is what the platform config resolves to)
  * the control-service virtualenv — re-invoked automatically when missing

Exit code 1 if any check FAILs.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONTROL_VENV_PYTHON = os.path.join(REPO_ROOT, "control-service", ".venv", "bin", "python")

# The production drain call sends this token; the fake runtime enforces it, so
# the run proves the header actually travels (unset would be rejected).
INTERNAL_TOKEN = "p8-verify-internal-token"

NAMESPACE = "datacode-p8-verify"
STATEFULSET = "p8-fake-runtime"
FAKE_RUNTIME_ID = "rt-fake-1"
FAKE_RUNTIME_PORT = 8199
LEASE_KEY = "t1/ws-fake/u1/dev"

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
RESULTS: list[tuple[str, str]] = []


def report(check_id: str, ok: bool, detail: str) -> None:
    status = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    RESULTS.append((check_id, "PASS" if ok else "FAIL"))
    print(f"  [{status}] {check_id}: {detail}")


def skip(check_id: str, detail: str) -> None:
    RESULTS.append((check_id, "SKIP"))
    print(f"  [{YELLOW}SKIP{RESET}] {check_id}: {detail}")


# ── bootstrap: re-exec inside the control-service venv when needed ─────────


def _ensure_venv() -> None:
    """Run this script with the control-service interpreter when the current
    one lacks the kubernetes client / the shared package."""
    if importlib.util.find_spec("kubernetes") is None or importlib.util.find_spec(
        "datacode_common"
    ) is None:
        if not os.path.exists(CONTROL_VENV_PYTHON):
            print(f"fatal: {CONTROL_VENV_PYTHON} not found", file=sys.stderr)
            sys.exit(2)
        # Environment first, so the child (and its pydantic settings) sees it.
        os.environ["CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN"] = INTERNAL_TOKEN
        os.execv(
            CONTROL_VENV_PYTHON,
            [CONTROL_VENV_PYTHON, os.path.abspath(__file__)] + sys.argv[1:],
        )


_ensure_venv()
# Belt and braces: also set it here for the no-re-exec path.
os.environ.setdefault("CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN", INTERNAL_TOKEN)

sys.path.insert(0, os.path.join(REPO_ROOT, "control-service"))
sys.path.insert(0, os.path.join(REPO_ROOT, "shared", "python"))


# ── kubectl helpers ─────────────────────────────────────────────────────────


def kubectl(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def sts_ready_replicas() -> int:
    out = kubectl(
        "get", "sts", STATEFULSET, "-o", "jsonpath={.status.readyReplicas}"
    )
    return int(out) if out else 0


def sts_spec_replicas() -> int:
    return int(kubectl("get", "sts", STATEFULSET, "-o", "jsonpath={.spec.replicas}"))


def pod_names() -> set[str]:
    out = kubectl("get", "pods", "-l", f"app={STATEFULSET}", "-o", "name")
    return {line.split("/")[-1] for line in out.splitlines() if line.strip()}


async def wait_for(predicate, timeout: float, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate() if asyncio.iscoroutinefunction(predicate) else predicate():
            return True
        await asyncio.sleep(1.0)
    print(f"        (timeout waiting for: {what})")
    return False


STS_MANIFEST = f"""apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {STATEFULSET}
spec:
  serviceName: {STATEFULSET}
  replicas: 3
  selector:
    matchLabels:
      app: {STATEFULSET}
  template:
    metadata:
      labels:
        app: {STATEFULSET}
    spec:
      terminationGracePeriodSeconds: 5
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.10
"""


# ── the fake runtime (same wire contract as the real one) ───────────────────


class FakeRuntime:
    """A stand-in workspace-runtime: heartbeat in Redis + drain endpoint.

    Its drain behaviour mirrors ``begin_controller_drain``: publish a
    draining=true heartbeat SYNCHRONOUSLY (so the answer implies "placement
    already avoids me"), return, and finish (delete the heartbeat) a moment
    later — the controller polls the heartbeat, never the HTTP call.
    """

    def __init__(self, redis_client, finish_delay: float = 2.0) -> None:
        self._client = redis_client
        self._finish_delay = finish_delay
        self._server = None
        self.drain_requests = 0
        self.token_failures = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", FAKE_RUNTIME_PORT
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        if method == "POST" and path == "/internal/leases/drain":
            self.drain_requests += 1
            if headers.get("x-internal-token") != INTERNAL_TOKEN:
                self.token_failures += 1
                await self._respond(writer, 401, b'{"error": "unauthorized"}')
                return
            # Synchronous announce (draining=true visible to placement NOW),
            # then finish in the background — the real runtime's split.
            await self.publish_heartbeat(draining=True)
            asyncio.get_running_loop().call_later(
                self._finish_delay,
                lambda: asyncio.create_task(self._finish()),
            )
            await self._respond(writer, 200, b'{"draining": true}')
        else:
            await self._respond(writer, 404, b"{}")

    async def publish_heartbeat(self, *, draining: bool, ttl: int = 60) -> None:
        from datetime import datetime, timezone

        from datacode_common.lease import (
            RuntimeHeartbeat,
            encode_runtime_heartbeat,
            runtime_index_redis_key,
            runtime_redis_key,
        )

        heartbeat = RuntimeHeartbeat(
            runtime_id=FAKE_RUNTIME_ID,
            addr=f"127.0.0.1:{FAKE_RUNTIME_PORT}",
            lease_count=1,
            max_leases=10,
            memory_used_ratio=0.1,
            reported_at=datetime.now(timezone.utc),
            draining=draining,
            leases=(LEASE_KEY,),
        )
        await self._client.set(
            runtime_redis_key(FAKE_RUNTIME_ID), encode_runtime_heartbeat(heartbeat), ex=ttl
        )
        await self._client.sadd(runtime_index_redis_key(), FAKE_RUNTIME_ID)

    async def _finish(self) -> None:
        """The background half: release everything, heartbeat disappears."""
        from datacode_common.lease import (
            runtime_index_redis_key,
            runtime_redis_key,
        )

        await self._client.delete(runtime_redis_key(FAKE_RUNTIME_ID))
        await self._client.srem(runtime_index_redis_key(), FAKE_RUNTIME_ID)

    async def cleanup(self) -> None:
        """Remove this fake's Redis footprint (heartbeat + index member)."""
        await self._finish()

    @staticmethod
    async def _respond(writer, status: int, body: bytes) -> None:
        reason = {200: "OK", 401: "Unauthorized", 404: "Not Found"}.get(status, "?")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()
        writer.close()


# ── checks ──────────────────────────────────────────────────────────────────


async def check_a_driver_mechanics(driver) -> bool:
    """A: the driver reads and writes the real scale subresource."""
    ok_read = await driver.current_replicas() == 3

    await driver.set_replicas(4)
    grew = await wait_for(
        lambda: sts_ready_replicas() == 4, timeout=90, what="4 ready replicas"
    )

    await driver.set_replicas(3)
    shrank = await wait_for(
        lambda: sts_ready_replicas() == 3, timeout=90, what="3 ready replicas"
    )

    ok = ok_read and grew and shrank
    report(
        "A",
        ok,
        f"scale subresource read={ok_read} 3→4={grew} 4→3={shrank} "
        "(read + patch through the live API server)",
    )
    return ok


async def check_b_victim_semantics(driver) -> bool:
    """B: K8s removes the highest ordinal on scale-down.

    This is the limitation documented in the controller: the drained victim
    (rt-fake-1, ordinal 0 in this run) survives; the SIGTERM drain is what
    protects whichever pod Kubernetes actually removes.
    """
    await driver.set_replicas(2)
    removed_lowest = await wait_for(
        lambda: f"{STATEFULSET}-2" not in pod_names(), timeout=90, what="ordinal-2 pod gone"
    )
    survivors = {f"{STATEFULSET}-0", f"{STATEFULSET}-1"} <= pod_names()

    ok = removed_lowest and survivors
    report(
        "B",
        ok,
        "scale-down removed the HIGHEST ordinal "
        f"(pods now: {sorted(pod_names())}) — victim choice is Kubernetes', "
        "SIGTERM drain is the backstop (documented limitation confirmed)",
    )
    # restore 3 replicas for check C
    await driver.set_replicas(3)
    await wait_for(lambda: sts_ready_replicas() == 3, timeout=90, what="3 ready replicas")
    return ok


async def check_c_drain_timing(controller, driver, fake: FakeRuntime) -> bool:
    """C: drain_requested → heartbeat gone → scale_down, via the production
    controller against the real cluster and a fake runtime on the real wire
    contract."""
    # Precondition: low utilization (1 lease / 10 capacity), 3 replicas.
    await fake.publish_heartbeat(draining=False)

    outcome1 = await controller.run_once()
    requested = (
        outcome1.action == "drain_requested"
        and fake.drain_requests == 1
        and fake.token_failures == 0
        and sts_spec_replicas() == 3
    )

    # The fake finishes ~2s after the drain request; give it a moment.
    await asyncio.sleep(3.5)
    outcome2 = await controller.run_once()
    scaled = await wait_for(
        lambda: sts_spec_replicas() == 2, timeout=60, what="sts scaled to 2"
    )
    ordinal2_gone = await wait_for(
        lambda: f"{STATEFULSET}-2" not in pod_names(), timeout=90, what="ordinal-2 pod gone"
    )

    ok = requested and scaled and ordinal2_gone and outcome2.action == "scale_down"
    report(
        "C",
        ok,
        f"tick1={outcome1.action} (replicas still {outcome1.desired_replicas}, "
        f"drain endpoint hit={fake.drain_requests}, token ok={fake.token_failures == 0}) → "
        f"tick2={outcome2.action} ({outcome2.reason}) → live replicas={sts_spec_replicas()}",
    )
    return ok


# ── main ────────────────────────────────────────────────────────────────────


async def main_async(args) -> int:
    print("P8 capacity orchestration — real-cluster acceptance (kind)")
    print(f"  namespace: {NAMESPACE}  statefulset: {STATEFULSET}")
    print()

    # Cluster reachable at all?
    ctx = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True
    )
    if ctx.returncode != 0 or not ctx.stdout.strip():
        print("fatal: no kube context — create one first (kind create cluster ...)")
        return 2
    print(f"  kube context: {ctx.stdout.strip()}")

    # Redis: the platform config resolves the dev compose instance.
    from datacode_common.redis import get_redis

    client = get_redis()
    if client is None or not await client.ping():
        print("fatal: Redis unreachable at the platform-configured URL")
        return 2
    print("  redis: reachable")

    # Fresh namespace + StatefulSet. The previous run's deletion is async
    # (--wait=false), so wait for the namespace to be GONE before recreating.
    subprocess.run(
        ["kubectl", "delete", "ns", NAMESPACE, "--wait=false"],
        capture_output=True,
        text=True,
    )
    ns_deadline = time.monotonic() + 60
    while time.monotonic() < ns_deadline:
        probe = subprocess.run(
            ["kubectl", "get", "ns", NAMESPACE, "--no-headers"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            break  # not found: fully deleted
        time.sleep(1.0)
    subprocess.run(
        ["kubectl", "create", "ns", NAMESPACE], capture_output=True, text=True, check=True
    )
    subprocess.run(
        ["kubectl", "apply", "-n", NAMESPACE, "-f", "-"],
        input=STS_MANIFEST,
        capture_output=True,
        text=True,
        check=True,
    )
    if not await wait_for(
        lambda: sts_ready_replicas() == 3, timeout=180, what="initial 3 ready replicas"
    ):
        print("fatal: StatefulSet never became ready (image pull?)")
        return 2

    from src.capacity.lease_controller import (
        KubernetesReplicaDriver,
        LeaseController,
    )

    driver = KubernetesReplicaDriver(namespace=NAMESPACE, statefulset=STATEFULSET)
    controller = LeaseController(
        client=client,
        driver=driver,
        usage_reporter=None,
        quota_publisher=None,
        # No drain_requester override: the PRODUCTION httpx call (with the
        # internal token header) must be what reaches the fake runtime.
    )

    fake = FakeRuntime(client)
    await fake.start()

    try:
        await check_a_driver_mechanics(driver)
        await check_b_victim_semantics(driver)
        await check_c_drain_timing(controller, driver, fake)
    finally:
        await fake.stop()
        await fake.cleanup()
        if not args.keep:
            subprocess.run(
                ["kubectl", "delete", "ns", NAMESPACE, "--wait=false"],
                capture_output=True,
                text=True,
            )

    failures = [cid for cid, verdict in RESULTS if verdict == "FAIL"]
    print()
    print(f"result: {len(RESULTS) - len(failures)}/{len(RESULTS)} passed")
    return 1 if failures else 0


def main() -> int:
    global NAMESPACE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--namespace", default=NAMESPACE, help="namespace for the throwaway StatefulSet"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the namespace after the run"
    )
    args = parser.parse_args()
    NAMESPACE = args.namespace
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
