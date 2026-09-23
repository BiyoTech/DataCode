# Kubernetes deployment

Kustomize, under `deploy/k8s/`:

```
deploy/k8s/
├── base/        the full platform; resources carry explicit namespaces
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
  adjust `deploy/k8s/base/ingress.yaml` for yours)
- PostgreSQL and Redis reachable from the cluster — in-cluster Services
  named `postgres` / `redis` (per the profiles) or managed instances; point
  `conf/platform.k8s-cell.yaml` at yours
- The secrets below, once per cluster

## 1. Secrets

Never committed; create with kubectl. Exact commands are in
`deploy/k8s/base/secrets.example.yaml`; the essentials:

```bash
kubectl -n datacode create secret generic datacode-db \
  --from-literal=password="$(openssl rand -hex 16)"
kubectl -n datacode create secret generic datacode-secrets \
  --from-literal=gateway-jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=identity-internal-token="$(openssl rand -hex 16)" \
  --from-literal=runtime-internal-token="$(openssl rand -hex 16)"
kubectl -n datacode-tasks create secret generic datacode-task-token \
  --from-literal=signing-secret="$(openssl rand -hex 32)"
```

`GW_JWT_SECRET` and `DB_PASSWORD` must be the SAME values in the global tier's
copies: a token minted by global identity must verify at every cell's
gateway.

## 2. Images

```bash
make images IMAGE_REGISTRY=<your-registry>
kubectl -n datacode set image ...   # or edit the overlay's kustomization images
```

The overlays read image tags from `deploy/k8s/base/kustomization.yaml`'s
`images:` block; set your tag there (or per-overlay) — nothing deploys
`:latest` on purpose.

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
make global-up-steps:   # (conceptually) the global tier first — cells need it for login
kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/k8s/overlays/global | kubectl apply -f -
kubectl -n datacode-global wait --for=condition=complete job/identity-migrate --timeout=300s

make k8s-apply OVERLAY=cell
make k8s-migrate                     # runs + waits for the migration Jobs
```

Order matters: **migrations before deployments roll**. The Deployments boot
with `AUTO_MIGRATE=0` and (identity) refuse to serve on a stale schema —
schema changes are a first-class deploy step, not a boot side effect.

## 4. Verifying locally (kind)

```bash
kind create cluster --name datacode
make k8s-apply OVERLAY=kind
kubectl -n datacode get svc gateway frontend   # NodePorts printed
```

The kind overlay keeps the FULL security posture (egress plane, policies) at
1 replica with trimmed resources — a verification overlay that silently
weakened isolation would verify nothing.

## Configuration delivery

The `datacode-conf` ConfigMap is generated from the repository's `conf/`
directory (base + all profiles + service YAMLs + isolation profiles). Its
name carries a content hash, so **editing any profile triggers a rolling
update of everything mounting it** — config changes propagate without a
manual `rollout restart`.

The one value you must set by hand: `egress_dns.address` in
`conf/platform.k8s-cell.yaml` — the ClusterIP of the `egress-dns` Service,
read it from the cluster after the first apply
(`kubectl -n datacode-tasks get svc egress-dns`).

## Release and rollback

```bash
make k8s-diff OVERLAY=cell      # review before applying
make k8s-apply OVERLAY=cell     # rolling update
kubectl -n datacode rollout undo deployment/gateway   # per-workload rollback
```

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
| Pod `CreateContainerConfigError` | a referenced Secret key is missing — `kubectl describe pod` names it |
| Everything pending | check the PVCs first: the default 100Gi workcopies claim can exceed a small node's allocatable |
| identity refuses to serve: `database schema is behind` | the migrate Job did not run before the rollout — `make k8s-migrate` |
| Gateway 502s to a service | the profile's URL for it — `make doctor PROFILE=k8s-cell SHOW=1` prints what the gateway resolves |
| Sandbox cannot resolve anything | `egress_dns.address` in the k8s-cell profile must equal the egress-dns Service's ClusterIP |
