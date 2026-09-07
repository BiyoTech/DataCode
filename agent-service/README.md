# agent-service

The conversational layer (Python, `:8001`): multi-agent chat over the OpenAI
Agents SDK, scaling on LLM concurrency alone — which it can only do because it
owns no working-copy state and spawns no user code.

## The split that defines this service

Agent and runtime split the two orthogonal halves of "run a command":

- **agent-service keeps the CONVERSATIONAL layer** — command policy, the
  L1–L6 approval flow, session memory, tool schemas. "Should this be done" is
  a dialogue question.
- **workspace-runtime owns the ISOLATION layer** — cleared environment,
  rlimits, container. "Doing it cannot hurt anyone else" is an infrastructure
  property.

Every gated tool call (shell, read/write, query, lineage, models, schema)
crosses `src/tools/runtime_client.py` into workspace-runtime. That client is
LEASE-AWARE: it resolves the replica holding the working copy through the same
shared router the gateway uses (`datacode_common.lease_routing`), repairs a
stale route on `409 LEASE_NOT_HELD`, and waits out `WORKSPACE_MATERIALIZING` /
`WORKSPACE_RELOCATING` — because `services.runtime` is a HEADLESS Service in
Kubernetes, and a direct dial lands on an arbitrary replica.

## Layout

```
src/
├── agents/        agent factory, triage, per-domain agents (design/data/…)
├── api/           REST + SSE chat routes
├── context/       session state, memory, timeline, event bus
├── protocol/      message contracts
├── runtime/       goal runs, streaming, approval, resume, model resolver
├── skills/        built-in skills (SKILL.md manifests)
├── subagents/     agent YAML declarations (prompts, per-agent config)
├── sqlmesh_docs/  curated SQLMesh docs served to the docs tool
├── tools/         the LLM tool surface; runtime_client.py is the lease-aware
│                  HTTP client every gated tool goes through
└── utils/         logging helpers
```

## Boundaries

- No filesystem access to working copies; no subprocesses running user code.
- The shell CLI shims (`dc`, `sqlmesh` wrappers) get their upstream address
  from lease resolution too — never from the raw Service URL.
- `AGENT_SERVICE_LLM_API_KEY` is the global fallback LLM key (env-only).

## Run

```
python3.13 -m venv .venv && ./.venv/bin/pip install -e ../shared/python -e ".[dev]"
./.venv/bin/python -m pytest -q
./.venv/bin/uvicorn src.main:app --port 8001
```
