# Redis degradation matrix

Redis is the platform's coordination substrate: leases, environment locks,
routing, quotas and sandbox egress grants all read it. It is **not** a cache —
losing it changes behaviour, and the change is different per capability.

This page states, per capability, what happens when Redis is unreachable, and
why that direction was chosen. Every row was read off the code (file and
symbol named), not off an intention: the point of the table is to be checkable.

The rule behind the table is one sentence: **anything protecting correctness
or a security boundary fails CLOSED; anything protecting capacity fails OPEN.**
A capacity control that locks every tenant out during an outage converts a
degraded platform into a dead one, while a single-writer guarantee that opens
up during an outage silently corrupts a working copy.

## The matrix

| Capability | Owner | Redis unreachable → | Direction | Visible as |
| --- | --- | --- | --- | --- |
| Working-copy lease (claim) | `workspace-runtime/src/lease/holder.py` | `LeaseUnavailable` — this replica refuses to serve the copy | **closed** | 409 `LEASE_NOT_HELD`; the caller re-routes |
| Lease placement | `workspace-runtime/src/lease/server.py` | placement cannot read the fleet → acquire fails | **closed** | 503 from `/internal/leases/acquire` |
| Lease-aware routing (gateway, agent, control) | `shared/.../lease_routing.py` | stable hash over the live replica set, else the Service address | **degraded, deterministic** | log `lease_routing_degraded mode=stable_hash` |
| Environment lifecycle lock | `shared/.../locks/env_locks.py` | in-process registry for that call only | **open, loudly** | log `environment_lock_in_process_backend` at ERROR (cross-service participants) |
| Tenant quotas (workspaces, environments) | `control-service/src/quota/service.py` | enforcement off (`limits_for` returns platform defaults, usage reads skipped) | **open** | log `quota_limits_read_failed` / `quota_usage_read_failed` |
| Lease quotas (per tenant) | `workspace-runtime/src/lease/quotas.py` | enforcement off for that acquire | **open** | log `lease_quota_read_failed` |
| Quota publication (control → runtime) | `control-service/src/quota/publisher.py` | snapshots expire after `quota.publish_ttl_seconds`; runtime then reads nothing | **open after TTL** | the row above |
| Sandbox egress grant | `egress-proxy/src/server.py` | every CONNECT is refused | **closed** | 503 `unavailable`, audit `grant store unreachable` |
| Capacity controller (replica scaling) | `control-service/src/capacity/lease_controller.py` | the tick makes no decision at all (no scaling, no usage accounting, no quota publication) | **inert** | log `lease_controller_tick_skipped`; next tick retries |
| Reconcile sweep: lease/sandbox drift | `control-service/src/environments/` | those two parties report no drift | **inert** | sweep continues for the other parties |

## Why each direction

**Leases fail closed.** The lease *is* the single-writer guarantee for a
working copy. A replica that cannot confirm its claim and serves anyway is
exactly the two-writers-on-one-worktree outcome the lease exists to prevent.
Refusing is recoverable (the caller retries against the real holder); writing
is not.

**Routing degrades deterministically, and that is not a contradiction.**
Routing only decides *which replica to ask first*; the replica's own lease gate
still fail-closes. So the degraded path cannot produce a second writer — the
worst case is an extra hop. Deterministic (stable hash over the live replica
set, the FNV-1a contract in `shared/lease-vectors.json`) rather than random,
so during an outage every router sends one key to the SAME replica. Three
semantics must survive degradation and are asserted in tests:

* a clean miss may trigger placement; an outage may not (placement needs the
  same Redis);
* a malformed record counts as an outage, never as a miss — treating it as a
  miss could mount a copy twice;
* a 429 capacity refusal propagates and never degrades, or the stable hash
  would route around the decision just made.

**The environment lock fails open, but only within one process.** It
serializes an environment's lifecycle operations (control's destroy/repair vs
the runtime's apply). With Redis down it still serializes callers inside one
process, which is strictly better than nothing; across services it does not,
which is why cross-service participants log that at ERROR rather than INFO
(`EnvironmentLocks(cross_service=True)`). Those services also refuse to start
on a configured `in_memory` backend — a permanent version of this degradation
is a misconfiguration, not an outage.

**Quotas fail open because they are capacity, not security.** A tenant over
their workspace ceiling costs money; a tenant unable to open any workspace
during a Redis blip costs the platform its availability claim. Authorization
is a separate mechanism (identity-service) and fails closed independently.

**Egress grants fail closed because they are a security boundary.** The grant
IS the allow-list; an unreadable allow-list means "we do not know what this
task may reach". Defaulting to "allow" would let a sandbox talk to anything
during an outage, which is indistinguishable from exfiltration.

**The capacity controller goes inert, and "inert" had to be made true.** Every
input it has — utilization, live instances, whether a drain finished — is
derived from the heartbeat stream. An unreadable stream once yielded an empty
list, which is indistinguishable from a healthy-but-idle fleet: utilization
read as 0.0 (scale down), and a draining victim read as "heartbeat gone, drain
complete" (scale down again). A Redis blip could therefore shrink a fleet whose
replicas were all serving leases. The read now returns "unknown" and the tick
is skipped whole; usage accounting and quota publication are skipped with it,
because both would otherwise write zeros derived from nothing.

## Operating implications

* **Redis must be HA in production.** The fail-closed half (leases, egress)
  makes a Redis outage a partial platform outage by design. Run a managed
  instance or Sentinel/Cluster; a single Pod with no replica is a single point
  of failure for working-copy access.
* **`redis.enabled: false` is a single-node choice, not an outage.** It is
  honest only where there is exactly one runtime replica and one control
  replica. The K8s profiles never select it, and the env-lock startup check
  rejects the configuration that would make it dangerous.
* **Alert on the degradation events, not on Redis alone.**
  `lease_routing_degraded`, `environment_lock_in_process_backend` (ERROR),
  `egress_grant_unavailable` and `quota_*_read_failed` each name a capability
  that is currently not doing its job — they tell an operator which half of the
  platform is affected, which "Redis is down" does not.
* **Recovery needs no intervention.** Every path above re-reads Redis on the
  next call; nothing latches. Leases re-establish on the next acquire, routing
  caches expire after 10s, quota snapshots are republished each controller
  tick.

## Related

* `doc/ops/configuration.md` — the `redis.*` keys and their env overrides.
* `doc/ops/deploy-k8s.md` — env-lock backend contract, orchestration plane.
* `scripts/verify/verify-lease-routing.py`, `verify-control-lease-routing.py` —
  the lease and routing behaviour above, against real processes.
