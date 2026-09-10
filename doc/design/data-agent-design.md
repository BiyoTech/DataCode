# Data Agent Design Specification

> DataCode Platform — AI Data Agent Architecture
> Based on OpenAI Agents SDK, inspired by Claude Code & Cursor Composer

---

## 1. Design Philosophy

### 1.1 Core Principles

| Principle | Description |
|-----------|-------------|
| **Workspace-Native** | Agent lives inside the workspace, not in the cloud. Every workspace has its own `.agent/` directory with rules, skills, tasks, and plans. |
| **Mode-Driven** | Three distinct interaction modes: `ask` (read-only Q&A), `craft` (full code generation + execution, L1-L5 tools), `plan` (implementation-plan generation, like Claude Code's plan mode). Each mode has different agent behavior, tool access, and UI presentation. |
| **Timeline-First** | All agent activity — thoughts, tools, artifacts, approvals, handoffs — is modeled as a stream of events. The frontend renders these into an interactive Agent Timeline. |
| **Subagent-Composable** | Complex tasks are decomposed into specialist subagents (Triage, Model, Debug, Review, Deploy) that communicate via Handoff. Users can define custom subagents via YAML. |
| **Skill-Extensible** | Built-in skills cover common data engineering tasks. Users can install community skills or write custom ones. Skills are self-contained packages with tools, prompts, and templates. |
| **Rule-Governed** | Rules constrain agent behavior at workspace, project, or global scope. Rules can enforce coding standards, restrict tool usage, or inject context. |

### 1.2 Reference Architecture

```
+-------------------------------------------------------------+
|                      Frontend (Vue 3)                        |
|  +-------------------+  +-------------------------------+  |
|  |   Chat Panel      |  |   Agent Timeline              |  |
|  |   (Mode Switcher) |  |   (Thought/Tool/Artifact/     |  |
|  |                   |  |    Human/Multi-Agent cards)    |  |
|  +-------------------+  +-------------------------------+  |
+------------------------|------------------------------------+
                         | ACP v2 (WebSocket)
                         ▼
+-------------------------------------------------------------+
|                    Agent Service (Python)                    |
|  +-------------------------------------------------------+  |
|  |  Session Manager  —  Workspace Context  —  Mode Router |  |
|  +-------------------------------------------------------+  |
|  |  Agent Orchestrator (openai-agents SDK Runner)        |  |
|  |  ├── Triage Agent (YAML-defined, extensible)          |  |
|  |  ├── Model Agent  (SQLMesh model development)         |  |
|  |  ├── Debug Agent  (error diagnosis & fix)             |  |
|  |  ├── Review Agent (code review & standards)           |  |
|  |  ├── Deploy Agent (plan/apply/validation)             |  |
|  |  └── User-defined Subagents (loaded from .agent/)     |  |
|  +-------------------------------------------------------+  |
|  |  Skill Engine — Rule Engine — Tool Registry           |  |
|  +-------------------------------------------------------+  |
|  |  Event Bus → ACP v2 Protocol Encoder → WebSocket      |  |
|  +-------------------------------------------------------+  |
+------------------------|------------------------------------+
                         | gRPC / HTTP
         +---------------+---------------+
         ▼                               ▼
+-------------------+         +---------------------------+
| SQLMesh Service   |         | Platform Service (Go)     |
| (Plan/Apply/Run)  |         | (Tenant/User/Workspace)   |
+-------------------+         +---------------------------+
```

---

## 2. Workspace `.agent` Directory

When a user selects a workspace, the Agent Service reads a `.agent/` directory from the workspace root. This directory is the agent's "home" — it contains all local configuration, rules, skills, tasks, and plans.

It is READ, never seeded: nothing is created just because a session opened. The
defaults documented below live in code, so an absent `.agent/` behaves exactly
like an empty one, and the platform never writes into a user's repository behind
their back. Entries appear when the user or the agent actually puts something
there — through workspace-runtime, which owns the working copy (agent-service
has no filesystem access to it; see `agent-service/src/tools/workspace_fs.py`).

### 2.1 Directory Structure

```
{workspace_root}/
├── .agent/
│   ├── rules/
│   │   ├── 01-coding-standards.md      # Global rules for this workspace
│   │   ├── 02-sqlmesh-conventions.md
│   │   └── 03-security-guardrails.md
│   ├── skills/
│   │   ├── installed/                  # Symlinks or copies of installed skills
│   │   │   ├── dbt-migration/
│   │   │   └── data-quality-check/
│   │   └── custom/                     # User-written skills
│   │       └── my-company-models/
│   ├── tasks/                          # Saved task definitions
│   │   ├── onboard-new-developer.yaml
│   │   └── weekly-model-review.yaml
│   ├── design/                          # Implementation plans generated in Plan Mode
│   │   ├── design.md                     # Current plan (Markdown, human-reviewable)
│   │   └── archive/                    # Previously generated plans
│   ├── memory/
│   │   ├── index.md                    # Session-agnostic memory index
│   │   ├── decisions.md                # Key architectural decisions
│   │   └── errors.md                   # Known errors and resolutions
│   ├── context/
│   │   ├── workspace-summary.md        # Auto-generated project overview
│   │   ├── schema-index.json           # Cached schema metadata
│   │   └── model-catalog.json          # Cached model metadata
│   └── config.yaml                     # Agent configuration for this workspace
├── models/
├── tests/
├── audits/
├── config.yaml                         # SQLMesh config
└── ...
```

### 2.2 `config.yaml` — Workspace Agent Configuration

```yaml
# .agent/config.yaml
# Per-workspace agent configuration. Loaded on session init.

agent:
  name: "Data Agent"
  default_mode: "ask"              # ask | craft | plan
  auto_apply_rules: true           # Auto-apply rules on session start
  max_turns_per_message: 20        # Per-message turn ceiling; on overflow the run pauses and is resumable via "继续"
  max_tool_call_timeout_seconds: 600

  # Subagent routing configuration
  routing:
    default_agent: "triage"
    fallback_to_triage: true       # If handoff fails, return to triage

  # Memory management
  memory:
    enabled: true
    auto_summarize_after_turns: 10
    max_memory_tokens: 4000

  # Skill loading
  skills:
    auto_load_builtin: true
    auto_load_installed: true
    allowed_skills: []             # Empty = all allowed
    blocked_skills: []

  # Rules loading
  rules:
    auto_load_from_dir: true       # Load all .md files from .agent/rules/
    enforce_guardrails: true       # Reject requests that violate rules

  # Tool permissions
  tools:
    default_level: 1               # Default risk level for tools
    require_confirmation_above: 3  # L4+ needs human approval
    allowed_tools: []
    blocked_tools: []

  # LLM model selection
  llm:
    default_model: "gpt-4o"
    fallback_model: "gpt-4o-mini"
    temperature: 0.1
```

### 2.3 `rules/` — Rule System

Rules are Markdown files with YAML frontmatter. They constrain agent behavior and inject context.

#### Rule File Format

```markdown
---
name: "SQLMesh Naming Conventions"
priority: 10                          # Higher = applied later (can override)
scope: "workspace"                    # workspace | project | global
categories: ["coding-style", "sqlmesh"]
agents: ["model_agent", "review_agent"]  # Which agents this applies to
# Guardrails
forbid:
  - "Never use SELECT * in model SQL"
  - "Never hardcode environment names in SQL"
require:
  - "All models must have grain() defined"
  - "All models must have description in MODEL() block"
---

# SQLMesh Naming Conventions

## Model Names
- Use snake_case: `schema_name.model_name`
- Fact tables: `{schema}_fct_{entity}`
- Dimension tables: `{schema}_dim_{entity}`
- staging models: `staging_{source}_{entity}`

## Column Names
- Use snake_case for all column names
- Primary key columns: `{entity}_id`
- Timestamp columns: `{event}_at` (UTC)
- Boolean columns: `is_{condition}`

## Model Kinds
- Large tables (> 1B rows): `INCREMENTAL_BY_TIME_RANGE`
- Small reference tables (< 1M rows): `FULL`
- Simple views: `VIEW`
- Static data: `SEED`
```

#### Rule Engine Behavior

1. **Loading**: On session init, the Rule Engine loads all `.md` files from `.agent/rules/` sorted by filename (number prefix controls order).
2. **Scoping**: Rules with `scope: workspace` only apply to the current workspace. `scope: global` applies everywhere (defined in agent-service global config).
3. **Agent Filtering**: Rules are only injected into the system prompt of agents listed in `agents`. If `agents` is empty, the rule applies to all agents.
4. **Guardrail Enforcement**: Before any tool call or response, the Rule Engine checks `forbid` and `require` lists. A violation triggers `system.warning` or blocks the action.
5. **Context Injection**: The Markdown body (after frontmatter) is injected into the agent's system prompt, prefixed with a header like `## Rule: {name}`.

### 2.4 `memory/` — Persistent Memory

Agent memory survives across sessions. It's stored as Markdown files that the agent can read and write.

| File | Purpose | Auto-Managed |
|------|---------|--------------|
| `index.md` | High-level workspace summary, active decisions, current focus | Yes (agent updates after each session) |
| `decisions.md` | Architectural decisions made by the agent or user | Yes (agent appends) |
| `errors.md` | Known errors and their resolutions | Yes (agent appends) |
| `user-preferences.md` | User-specific preferences (coding style, communication style) | No (user edits) |

**Memory Update Protocol**:
- After every session, the Agent Service triggers a memory summarization subagent.
- The subagent reads `index.md`, updates it with new context, and appends to `decisions.md` if architectural decisions were made.
- The updated `index.md` is loaded into the context of the next session.

### 2.5 `context/` — Cached Workspace Context

Auto-generated files that provide fast context to the agent without re-scanning the workspace.

| File | Source | Update Trigger |
|------|--------|---------------|
| `workspace-summary.md` | Scans `models/`, `tests/`, `audits/`, `config.yaml` | On file watcher events or manual refresh |
| `schema-index.json` | SQLMesh Service `schema_discovery` API | On demand or scheduled |
| `model-catalog.json` | SQLMesh Service `list_models` API | On file watcher events |
| `lineage-graph.json` | SQLMesh Service `get_lineage` API | On model changes |

---

## 3. Interaction Modes

The agent supports three distinct interaction modes. The user can switch modes via the frontend UI. Each mode changes the active agent, available tools, and frontend presentation.

### 3.1 Ask Mode

**Purpose**: Answer questions about the workspace, SQLMesh concepts, data schemas, and existing models. Read-only by default.

**Behavior**:
- Agent: `triage` → routed to `knowledge_agent` or `model_agent` (read-only)
- Tools: `schema_discovery`, `list_models`, `get_lineage`, `read_file`, `search_code`
- Risk Level: L1 (read-only)
- No human approval required
- Frontend: Standard chat bubbles, no timeline cards

**Example Prompts**:
- "What models depend on `staging_orders`?"
- "Explain the difference between FULL and INCREMENTAL_BY_TIME_RANGE"
- "Show me the schema of the `customers` table"

### 3.2 Craft Mode

**Purpose**: Build and ship changes. Generate, modify, or refactor code (SQLMesh models, tests, audits, macros) **and** execute the full workflow — preview, apply, test, and audit. This is the primary "do the work" mode and has access to **all tools (L1-L5)**.

**Behavior**:
- Agent: `triage` → routed to `model_agent` / `debug_agent` (which may delegate to `deploy_agent`)
- Tools: **All tools (L1-L5)** — `schema_discovery`, `list_models`, `get_lineage`, `read_file`, `search_code`, `write_file`, `edit_file`, `create_file`, `preview_plan`, `apply_plan`, `run_tests`, `run_audit`
- Risk Level: L1-L5 (file writes through environment mutation)
- Human approval required for destructive tools (e.g. `apply_plan`) per `require_confirmation_above`
- Frontend: Chat + Artifact cards (generated files), inline diff previews, terminal output streaming, approval gates

**Example Prompts**:
- "Create a new model `analytics.fct_orders` that joins orders and customers"
- "Refactor `staging_users` to add an `is_active` flag"
- "Add a test for `fct_orders` ensuring `total_amount > 0`, then preview and apply to dev"

### 3.3 Plan Mode

**Purpose**: Turn a user request into a concrete **implementation plan** — exactly like Claude Code's plan mode. The agent investigates the workspace (read-only) and writes a structured plan to `.agent/design/{design_name}.md`. **It never executes the plan** (no `apply_plan`, no tests, no model edits). The user reviews the plan and then switches to Craft Mode to execute it.

**Behavior**:
- Agent: `triage` → routed to `plan_agent`
- Tools: read-only investigation tools (`schema_discovery`, `list_models`, `get_lineage`, `read_file`, `search_code`) + `write_file` **restricted to `.agent/design/{design_name}.md`**
- Risk Level: L1 (read-only investigation; the only write is the plan document)
- No environment mutation, no SQLMesh command execution
- Frontend: Plan document preview (`design.md`) rendered as an editable, reviewable artifact

**Example Prompts**:
- "Plan how to build the new `fct_orders` model and its tests"
- "Draft an implementation plan to migrate `staging_users` to incremental"
- "Lay out the steps to add data-quality audits across the analytics layer"

### 3.4 Mode State Machine

```
[Idle] --(user selects mode)--> [Mode Active]
   |
   |-- ask mode --> [Asking] --(response complete)--> [Idle]
   |
   |-- plan mode --> [Investigating] --(plan written to design.md)--> [Plan Ready]
   |                                          |
   |                                          |--(user edits plan)--> [Investigating]
   |                                          |--(user switches to craft)--> [Crafting]
   |
   |-- craft mode --> [Crafting] --(file artifact / preview)--> [Confirm?]
                       |                          |
                       |                          |--(user confirms)--> [Applying] --(apply/tests)--> [Applied] --> [Idle]
                       |                          |--(user rejects)--> [Revising] --> [Crafting]
```


---

## 4. Agent Orchestration

### 4.1 Agent Hierarchy

```
Triage Agent (entry point, always active)
    ├── Model Agent     (SQLMesh model CRUD)
    ├── Debug Agent     (error diagnosis, log analysis)
    ├── Review Agent    (code review, standards compliance)
    ├── Deploy Agent    (plan/apply/test orchestration)
    ├── Knowledge Agent (SQLMesh docs, best practices)
    └── User-defined Subagents (loaded from .agent/ or src/subagents/)
```

### 4.2 Triage Agent

The Triage Agent is the entry point for every user message. It classifies intent, gathers context, and decides whether to handle the request itself or handoff to a specialist.

**Triage Decision Flow**:

```
User Message
    |
    ▼
[Intent Classification]
    ├── "question"          → Knowledge Agent (or answer directly)
    ├── "create_model"      → Model Agent
    ├── "modify_model"      → Model Agent
    ├── "debug_error"       → Debug Agent
    ├── "review_code"       → Review Agent
    ├── "deploy"            → Deploy Agent
    ├── "plan_request"      → Plan Agent
    └── "ambiguous"         → Ask clarifying question
```

**Triage System Prompt** (excerpt):

```
You are the Triage Agent for a data development platform.

## Your Job:
1. Classify the user's intent into one of: question, create_model, modify_model,
   debug_error, review_code, deploy, plan_workflow.
2. Gather relevant workspace context (active files, recent changes, current environment).
3. Route to the appropriate specialist via Handoff.
4. If the intent is a simple question, answer it directly.

## Context Available:
- Current workspace: {workspace_path}
- Active mode: {current_mode}
- Current environment: {current_environment}
- Recently modified files: {recent_files}
- Open files in editor: {open_files}
```

### 4.3 Built-in Subagents

Built-in subagents are defined as YAML files in `agent-service/src/subagents/` and loaded at startup.

#### YAML Subagent Definition Format

```yaml
# agent-service/src/subagents/model_agent.yaml
name: "model_agent"
display_name: "Model Agent"
description: "Specializes in SQLMesh data model development"
mode: "craft"                      # ask | craft | plan
risk_level: 2                      # 1-6

system_prompt: |
  You are the Model Agent, a specialized AI assistant for SQLMesh data model development.

  ## Your Capabilities:
  1. Schema Discovery: Query the data warehouse for available tables
  2. Model Creation: Generate SQLMesh model SQL files
  3. Model Modification: Edit existing models
  4. Plan Preview: Preview impact before deployment

  ## Rules:
  - Always check schemas before writing SQL
  - Use proper naming: schema.model_name (snake_case)
  - All models must have grain() and description
  - Prefer INCREMENTAL_BY_TIME_RANGE for large tables

  ## Workflow:
  1. Discover schemas
  2. Design model SQL
  3. Create/modify file
  4. Preview plan (if requested)
  5. Apply plan (if in plan mode and approved)

tools:
  - schema_discovery
  - list_models
  - get_lineage
  - read_file
  - write_file
  - edit_file
  - preview_plan

handoffs:
  - debug_agent        # Can handoff for error diagnosis
  - review_agent       # Can handoff for code review

output_rules:
  - "Always show the generated SQL before creating the file"
  - "After file creation, suggest running preview_plan"
```

#### Built-in Subagent Catalog

| Agent           | File                       | Mode | Risk | Description                                                                                            |
|-----------------|----------------------------|------|------|--------------------------------------------------------------------------------------------------------|
| `triage`        | `triage.yaml`              | all | 1 | Entry point, intent routing                                                                            |
| `model`         | `model_agent.yaml`         | craft | 2 | SQLMesh model CRUD                                                                                     |
| `debug`         | `debug_agent.yaml`         | ask/craft | 1-2 | Error diagnosis, log analysis                                                                          |
| `review`        | `review_agent.yaml`        | ask | 1 | Code review, standards check                                                                           |
| `deploy`        | `deploy_agent.yaml`        | plan | 3-5 | Plan/apply/test orchestration                                                                          |
| `knowledge`     | `knowledge_agent.yaml`     | ask | 1 | SQLMesh docs, best practices                                                                           |
| `design_agent`  | `design_agent.yaml`        | plan | 1 | Generate an implementation plan (design.md) from a user request; read-only investigation, no execution |
| `code-explorer` | `code_explorer_agent.yaml` | all | 1 | code exploration and analysis                                                                          |
| `memory`        | `memory_agent.yaml`        | all | 1 | Memory summarization, indexing                                                                         |

### 4.4 Handoff Protocol

When an agent decides to delegate to another agent, it emits a `agent.delegate` event.

```python
# Conceptual handoff flow (openai-agents SDK)
# Triage Agent decides user wants to create a model

result = await Runner.run(triage_agent, input=user_message)
# result includes a handoff call to model_agent

# The orchestrator:
# 1. Emits agent.delegate event to frontend
# 2. Transfers conversation context to model_agent
# 3. Runs model_agent
# 4. Emits agent.finish when model_agent completes
# 5. Optionally returns control to triage_agent
```

**Handoff Event**:

```json
{
  "type": "agent.delegate",
  "session_id": "uuid",
  "payload": {
    "from_agent": "triage",
    "to_agent": "model_agent",
    "reason": "User wants to create a new SQLMesh model",
    "context_summary": "User requested a fact table for orders",
    "conversation_truncated": false
  }
}
```

---

## 5. Skills System

Skills are self-contained packages that extend agent capabilities. A skill can provide:
- Custom tools (Python functions)
- Prompt templates
- Model templates
- Validation rules

### 5.1 Skill Directory Structure

```
agent-service/src/skills/{skill_name}/
├── skill.yaml          # Skill manifest
├── readme.md          # skill description
├── tools/
│   ├── __init__.py
│   └── migrate_dbt.py  # Tool implementations
├── prompts/
│   └── dbt-to-sqlmesh.jinja2
├── templates/
│   └── model_template.sql
└── rules/
    └── dbt-migration-rules.md
```

### 5.2 Skill Manifest (`skill.yaml`)

```yaml
name: "dbt-migration"
display_name: "DBT to SQLMesh Migration"
description: "Migrate dbt models, tests, and macros to SQLMesh"
version: "1.0.0"
author: "DataCode"
categories: ["migration", "dbt"]

# Requirements
requires:
  agent_version: ">=0.2.0"
  skills: []           # Dependency skills

# Tools provided
tools:
  - name: "parse_dbt_model"
    description: "Parse a dbt model file and extract metadata"
    agent: "model_agent"
    level: 1
  - name: "generate_sqlmesh_model"
    description: "Generate SQLMesh model from dbt model"
    agent: "model_agent"
    level: 2

# Prompts
prompts:
  migration_guide: "prompts/dbt-to-sqlmesh.jinja2"

# Templates
templates:
  model: "templates/model_template.sql"

# Rules injected when skill is active
rules:
  - "rules/dbt-migration-rules.md"

# Installation hook
install:
  - "pip install dbt-parser>=2.0"
```

### 5.3 Skill Lifecycle

```
[Discovery]        Scan src/skills/ (built-in) and .agent/skills/installed/ (user)
    |
    ▼
[Registration]     Parse skill.yaml, register tools, load rules, compile prompts
    |
    ▼
[Activation]       When a session starts, activate skills based on config
    |
    ▼
[Execution]        Tools are available to agents, rules are injected into prompts
    |
    ▼
[Update]           Check for skill updates, reload dynamically
```

### 5.4 Built-in Skills

| Skill | Description | Tools |
|-------|-------------|-------|
| `sqlmesh-core` | Core SQLMesh operations | `preview_plan`, `apply_plan`, `run_tests`, `run_audit` |
| `schema-introspection` | Data warehouse schema discovery | `schema_discovery`, `table_stats`, `column_profiler` |
| `lineage-analysis` | Model dependency analysis | `get_lineage`, `impact_analysis`, `downstream_models` |
| `code-generation` | SQLMesh model/code generation | `create_file`, `edit_file`, `refactor_model` |
| `data-quality` | Data quality checks | `run_tests`, `run_audit`, `data_diff` |
| `git-integration` | Git operations | `git_status`, `git_commit`, `git_branch` |

### 5.5 User-Defined Skills

Users can install skills via the frontend:
1. Browse a skill marketplace (GitHub repo or internal registry)
2. Click "Install" → Agent Service clones skill to `.agent/skills/installed/`
3. Skill is auto-registered on next session start
4. User can enable/disable per skill

Users can also write custom skills in `.agent/skills/custom/`:
```bash
.agent/skills/custom/my-skill/
├── skill.yaml
├── tools/
│   └── my_tool.py
└── prompts/
    └── my_prompt.jinja2
```

---

## 6. Rules System

### 6.1 Rule Types

| Type | Scope | Enforcement | Example |
|------|-------|-------------|---------|
| **Prompt Rule** | Workspace | Injected into system prompt | "Always use snake_case" |
| **Guardrail Rule** | Workspace | Pre-action validation | "Never use SELECT *" |
| **Tool Rule** | Workspace | Tool access control | "Disable apply_plan in prod" |
| **Output Rule** | Workspace | Post-action validation | "All models must have tests" |

### 6.2 Rule Engine Architecture

```
User Request
    |
    ▼
[Prompt Rules]  --> Inject into agent system prompt
    |
    ▼
[Agent Decision] --> LLM generates response / tool call
    |
    ▼
[Guardrail Rules] --> Validate before execution
    |-- Violation? --> Block + emit system.warning + ask user
    |
    ▼
[Tool Rules] --> Check tool permission level
    |-- Forbidden? --> Block + emit system.error
    |
    ▼
[Execute Tool]
    |
    ▼
[Output Rules] --> Validate result
    |-- Violation? --> Emit system.warning, suggest fix
    |
    ▼
[Return to User]
```

### 6.3 Rule Priority and Conflict Resolution

Rules are applied in filename order (number prefix). Later rules override earlier ones.

```
rules/
├── 00-global-guardrails.md      # Base layer
├── 10-sqlmesh-conventions.md    # Middle layer
├── 20-team-override.md          # Top layer (wins on conflict)
└── 99-user-personal.md          # User-specific (highest priority)
```

---

## 7. Tools

### 7.1 Tool Risk Levels

| Level | Name | Description | Examples |
|-------|------|-------------|----------|
| L1 | Read | Read-only queries | `schema_discovery`, `list_models`, `read_file` |
| L2 | Write | File system writes | `create_file`, `edit_file`, `write_file` |
| L3 | Plan | Non-destructive plan ops | `preview_plan`, `validate_model` |
| L4 | Apply | Destructive plan ops | `apply_plan` (dev only) |
| L5 | Deploy | Environment mutation | `apply_plan` (staging/prod) |
| L6 | Admin | Administrative ops | `delete_environment`, `manage_users` |

### 7.2 Tool Registry

Tools are registered via decorators and assigned to agents:

```python
from agents import function_tool
from src.tools.registry import ToolRegistry

@ToolRegistry.register(
    name="preview_plan",
    agents=["model_agent", "deploy_agent"],
    level=3,
    description="Preview SQLMesh plan without applying",
)
@function_tool
def preview_plan(environment: str = "dev") -> str:
    ...
```

### 7.3 Tool Execution Context

Every tool receives a `RunContextWrapper` from the openai-agents SDK, which includes:
- `session_id`: Current chat session
- `workspace_path`: Active workspace
- `user_id`: Current user
- `current_environment`: Active SQLMesh environment
- `open_files`: Files currently open in the editor

```python
from agents import RunContextWrapper

@function_tool
def create_file(
    ctx: RunContextWrapper,
    file_path: str,
    content: str,
) -> str:
    workspace_path = ctx.context.workspace_path
    full_path = Path(workspace_path) / file_path
    full_path.write_text(content)
    return f"Created {file_path}"
```

---

## 8. Agent Client Protocol v2 (ACP)

ACP v2 is the standardized message protocol between frontend and Agent Service. All agent activity is modeled as a stream of typed events. The frontend renders these events into an interactive **Agent Timeline**.

### 8.1 Event Taxonomy

Events are grouped into 6 semantic categories:

| Category | Events | Frontend Rendering |
|----------|--------|-------------------|
| **Task** | task.created, task.running, task.completed, task.failed | Task header card with status indicator |
| **Agent** | agent.start, agent.finish, agent.delegate | Agent avatar + name + status |
| **Thought** | thought.plan, thought.update | Collapsible reasoning card |
| **Tool** | tool.start, tool.progress, tool.end, tool.error | Tool execution card with spinner/result |
| **Artifact** | artifact.created, artifact.diff, artifact.file, artifact.image, artifact.chart | File card with syntax highlight / diff / preview |
| **Human** | human.required, human.response | Approval gate / input form |
| **Message** | message.delta, message.complete | Streaming text |
| **System** | system.warning, system.error | Toast / alert banner |

### 8.2 Event Schema

All events share a common envelope:

```json
{
  "type": "{category}.{event}",
  "session_id": "uuid",
  "event_id": "uuid",
  "parent_event_id": "uuid | null",
  "timestamp": "2026-06-03T21:34:00Z",
  "payload": { ... }
}
```

#### Task Events

```json
// task.created
{
  "type": "task.created",
  "session_id": "sess-001",
  "event_id": "ev-001",
  "parent_event_id": null,
  "timestamp": "2026-06-03T21:34:00Z",
  "payload": {
    "task_id": "task-001",
    "task_type": "craft",
    "title": "Deploy fct_orders to dev",
    "description": "Create and deploy the orders fact table model",
    "mode": "craft",
    "agent": "model_agent",
    "steps": [
      {"step_id": "s1", "title": "Create model file", "status": "pending"},
      {"step_id": "s2", "title": "Preview plan", "status": "pending"},
      {"step_id": "s3", "title": "Apply to dev", "status": "pending"},
      {"step_id": "s4", "title": "Run tests", "status": "pending"}
    ]
  }
}

// task.running
{
  "type": "task.running",
  "payload": {
    "task_id": "task-001",
    "current_step_id": "s1",
    "progress": 0.25
  }
}

// task.completed
{
  "type": "task.completed",
  "payload": {
    "task_id": "task-001",
    "result_summary": "Model created and deployed successfully",
    "artifacts_created": ["models/analytics/fct_orders.sql"]
  }
}

// task.failed
{
  "type": "task.failed",
  "payload": {
    "task_id": "task-001",
    "error": "Plan apply failed: Test failure in fct_orders",
    "failed_step_id": "s4",
    "recoverable": true
  }
}
```

#### Agent Events

```json
// agent.start
{
  "type": "agent.start",
  "payload": {
    "agent_name": "model_agent",
    "agent_display_name": "Model Agent",
    "task_id": "task-001",
    "mode": "craft"
  }
}

// agent.finish
{
  "type": "agent.finish",
  "payload": {
    "agent_name": "model_agent",
    "task_id": "task-001",
    "outcome": "success",
    "summary": "Created models/analytics/fct_orders.sql"
  }
}

// agent.delegate
{
  "type": "agent.delegate",
  "payload": {
    "from_agent": "triage",
    "to_agent": "model_agent",
    "task_id": "task-001",
    "reason": "User wants to create a new SQLMesh model",
    "context_summary": "Fact table for orders with customer join"
  }
}
```

#### Thought Events

```json
// thought.plan
{
  "type": "thought.plan",
  "payload": {
    "agent_name": "plan_agent",
    "plan": [
      "1. Discover schema for orders and customers tables",
      "2. Design SQLMesh model with proper grain and joins",
      "3. Create model file in models/analytics/",
      "4. Preview plan to validate changes",
      "5. Apply plan to dev environment",
      "6. Run tests to validate correctness"
    ],
    "estimated_steps": 6
  }
}

// thought.update
{
  "type": "thought.update",
  "payload": {
    "agent_name": "model_agent",
    "thought": "The orders table has order_id, customer_id, order_date, and total_amount. I need to join with customers to get customer_name and customer_segment. The grain should be order_id.",
    "context": {
      "relevant_files": ["models/staging/stg_orders.sql"],
      "schema_discovered": true
    }
  }
}
```

#### Tool Events

```json
// tool.start
{
  "type": "tool.start",
  "payload": {
    "tool_name": "schema_discovery",
    "tool_display_name": "Schema Discovery",
    "agent_name": "model_agent",
    "arguments": {"table_name": "orders"},
    "task_id": "task-001"
  }
}

// tool.progress
{
  "type": "tool.progress",
  "payload": {
    "tool_name": "schema_discovery",
    "message": "Querying data warehouse for table schema...",
    "progress": 0.5
  }
}

// tool.end
{
  "type": "tool.end",
  "payload": {
    "tool_name": "schema_discovery",
    "result": {
      "table": "orders",
      "columns": [
        {"name": "order_id", "type": "INT", "nullable": false},
        {"name": "customer_id", "type": "INT", "nullable": false},
        {"name": "order_date", "type": "TIMESTAMP", "nullable": false},
        {"name": "total_amount", "type": "DECIMAL(18,2)", "nullable": false}
      ]
    },
    "duration_ms": 1200
  }
}

// tool.error
{
  "type": "tool.error",
  "payload": {
    "tool_name": "apply_plan",
    "error": "ConflictingPlanError: Another plan is already being applied",
    "code": "CONFLICT",
    "recoverable": true
  }
}
```

#### Terminal Events (for long-running commands)

```json
// tool.terminal.start
{
  "type": "tool.terminal.start",
  "payload": {
    "tool_name": "sqlmesh_plan",
    "command": "sqlmesh plan --environment dev",
    "task_id": "task-001"
  }
}

// tool.terminal.output
{
  "type": "tool.terminal.output",
  "payload": {
    "tool_name": "sqlmesh_plan",
    "output": "New environment `dev` will be created\nModels:\n  - analytics.fct_orders (added)\n",
    "is_stderr": false
  }
}

// tool.terminal.end
{
  "type": "tool.terminal.end",
  "payload": {
    "tool_name": "sqlmesh_plan",
    "exit_code": 0,
    "duration_ms": 3500
  }
}
```

#### Artifact Events

```json
// artifact.created
{
  "type": "artifact.created",
  "payload": {
    "artifact_id": "art-001",
    "artifact_type": "file",
    "file_path": "models/analytics/fct_orders.sql",
    "language": "sql",
    "content": "MODEL (...)\nSELECT ...",
    "task_id": "task-001"
  }
}

// artifact.diff
{
  "type": "artifact.diff",
  "payload": {
    "artifact_id": "art-002",
    "file_path": "models/staging/stg_orders.sql",
    "language": "diff",
    "diff": "@@ -1,5 +1,6 @@\n SELECT\n   order_id,\n+  is_active,\n   customer_id,\n   ...",
    "task_id": "task-001"
  }
}

// artifact.file
{
  "type": "artifact.file",
  "payload": {
    "artifact_id": "art-003",
    "file_path": "reports/weekly_metrics.html",
    "language": "html",
    "preview_url": "/api/v1/artifacts/art-003/preview",
    "task_id": "task-001"
  }
}

// artifact.image
{
  "type": "artifact.image",
  "payload": {
    "artifact_id": "art-004",
    "file_path": "reports/lineage_graph.png",
    "mime_type": "image/png",
    "data": "base64...",
    "task_id": "task-001"
  }
}

// artifact.chart
{
  "type": "artifact.chart",
  "payload": {
    "artifact_id": "art-005",
    "chart_type": "lineage",
    "data": { "nodes": [...], "edges": [...] },
    "task_id": "task-001"
  }
}
```

#### Human Events

```json
// human.required
{
  "type": "human.required",
  "payload": {
    "request_id": "req-001",
    "request_type": "approval",      # approval | input | selection
    "title": "Apply Plan to Dev?",
    "description": "The plan will create 1 new model and requires backfill for 7 days of data.",
    "risk_level": 4,
    "options": [
      {"id": "approve", "label": "Apply", "style": "primary"},
      {"id": "cancel", "label": "Cancel", "style": "secondary"}
    ],
    "timeout_seconds": 300,
    "task_id": "task-001"
  }
}

// human.response
{
  "type": "human.response",
  "payload": {
    "request_id": "req-001",
    "response": "approve",
    "response_data": {}
  }
}
```

#### Message Events

```json
// message.delta
{
  "type": "message.delta",
  "payload": {
    "agent_name": "model_agent",
    "delta": "I'll create the `fct_orders` model",
    "task_id": "task-001"
  }
}

// message.complete
{
  "type": "message.complete",
  "payload": {
    "agent_name": "model_agent",
    "content": "I've created the `fct_orders` model. Here's what it does...",
    "task_id": "task-001"
  }
}
```

#### System Events

```json
// system.warning
{
  "type": "system.warning",
  "payload": {
    "code": "RULE_VIOLATION",
    "message": "Generated model is missing grain() definition",
    "rule_name": "SQLMesh Naming Conventions",
    "suggestion": "Add grain(order_id) to the MODEL() block"
  }
}

// system.error
{
  "type": "system.error",
  "payload": {
    "code": "TOOL_TIMEOUT",
    "message": "Schema discovery timed out after 30s",
    "recoverable": true,
    "retryable": true
  }
}
```

### 8.3 Client → Server Messages

```json
// chat.message
{
  "type": "chat.message",
  "session_id": "sess-001",
  "payload": {
    "content": "Create a fact table for orders",
    "mode": "craft",
    "attachments": []
  }
}

// chat.cancel
{
  "type": "chat.cancel",
  "session_id": "sess-001",
  "payload": {
    "task_id": "task-001"
  }
}

// action.confirm
{
  "type": "action.confirm",
  "session_id": "sess-001",
  "payload": {
    "request_id": "req-001",
    "response": "approve"
  }
}

// action.reject
{
  "type": "action.reject",
  "session_id": "sess-001",
  "payload": {
    "request_id": "req-001",
    "response": "cancel",
    "reason": "I need to change the grain definition first"
  }
}

// context.file_change
{
  "type": "context.file_change",
  "session_id": "sess-001",
  "payload": {
    "file_path": "models/analytics/fct_orders.sql",
    "change_type": "modified",
    "diff": "..."
  }
}

// context.file_open
{
  "type": "context.file_open",
  "session_id": "sess-001",
  "payload": {
    "file_path": "models/analytics/fct_orders.sql",
    "language": "sql",
    "cursor_position": {"line": 10, "column": 5}
  }
}
```

---

## 9. Agent Timeline Frontend

The Agent Timeline is the primary UI for Plan Mode. It renders the event stream as a vertical timeline of interactive cards.

### 9.1 Timeline Card Types

| Card | Events | Visual |
|------|--------|--------|
| **Task Header** | task.* | Colored banner with task name, progress bar, status |
| **Agent Badge** | agent.start, agent.finish | Avatar + name pill, animated when active |
| **Thought Card** | thought.* | Light bulb icon, expandable to show reasoning |
| **Tool Card** | tool.* | Wrench icon, spinner while running, green check on success |
| **Terminal Card** | tool.terminal.* | Terminal emulator with ANSI colors, scrollable |
| **Artifact Card** | artifact.* | File icon with syntax highlight, diff view, or image preview |
| **Approval Gate** | human.required | Modal-like card with action buttons, blocks timeline |
| **Message Bubble** | message.* | Standard chat bubble, streams deltas |
| **System Alert** | system.* | Warning/error banner at top of timeline |

### 9.2 Timeline Layout

```
+--------------------------------------------------+
| Task: Deploy fct_orders to dev        [Running]  |
| [=====>                    ] 25%                 |
+--------------------------------------------------+
  |-- Agent: Model Agent started
  |-- Thought: "I'll create a fact table..."
  |-- Message: "I'll create the `fct_orders` model..."
  |-- Tool: schema_discovery [✓ 1.2s]
  |     |-- Result: 4 columns found
  |-- Thought: "orders has order_id, customer_id..."
  |-- Artifact: models/analytics/fct_orders.sql [New]
  |     |-- [Code preview with syntax highlight]
  |-- Agent: Model Agent finished
  |-- Agent: Deploy Agent started
  |-- Tool: preview_plan [Running...]
  |     |-- Terminal output streaming...
```

### 9.3 Interaction Patterns

- **Expand/Collapse**: All cards except messages are collapsible. User can collapse completed steps to focus on active ones.
- **Re-run Tool**: On `tool.error`, user can click "Retry" to re-execute the tool.
- **Edit Artifact**: On `artifact.created`, user can click "Edit" to modify the file in the editor.
- **Approve/Reject**: On `human.required`, the timeline pauses until user responds.
- **Scroll-to-Active**: Auto-scroll to the most recent event. User can pin a card to keep it visible.

---

## 10. Session Lifecycle

```
[User selects workspace]
    |
    ▼
[Agent Service: load workspace context]
    ├── Read .agent/config.yaml            (via workspace-runtime)
    ├── Load rules from .agent/rules/*.md  (via workspace-runtime)
    ├── Load recollection docs .agent/memory/*.md
    ├── Load skills from src/skills/ (built-in manifests)
    └── Build agent system prompts with rules + memory
    |
    ▼
[WebSocket connect]
    ├── Frontend sends session.create
    ├── Agent Service creates SessionContext
    └── Emit: agent.start(triage)
    |
    ▼
[Chat Loop]
    ├── User sends chat.message
    ├── Triage Agent classifies intent
    ├── Handoff to specialist (or answer directly)
    ├── Specialist agent executes (tools, artifacts, thoughts)
    ├── Events streamed via WebSocket
    └── Frontend renders Timeline
    |
    ▼
[Session End]
    ├── User closes tab / disconnects
    ├── Agent Service triggers memory summarization
    ├── Update .agent/memory/index.md
    └── Archive session
```

---

## 11. Implementation Phases

### Phase 1: Foundation (MVP)

**Goal**: Ask mode with Triage + Model Agent, basic ACP v2 protocol

- [ ] ACP v2 message protocol (all event types)
- [ ] WebSocket endpoint with bidirectional streaming
- [ ] Triage Agent with handoff to Model Agent
- [ ] Basic tools: `schema_discovery`, `read_file`, `create_file`, `preview_plan`
- [ ] Frontend: Chat panel + basic timeline (message + tool cards)
- [ ] `.agent/` directory auto-creation
- [ ] Rule loading (prompt injection only)

### Phase 2: Craft Mode

**Goal**: Full build-and-ship workflow — file generation/editing plus execution (preview/apply/test/audit)

- [ ] Craft mode switcher in frontend
- [ ] `write_file`, `edit_file`, `create_file` tools
- [ ] Artifact cards with syntax highlighting
- [ ] Diff preview for file modifications
- [ ] Execution tools: `preview_plan`, `apply_plan`, `run_tests`, `run_audit`
- [ ] Human approval gates for destructive tools (e.g. `apply_plan`)
- [ ] Terminal output streaming
- [ ] Basic rule guardrails (forbid/require lists)
- [ ] Memory system (index.md auto-update)

### Phase 3: Plan Mode

**Goal**: Implementation-plan generation (like Claude Code's plan mode)

- [ ] Plan Agent (read-only investigation, no execution)
- [ ] `thought.plan` event stream for the plan outline
- [ ] Write structured plan to `.agent/design/{design_name}.md`
- [ ] Plan document preview rendered as an editable artifact
- [ ] "Switch to Craft Mode to execute" handoff from a ready plan

### Phase 4: Extensibility

**Goal**: Custom subagents, skills, and rules

- [ ] YAML-defined subagents
- [ ] Skill package format + registry
- [ ] User-defined skills in `.agent/skills/custom/`
- [ ] Skill marketplace (install from GitHub)
- [ ] Advanced rule engine (tool rules, output rules)
- [ ] Debug Agent + Review Agent

### Phase 5: Production Hardening

**Goal**: Multi-tenant, scalable, observable

- [ ] Session persistence in PostgreSQL
- [ ] Concurrent session management
- [ ] Rate limiting and quota enforcement
- [ ] Comprehensive metrics and tracing
- [ ] Skill versioning and rollback
- [ ] A/B testing for agent prompts

---

## 12. Key Design Decisions

| # | Decision | Rationale | Alternatives |
|---|----------|-----------|--------------|
| 1 | OpenAI Agents SDK | Native Handoff, Tool use, Guardrails. Best agent abstraction in 2025. | LangGraph (complex), CrewAI (less structured) |
| 2 | YAML-defined subagents | Easy to extend without code changes. Version controllable. | Python-only (harder for users) |
| 3 | `.agent/` directory in workspace | Agent config lives with code. Git-trackable. Portable. | Centralized DB config (opaque) |
| 4 | ACP v2 event protocol | All activity as events enables rich Timeline UI. Replayable. | Simple request/response (limited UX) |
| 5 | Markdown-based rules | Human-readable, editable in any editor. Frontmatter for metadata. | JSON/YAML (less readable) |
| 6 | Skill packages | Self-contained, installable, versioned. Community extensible. | Monolithic tool registry (rigid) |
| 7 | Three modes (ask/craft/plan) | Clear UX mental model. Different risk/approval profiles. | Single mode (confusing) |
| 8 | Memory as Markdown files | Survives across sessions. Git-trackable. Human inspectable. | In-memory only (lost on restart) |

---

## Appendix A: File Paths

| Component | Path |
|-----------|------|
| Agent Service entry | `agent-service/src/main.py` |
| Agent Registry | `agent-service/src/agents/registry.py` |
| Tool Registry | `agent-service/src/tools/registry.py` |
| ACP Protocol | `agent-service/src/protocol/messages.py` |
| Session Manager | `agent-service/src/context/session.py` |
| Built-in subagents | `agent-service/src/subagents/*.yaml` |
| Built-in skills | `agent-service/src/skills/*/` |
| Workspace config | `{workspace}/.agent/config.yaml` |
| Workspace rules | `{workspace}/.agent/rules/*.md` |
| Workspace memory | `{workspace}/.agent/memory/*.md` |
| Design doc | `doc/design/data-agent-design.md` |
