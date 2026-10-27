# Kubernetes deployment

Kustomize, under `deploy/k8s/`:

```
deploy/k8s/
├── base/        the full platform; resources carry explicit namespaces
├── local/       in-cluster postgres/redis for LOCAL clusters only
└── overlays/
    ├── cell/    a complete Cell (datacode + datacode-tasks namespaces)
    ├── global/  the global tier (identity only, datacode-global namespace)
    └── kind/    the cell shape at 1 replica, NodePort, for local verification
```

The overlays exist because the two tiers own different workloads: the base
carries everything, the cell overlay removes identity, the global overlay
removes everything except identity. Neither tier runs a workload it does not
own — that is what bounds blast radius.

## Prerequisites

- A cluster with an ingress controller (the Ingress targets nginx-ingress;
  adjust `deploy/k8s/base/ingress.yaml` for yours). The `kind` overlay uses
  NodePorts instead and needs none.
- PostgreSQL and Redis reachable from the cluster. The profiles name four
  endpoints — `postgres.datacode.svc`, `redis.datacode.svc`,
  `global-pg.datacode-global.svc`, `redis.datacode-global.svc` — so that a
  real install re-points them at managed instances by editing
  `conf/platform.k8s-cell.yaml` / `platform.k8s-global.yaml`. For a local
  cluster, `deploy/k8s/local/infra.yaml` provides exactly those four
  (`emptyDir` storage: verification, not persistence).
- The secrets below, once per cluster.

## 1. Secrets

Never committed; create with kubectl. Exact commands, per namespace, are in
`deploy/k8s/base/secrets.example.yaml`; the essentials:

```bash
kubectl create namespace datacode
kubectl create namespace datacode-tasks
kubectl create namespace datacode-global

kubectl -n datacode create secret generic datacode-db \
  --from-literal=password="$(openssl rand -hex 16)"
kubectl -n datacode create secret generic datacode-secrets \
  --from-literal=gateway-jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=identity-internal-token="$(openssl rand -hex 16)" \
  --from-literal=runtime-internal-token="$(openssl rand -hex 16)"

# The sandbox token has a MINTER (runtime, in datacode) and a VERIFIER
# (egress-proxy, in datacode-tasks). A Secret cannot cross namespaces, so the
# same value is created in both — a mismatch denies every sandbox's egress.
TASK_TOKEN="$(openssl rand -hex 32)"
kubectl -n datacode       create secret generic datacode-task-token \
  --from-literal=signing-secret="$TASK_TOKEN"
kubectl -n datacode-tasks create secret generic datacode-task-token \
  --from-literal=signing-secret="$TASK_TOKEN"
```

`GW_JWT_SECRET` and `DB_PASSWORD` must be the SAME values in the global tier's
copies: a token minted by global identity must verify at every cell's
gateway.

`runtime-internal-token` is read by both sides of the runtime's `/internal/*`
channel (gateway/control/agent send it, workspace-runtime verifies it) from
that one Secret key, so the two halves cannot drift apart.

## 2. Images

```bash
make images IMAGE_REGISTRY=<your-registry>     # tag: VERSION-g<sha>
make k8s-tag TAG=<the tag it printed>          # point the manifests at it
```

The overlays read image tags from `deploy/k8s/base/kustomization.yaml`'s
`images:` block. `make k8s-tag` rewrites that block, which is the only thing
that connects what you built to what the cluster runs — the committed default
is `latest`, and leaving it there means the cluster keeps whatever `latest`
happened to be when a node last pulled.

In a cluster, user code runs in `task-runner`, not in `workspace-runtime`: the
Kubernetes task backend submits every task to it, and
`dagster.code_location_mode: pod` loads a working copy's `defs/` package in it.
A package with no wheel therefore has to be baked in there too:

```bash
RUNTIME_EXTRAS=spark RUNNER_EXTRAS=spark make images IMAGE_REGISTRY=<your-registry>
```

