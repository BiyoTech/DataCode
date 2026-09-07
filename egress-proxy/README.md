# egress-proxy

The ONLY outbound path open to a task sandbox (design 04 §3.4, P6b) — an
authorization boundary, not a content filter. It speaks HTTP CONNECT and
nothing else: the tunnel is TLS, so it cannot and should not inspect payloads.

## The five-step connection, which is the whole security argument

1. **Identify** — no valid scoped task token, no connection. The token is
   HMAC-signed with its scope INSIDE the signature and expires with the task
   deadline, so there is no revocation list and no way to borrow another
   tenant's rules.
2. **Fetch the grant** — what THIS task may reach, published to Redis by the
   runtime at submit time (`eg:grant:{task_id}`, TTL = task timeout). Missing
   grant or unreachable store both DENY: the safe answer to "I cannot tell"
   is "nothing".
3. **Authorize** — the shared policy (`datacode_common.egress`) resolves the
   destination and refuses every internal address (baseline denied CIDRs are
   append-only, cover IPv6 / `::ffff:` mappings / cloud metadata).
4. **Connect to the RESOLVED address** — never re-resolve the hostname, or
   step 3's DNS-rebinding TOCTOU gap reopens.
5. **Tunnel and account** — bytes are counted and audited on close.

## Where it runs (and where it deliberately does not)

Only in Kubernetes cell deployments, alongside the egress plane
(`deploy/k8s/base/egress-plane.yaml`: CoreDNS NXDOMAIN for cluster-internal
names, default-deny NetworkPolicy). Without a NetworkPolicy making the proxy
a sandbox's only option, a configured proxy mediates SOME traffic while the
rest flows freely — control as an illusion. `scripts/config-doctor.py` rejects
`services.egress_proxy` in every non-k8s profile for exactly that reason, and
dev does not start it.

## Layout

```
src/
├── server.py    one coroutine per connection: the five steps above
├── connect.py   CONNECT handshake + tunnel plumbing
├── audit.py     byte accounting / egress audit log
├── config.py    env + platform.yaml loading
└── main.py      entrypoint
```

## Run

```
./.venv/bin/python -m pytest -q        # unit tests; no cluster needed
```

Production deployment is `make k8s-apply OVERLAY=cell` — see
`doc/ops/deploy-k8s.md` for the mandatory ordering (proxy first, NetworkPolicy
only after the audit log proves the allow-lists).
