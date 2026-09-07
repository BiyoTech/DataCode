# task-runner

The sandbox execution plane for user code (design 04, P6a): every task kind
that executes tenant-authored code — `agent.shell`, `sqlmesh.plan`,
`sqlmesh.render_macro`, `sqlmesh.render_project`, `dagster.codelocation` —
runs HERE, in its own Pod, never in workspace-runtime's process.

## Two shapes, one code path

- **One-shot** (`src/main.py`): a Job per task. Hardened subprocess, result
  reported on stdout as a structured `@@DATACODE_TASK@@ {...}` line — not an
  HTTP callback. Pod logs outlive Pods, dev and production share the path,
  and no network policy is needed for the report channel.
- **Warm runner** (`src/serve.py --serve PORT`): a resident Pod per working
  copy (keyed by copy, NOT by tenant — a per-tenant runner would have to mount
  multi-copy subtrees, which is the T1 exposure subPath eliminated). One task
  at a time; a second gets `busy`, never a queue. Only read-only kinds are
  pooled; L2 write profiles are never pooled.

## Isolation is chosen, not assumed

The profile comes from `conf/isolation-profiles.yaml`, resolved by task kind
with **overlap-takes-strongest** and a fail-safe chain that never includes
L1/L2. The runner declares which levels it can execute; a deployment wanting
L2 gets it only if this process can actually deliver it, otherwise the task is
rejected — never silently downgraded.

Handlers are an explicit registry (`src/handlers.py`): a kind without a
registered handler fails loudly instead of routing somewhere by accident.

## Layout

```
src/
├── main.py        one-shot entrypoint
├── serve.py       warm-runner entrypoint (frame protocol, task token per run)
├── handlers.py    the explicit task-kind registry
├── sqlmesh_ops.py plan / render_project (two-phase: render read-only, apply writable)
├── shell.py       shell execution under the profile's constraints
└── stream.py      the @@DATACODE_TASK@@ stdout contract
```

## Run

The container is the deployment unit (`Dockerfile`, image `datacode/task-runner`).
Locally:

```
./.venv/bin/python -m pytest -q
```

See `scripts/verify/verify-sandbox-isolation.py` for the executable acceptance
gate, and `doc/design/04-*` for the threat model (T1–T10) this plane answers.