Both, not one: the platform baseline that decides "already provided" is
computed by workspace-runtime off its own interpreter, while the import happens
in the sandbox. The `k8s` group (the task backend's cluster client) needs no
argument — the runtime image carries it always, since `task_backend:
kubernetes` is what this overlay sets and a missing client refuses to start.

## 3. First deploy

```bash
# Local clusters only: the in-cluster databases the profiles expect.
kubectl apply -f deploy/k8s/local/infra.yaml

# The global tier first — cells need it for login.
make k8s-migrate OVERLAY=global

# Then the cell.
make k8s-migrate OVERLAY=cell
```

`make k8s-migrate` applies the overlay, waits for whichever migration Jobs it
contains, and then restarts the workloads. Order matters: **migrations before
deployments serve**. The Deployments boot with `AUTO_MIGRATE=0` and (identity)
refuse to serve on a stale schema — schema changes are a first-class deploy
step, not a boot side effect. A replica that started first crash-loops until
the restart at the end of that target clears it.

For a rollout with no schema change, `make k8s-apply OVERLAY=cell` is enough.

### The Dagster orchestration plane (per cell)

`deploy/k8s/base/dagster-orchestrator.yaml` is part of every cell: one
`dagster-daemon` (with a `workspace-sync` sidecar), one `dagster-webserver`
(with its own sidecar), PostgreSQL run storage in the cell's `dagster`
database, and the `K8sRunLauncher` RBAC. It is a hard dependency, not an
add-on: the runtime StatefulSet runs `dagster.mode=remote` +
`code_location_mode=pod`, and `Settings.dagster_contract()` refuses to boot a
runtime whose task backend is `kubernetes` in any other shape — otherwise every
replica would run its own scheduler on a Pod-local SQLite instance.

- **The daemon is a singleton** (`replicas: 1`, `strategy: Recreate`). Two
  daemons fire every schedule twice; do not scale it.
- **`DATACODE_DATA_PLANE_URLS` lists runtime Pods by name**
  (`workspace-runtime-{0,1,2}.workspace-runtime…`). When you change the
  StatefulSet's replica count, edit both sidecars' lists to match; a missing
  replica's code locations are simply not shown.
- The `dagster` database must exist on the cell's Postgres (the local
  `infra.yaml` and `deploy/init-db/init.sql` create it).
- Images: `datacode/dagster-orchestrator` (webserver/daemon, includes
  `dagster-k8s`) and `datacode/dagster-workspace-sync`, built by
  `scripts/build-images.sh`.

### Environment lock backend

Both `CONTROL_SERVICE_ENV_LOCK_BACKEND` and
`WORKSPACE_RUNTIME_ENV_LOCK_BACKEND` are `redis`, and both services refuse to
start on `in_memory`. The lease makes a runtime the single writer of a working
copy, but the environment lock's other contender is control-service
(destroy / reconcile repair) — a different process no in-process lock can
exclude. `scripts/config-doctor.py` checks the manifests agree.

Redis is therefore a production dependency, not a cache: see
[redis-degradation-matrix.md](redis-degradation-matrix.md) for what each
capability does while it is unreachable, and which log events name the
capability that is currently degraded.

### Multi-replica routing check

Every env-control call from control-service is addressed by
`control-service/src/runtime/routing.py` (lease holder, every replica, or any
replica). To re-check that against two real runtime processes before a
release:

```bash
python3 scripts/verify/verify-control-lease-routing.py   # needs local Redis
```

### Interrupted workspace create / delete

Every workspace create and delete is recorded step by step in
`workspace_operations` (control-service, migration `0006`). A control Pod
killed mid-operation leaves its record `running`; the reconcile sweep's
resumer picks it up after `reconcile.operation_resume_grace_seconds`
(default 900s) and drives it to a terminal state:

| kind | policy |
| --- | --- |
| create | rolled back, unless only the bookkeeping was lost (`succeeded`); never touches a row older than the operation, and is `superseded` by any newer operation on the same workspace |
| delete | always finished forward (every step is idempotent) |

Like every other sweep repair, acting requires `reconcile.sweep_repair: true`;
with it off, stalled operations are reported as `stuck_operation` drift only.
After `operation_resume_max_attempts` (default 5) a record becomes `abandoned`
and needs a human: query `workspace_operations` for `status = 'abandoned'`.

```bash
python3 scripts/verify/verify-operation-resume.py   # SIGKILL mid-operation, then resume
```

## 4. Verifying locally (kind, colima, k3d)

```bash
kind create cluster --name datacode      # or: colima start --kubernetes
kubectl label nodes --all datacode.io/workload=user-code --overwrite

kubectl apply -f deploy/k8s/local/infra.yaml
make k8s-migrate OVERLAY=global
make k8s-migrate OVERLAY=kind

kubectl -n datacode get svc gateway frontend   # NodePorts printed
```

The kind overlay keeps the FULL security posture (egress plane, policies) at
1 replica with trimmed resources — a verification overlay that silently
weakened isolation would verify nothing.

Three things behave differently on a single-node local cluster:

- **Label the node, do NOT taint it.** The isolation profiles request
  `datacode.io/workload=user-code`, so the label is required or every task
  Pod stays Pending. The taint from `nodepool-user-code.yaml` is for a real
  multi-pool cluster: `workspace-runtime` declares no toleration, so tainting
  the only node makes the runtime itself unschedulable.
