# DataCode Operations — 00 Overview

How this platform is configured, started locally, and deployed. Start here;
each topic has its own page.

| If you want to… | Read |
|---|---|
| Understand how configuration resolves (profiles, env vars, secrets) | [configuration.md](configuration.md) |
| Run everything on your laptop for development | [deploy-host.md](deploy-host.md) |
| Run everything in containers (one machine) | [deploy-docker.md](deploy-docker.md) |
| Deploy to production Kubernetes | [deploy-k8s.md](deploy-k8s.md) |
| Know what breaks (and what does not) when Redis is down | [redis-degradation-matrix.md](redis-degradation-matrix.md) |

## The three deployment shapes

| | host dev | Docker (single machine) | Kubernetes (production) |
|---|---|---|---|
| Entry point | `make dev` | `make up` | `make k8s-apply OVERLAY=cell` |
| Config mechanism | repository `conf/`, profile `dev` | mounted `conf/`, profile `docker` | ConfigMap from `conf/`, profile `k8s-cell` |
| Storage | local PostgreSQL + Redis (auto-started via Docker) | compose volumes | in-cluster / managed instances |
| Secrets | `.env` + auto-generated under `.data/` | `deploy/.env` | K8s Secrets |
| Migrations | automatic at `make dev` (or `make db-upgrade`) | one-shot migrate service | migration Jobs (`make k8s-migrate`) |
| Frontend | Vite dev server :5173 | nginx container :8080 | nginx Deployment + Ingress |
| Sandbox egress plane | — (no NetworkPolicy locally; see below) | — | deployed with the cell overlay |
| Cell isolation | — | `make global-up` + `make cell-up` | `overlays/global` + `overlays/cell` |

**The same four-layer config resolution serves all three** — a deployment
differs only in which profile is selected, which directory is mounted, and
which secrets are supplied. See [configuration.md](configuration.md).

## Frozen port assignment

Identical in every environment; only the host part of an address differs.

| Port | Service |
|---|---|
| 8000 | gateway |
| 8001 | agent-service (REST/WS) |
| 8002 | workspace-runtime |
| 8003 | identity-service |
| 8004 | control-service |
| 5173 | frontend (Vite dev server) |
| 8080 | frontend (nginx container, all-container stack) |
| 3000 | dagster-webserver |
| 3128 | egress-proxy |
| 4000 | code-location gRPC (pod-internal) |
| 50051 | agent-service gRPC |

## Capability matrix — what each shape does NOT give you

Being honest about gaps beats a config file that pretends:

- **host dev / Docker compose**: no NetworkPolicy, so the sandbox egress proxy
  is deliberately not configured — a proxy that mediates SOME traffic while
  the rest flows freely is an illusion of control. Sandboxes get no proxy
  configuration at all. Use the kind overlay or a real cluster to exercise
  the egress plane.
- **Docker compose**: cell isolation is available (global + cell stacks) but
  there is no scheduler-driven horizontal scaling; the replica counts are
  fixed in the compose files.
- **kind overlay**: production shapes at 1 replica and reduced resources. The
  security posture is NOT reduced — the egress plane and its policies apply
  unchanged.
- **Any single-machine shape**: task sandboxes run as hardened local
  subprocesses (L3-proc), not container-isolated pods. Design 04 §2 explains
  what that does and does not isolate.
