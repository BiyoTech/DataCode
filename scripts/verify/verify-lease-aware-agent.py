"""Live smoke test for lease-aware routing from agent-service.

Uses REAL processes end to end: real Redis, two real HTTP servers standing in for
runtime replicas, the production LeaseRouter and the production RuntimeClient.
The unit tests inject fakes at the seams; this one proves the seams are wired to
something that works, which is the part a fake cannot show.

What it asserts, and why each would be missed by a unit test:

  1. a real lease record written by the holder's format resolves to that pod, so
     the Redis key layout and the record shape agree across writer and reader;
  2. with only the Service address configured (no lease), requests still work —
     the single-replica deployment must not regress;
  3. a lease pointing at a NON-holder that answers 409 LEASE_NOT_HELD is
     repaired: the client re-resolves and reaches the new holder. This is the
     production failure mode the whole change exists to fix.

Run:  python scripts/verify/verify-lease-aware-agent.py
Needs a reachable Redis (the dev stack's is fine). Exits 0 on success, 1 on any
failed assertion, 2 on a missing prerequisite.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_VENV_PYTHON = os.path.join(REPO_ROOT, "agent-service", ".venv", "bin", "python")


def _ensure_venv() -> None:
    """Re-exec inside agent-service's venv, which has httpx + redis + the src tree."""
    if os.path.abspath(sys.executable) != os.path.abspath(AGENT_VENV_PYTHON):
        if not os.path.exists(AGENT_VENV_PYTHON):
            print(f"fatal: {AGENT_VENV_PYTHON} not found", file=sys.stderr)
            sys.exit(2)
        os.environ.setdefault("DATACODE_ENV", "dev")
        os.execv(
            AGENT_VENV_PYTHON,
            [AGENT_VENV_PYTHON, os.path.abspath(__file__)] + sys.argv[1:],
        )


_ensure_venv()

sys.path.insert(0, os.path.join(REPO_ROOT, "agent-service"))
sys.path.insert(0, os.path.join(REPO_ROOT, "shared", "python"))

from datacode_common.lease import lease_redis_key  # noqa: E402
from datacode_common.lease_routing import LeasePlacement, LeaseRouter  # noqa: E402
from datacode_common.naming import lease_key_from_working_copy_path  # noqa: E402
from datacode_common.redis.client import get_redis, redis_available  # noqa: E402

from src.tools.runtime_client import RuntimeClient  # noqa: E402

WORKSPACE = "/tmp/datacode-verify/t1/ws-t1-shop/u1/envs/dev"

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))


class FakeReplica:
    """One runtime replica: reports its own identity, or refuses the lease.

    ``on_refusal`` runs synchronously while answering a 409, which is what makes
    the hand-over test deterministic AND causally honest: a replica answers
    LEASE_NOT_HELD *because* it no longer owns the copy, so releasing the lease
    is part of the same event, not a race running beside it.
    """

    def __init__(
        self,
        name: str,
        *,
        holds_lease: bool = True,
        on_refusal=None,
    ) -> None:
        self.name = name
        self.holds_lease = holds_lease
        self.on_refusal = on_refusal
        self.requests = 0
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def addr(self) -> str:
        return f"127.0.0.1:{self.port}"

    async def _handle(self, reader, writer) -> None:
        try:
            # Read the request head; the body is irrelevant to the assertions and
            # a fixed-size read keeps this from blocking on keep-alive.
            await reader.read(4096)
            self.requests += 1
            if self.holds_lease:
                body = json.dumps({"replica": self.name}).encode()
                status = b"200 OK"
            else:
                if self.on_refusal is not None:
                    await self.on_refusal()
                body = json.dumps(
                    {
                        "error_code": "LEASE_NOT_HELD",
                        "message": "this runtime does not hold the lease",
                    }
                ).encode()
                status = b"409 Conflict"
            writer.write(
                b"HTTP/1.1 " + status + b"\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()


async def main() -> int:
    if not await redis_available():
        print("fatal: Redis is not reachable; start the dev stack first", file=sys.stderr)
        return 2
    redis = get_redis()

    Path(WORKSPACE).mkdir(parents=True, exist_ok=True)
    key = lease_key_from_working_copy_path(WORKSPACE)
    redis_key = lease_redis_key(key)
    print(f"lease key: {key}\nredis key: {redis_key}\n")

    holder = FakeReplica("holder")
    # The stale replica releases the lease as it refuses: that is the real causal
    # order (it refuses BECAUSE it no longer owns the copy), and it removes the
    # race a side-channel poller would introduce.
    other = FakeReplica(
        "stale",
        holds_lease=False,
        on_refusal=lambda: redis.delete(lease_redis_key(key)),
    )
    await holder.start()
    await other.start()

    async def write_lease(addr: str) -> None:
        """Write a lease record in exactly the holder's on-wire format."""
        await redis.set(
            redis_key,
            json.dumps(
                {
                    "token": "verify-token",
                    "runtime_id": "verify-runtime",
                    "runtime_addr": addr,
                    "acquired_at": time.time(),
                }
            ),
            ex=60,
        )

    try:
        # ── 1. a real lease record routes to that pod ──────────────────
        await write_lease(holder.addr)
        client = RuntimeClient(
            base_url=f"http://{other.addr}",  # the "Service": deliberately wrong
            router=LeaseRouter(fallback_url=f"http://{other.addr}"),
        )
        body = await client.get("/api/v1/models", workspace_path=WORKSPACE)
        check(
            "a lease record routes to the holding pod, not the Service address",
            body.get("replica") == "holder" and other.requests == 0,
            f"body={body} service_hits={other.requests}",
        )

        # ── 2. no lease + no placement: single-replica behaviour holds ──
        await redis.delete(redis_key)
        holder.requests = 0
        single = RuntimeClient(
            base_url=f"http://{holder.addr}",
            router=LeaseRouter(fallback_url=f"http://{holder.addr}"),
        )
        body = await single.get("/api/v1/models", workspace_path=WORKSPACE)
        check(
            "without a lease the configured address still serves (single replica)",
            body.get("replica") == "holder",
            f"body={body}",
        )

        # ── 3. a stale lease is repaired, not relayed ──────────────────
        # The record points at a replica that answers LEASE_NOT_HELD and releases
        # the lease as it does so. The client must invalidate, re-resolve and
        # reach the real holder — which is exactly what a direct dial to the
        # headless Service could never do.
        await write_lease(other.addr)
        other.requests = 0
        holder.requests = 0
        repaired = RuntimeClient(
            base_url=f"http://{holder.addr}",
            router=LeaseRouter(fallback_url=f"http://{holder.addr}"),
        )
        body = await repaired.get("/api/v1/models", workspace_path=WORKSPACE)
        check(
            "a stale route is repaired after LEASE_NOT_HELD",
            body.get("replica") == "holder" and other.requests == 1,
            f"body={body} refusals={other.requests}",
        )

        # ── 4. placement is only attempted with a token ─────────────────
        check(
            "placement stays disabled without the internal token",
            not LeasePlacement("http://localhost:8002", "").enabled
            and LeasePlacement("http://localhost:8002", "tok").enabled,
        )
    finally:
        await redis.delete(redis_key)
        await holder.stop()
        await other.stop()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
