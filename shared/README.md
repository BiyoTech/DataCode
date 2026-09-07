# shared

The cross-language CONTRACT layer: everything Go (gateway, identity-service)
and Python (control/agent/runtime/task-runner/egress-proxy) must agree on,
implemented exactly once. It exists because two independent implementations of
"which replica owns this working copy" or "how do I decode a lease record" is
how a second writer eventually appears.

## The admission rule

A module moves here only when it has **two or more consumers across services**
(and ideally across languages). Single-service helpers stay in their service —
this library must never become a junk drawer, or every service gains a
dependency on every other's details.

## What lives here

```
python/datacode_common/
├── naming.py            environment/branch/deployment algebra, lease keys
├── workspace_paths.py   the {base}/{tenant}/{ws}/{user}/envs/{env} layout
├── workspace_layout.py  what a working copy holds BESIDES user content:
│                        platform state (.datacode/), the sandbox HOME, and
│                        which paths the explorer, the search index and the
│                        generated .gitignore must hide
├── lease.py             lease records, heartbeats, CAS Lua scripts, quota
│                        snapshots, address normalization, stable-hash pick
├── lease_routing.py     resolve a working copy → the replica holding it
│                        (the gateway's Go router mirrors this contract)
├── rbac/                the permission catalog: which permission codes exist
│                        (_catalog_data.py is GENERATED, see below)
├── task_protocol.py     TaskSpec / TaskResult / TaskProgress (sandbox plane)
├── warm_protocol.py     warm-runner frame protocol
├── executor_protocol.py L3-proc executor protocol
├── framing.py           length-prefixed JSON frames (one implementation)
├── egress.py            the egress policy: baseline denied CIDRs, TOCTOU-safe
├── egress_grant.py      per-task allow-list in Redis
├── security/            internal-token helpers, scoped task tokens
├── isolation.py         isolation-profile resolution (strongest-wins)
├── config/              platform.yaml + profile deep-merge loading
├── database/            engine/URL builders shared by the Python services
├── redis/               client wiring, run registry
├── identity/            IdentityClient for non-HTTP entry points
└── errors.py            the frozen platform error codes

go/lease/                the Go mirror: keys, records, CAS scripts,
                        normalization, stable hash — pinned to Python by tests
go/rbac/                 the permission catalog for gateway + identity-service
                        (catalog_gen.go is GENERATED, see below)
lease-vectors.json       cross-language test vectors: BOTH sides assert
                        against this file, never against each other's source
rbac-catalog.json        the permission catalog DECLARATION: permission codes
                        and built-in role templates
```

## The vectors file is the point

`lease-vectors.json` pins key derivation, address normalization and the
stable-hash index. The Go and Python suites each load it and assert their own
implementation — a divergence fails BOTH test suites at once instead of
surfacing in production as a Redis miss and a twice-mounted working copy.

## The permission catalog: one declaration, two generated tables

`rbac-catalog.json` declares every permission code and the built-in role
templates. `scripts/gen-rbac-catalog.py` generates `go/rbac/catalog_gen.go` and
`python/datacode_common/rbac/_catalog_data.py` from it; both are committed, and
`--check` fails CI when either has drifted.

Generated rather than read at runtime, for two concrete reasons:

* `//go:embed` cannot reference a parent directory, so the Go package next to the
  JSON could not embed it anyway;
* `datacode_common` ships as a wheel containing the package directory only, so a
  repository-relative JSON does not exist in a deployed service.

Both languages still assert their table against the JSON (`go/rbac/catalog_test.go`,
`python/tests/test_rbac_catalog.py`), so the file stays the single source in the
same way `lease-vectors.json` does. The failure this prevents is specific: a
permission code that exists on one side only produces a route that answers 403 to
everyone, including admin, with no way to grant it — the catalog is also what
identity-service synchronizes into the `permissions` table, which is a projection
and never a source of truth.

## Run

```
cd python && python -m pytest -q          # via any service venv
cd go && go test ./...
```