- **Images must reach the cluster's own runtime.** k3s (colima) and kind each
  keep a containerd separate from the Docker daemon that built the images.
  Every manifest uses `imagePullPolicy: IfNotPresent`, so importing is enough:

  ```bash
  # colima / k3s
  docker save datacode/gateway:latest | colima ssh -- sudo k3s ctr -n k8s.io images import -
  # kind
  kind load docker-image datacode/gateway:latest --name datacode
  ```

- **NetworkPolicy may not be enforced.** colima's k3s ships flannel, which
  implements no NetworkPolicy. The egress plane's objects are created and
  ignored — convenient (nothing locks you out) and misleading: do not use a
  flannel cluster to verify isolation.


## Configuration delivery

The `datacode-conf` ConfigMap is generated from the repository's `conf/`
directory (base + all profiles + service YAMLs + isolation profiles). Its
name carries a content hash, so **editing any profile triggers a rolling
update of everything mounting it** — config changes propagate without a
manual `rollout restart`.

The sandbox resolver is configured by Service NAME
(`services.egress_dns.host` in `conf/platform.k8s-cell.yaml`); the runtime
resolves it to the ClusterIP each time it builds a sandbox Pod, because
`dnsConfig` accepts IPs only and a ClusterIP is cluster-assigned. Nothing has
to be copied from the cluster after the first apply. `scripts/config-doctor.py`
rejects a literal IP there.

## Release and rollback

```bash
make k8s-diff OVERLAY=cell      # review before applying
make k8s-apply OVERLAY=cell     # rolling update
kubectl -n datacode rollout undo deployment/gateway   # per-workload rollback
```

Validate against the cluster's own schema before a real apply:

```bash
make k8s-render OVERLAY=cell | kubectl apply --dry-run=server -f -
```

`--dry-run=server`, not `client`. The client form validates almost nothing —
it accepts `replicas: "two"` and an invented `imagePullPolicy` even with
`--validate=strict`, because the schema lives on the API server. CI therefore
checks the RELATIONSHIPS between objects (every mount, ServiceAccount,
selector and Ingress backend resolves within its namespace — see
`workspace-runtime/tests/test_deploy_contracts.py`) and leaves field-level
validation to this step, which needs a cluster.

The workspace-runtime StatefulSet's per-replica RWO volumes mean a rolling
update keeps working copies in place — no re-clone, no WIP recovery prompt.
Pods re-adopt their leases by name.

## Scaling

| Workload | How to scale | What drives it |
|---|---|---|
| gateway | HPA (CPU 70%), 2–8 | request volume |
| workspace-runtime | edit StatefulSet replicas | lease capacity: replicas × max_per_instance, bounded by the per-pod memory limit (the admission controller reads the cgroup limit) |
| control / agent / identity | edit replicas | stateless beyond their databases; identity wants ≥2 behind its PDB |
| frontend | replicas or HPA | static serving |

The prometheus-adapter config (`deploy/prometheus-adapter-config.yaml`) adds
the `active_contexts_per_pod` custom metric if you prefer scaling the
runtimes on lease pressure rather than replica math.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Pod `CreateContainerConfigError` | a referenced Secret key is missing — `kubectl describe pod` names it; `secrets.example.yaml` lists every one the base needs, per namespace |
| Everything pending | check the PVCs first: the default 100Gi workcopies claim can exceed a small node's allocatable |
| Tasks pending forever, runtime healthy | the node carries no `datacode.io/workload=user-code` label, or (single-node clusters) it was tainted: `workspace-runtime` declares no toleration and becomes unschedulable itself |
| `could not start a sandbox` on every task | the runtime's RBAC — it creates Jobs/Pods/Secrets in `datacode-tasks` and reads results from Pod logs. `kubectl -n datacode-tasks auth can-i create jobs --as=system:serviceaccount:datacode:workspace-runtime` |
| identity refuses to serve: `database schema is behind` | the migrate Job did not run before the rollout — `make k8s-migrate OVERLAY=global` |
| 401 on every control→runtime call | the two sides read different Secret keys for `runtime-internal-token`; both must come from `datacode-secrets` |
| Gateway 502s to a service | the profile's URL for it — `make doctor PROFILE=k8s-cell SHOW=1` prints what the gateway resolves |
| Sandbox cannot resolve anything | runtime log `task_k8s_dns_resolver_unresolvable`: the runtime could not resolve `services.egress_dns.host` — is the `egress-dns` Service applied in `datacode-tasks`? |
