
# DataOps 多租户平台设计方案（AI 原生版 v2）

> 基于 SQLMesh + Airflow + OpenAI Agents SDK 的 AI 原生数据开发平台
> Vibe Data Engineering：用自然语言驱动数据开发全流程

## Current Implementation (2026-Q3)

当前 MVP 运行时为 **4 个 HTTP 微服务**（Gateway、Agent、SQLMesh、Ontology）。Workspace/Git/Files API 由 SQLMesh Service 提供；Platform Service、独立 Workspace Manager、gRPC 为远期规划。详见 [backend-architecture.md](backend-architecture.md)。

---

## 1. 设计目标

### 1.1 核心目标

| 目标 | 说明 |
|------|------|
| **AI 原生开发** | 自然语言描述需求 → AI 生成 SQLMesh 模型/审计/测试 |
| **Vibe Coding 体验** | 对话式开发，AI 理解血缘、Schema、业务语义，持续迭代 |
| **用户级隔离** | 每个用户独立的 Agent 上下文 + Git 工作区，互不干扰 |
| **多租户** | 租户间数据、计算、配置完全隔离 |
| **环境管理** | dev / staging / prod 多环境，Virtual Update 零停机发布 |
| **增量计算** | 仅计算变更影响的数据区间 |
| **生产调度** | Airflow 驱动的可靠定时调度 |
| **Agent Protocol** | 标准化的 Agent Client Protocol，前后端解耦 |

### 1.2 非功能性需求

- **可用性**：核心服务 SLA ≥ 99.9%
- **扩展性**：100+ 租户、10000+ 模型、1000+ 并发用户
- **安全性**：RBAC + 数据隔离 + 操作审计 + AI 操作分级
- **AI 响应**：模型生成 < 5s，智能补全 < 500ms，流式首 token < 1s

---

## 2. 整体架构

### 2.1 系统架构总览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         用户层 (AI-Native UI)                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ AI Chat Panel│  │ Smart Editor │  │Visual Lineage│  │Plan & Deploy │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │ Agent Client Protocol (WebSocket + REST)
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      API Gateway (Go - Chi)                                   │
│           认证 / 限流 / 租户路由 / WebSocket 代理 / 审计日志                  │
└──────────┬──────────────────────┬───────────────────────┬───────────────────┘
           │                      │                       │
           ▼                      ▼                       ▼
┌────────────────────┐ ┌────────────────────┐ ┌────────────────────────────┐
│  Platform Service  │ │  Agent Service     │ │  SQLMesh Service           │
│  (Go)              │ │  (Python)          │ │  (Python)                  │
│                    │ │                    │ │                            │
│ • 租户/用户管理    │ │ • openai-agents SDK│ │ • Plan / Apply / Run       │
│ • 项目管理        │ │ • Agent 编排       │ │ • Lineage / Audit / Test   │
│ • 环境管理        │ │ • 工具注册         │ │ • Schema Discovery         │
│ • 审批流          │ │ • 会话管理         │ │ • LSP (补全/诊断)          │
│ • 调度管理        │ │ • 上下文构建       │ │ • 配置动态生成             │
│ • 审计日志        │ │ • 流式响应         │ │                            │
└────────────────────┘ └────────────────────┘ └────────────────────────────┘
           │                      │                       │
           ▼                      ▼                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Workspace Manager (Go)                                  │
│          用户级 Git 工作区管理 / 文件系统隔离 / 生命周期管理                  │
└─────────────────────────────────────────────────────────────────────────────┘
           │                      │                       │
           ▼                      ▼                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        调度引擎 (Apache Airflow)                              │
│              DAG 动态生成 / sqlmesh run / 任务监控 / 告警                     │
└─────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           存储层                                               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐                     │
│  │PostgreSQL│ │  Redis   │ │ Git Repo │ │ 数据仓库  │                     │
│  │(元数据+  │ │(缓存+    │ │(Gitea/   │ │(DW/Lake) │                     │
│  │ State)   │ │ 会话)    │ │ GitLab)  │ │          │                     │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 服务职责划分

| 服务 | 语言 | 职责 | 通信方式 |
|------|------|------|---------|
| **API Gateway** | Go | 认证、限流、路由、WebSocket 代理 | HTTP/WS → 内部 gRPC |
| **Platform Service** | Go | 租户/用户/项目/环境/审批/调度管理 | gRPC + REST |
| **Agent Service** | Python | AI Agent 编排（openai-agents SDK）、工具执行 | gRPC + WebSocket |
| **SQLMesh Service** | Python | SQLMesh 核心操作封装 | gRPC |
| **Workspace Manager** | Go | 用户级 Git 工作区生命周期管理 | gRPC |
| **Airflow** | Python | 生产调度（sqlmesh run） | REST API |

---

## 3. Agent Service 设计（openai-agents-python SDK）

### 3.1 为什么选择 openai-agents-python SDK

| 对比维度 | openai-agents-python | Go 自研 | LangChain |
|---------|---------------------|---------|-----------|
| Agent 编排 | 原生支持 Handoff、Guardrails | 需自研 | 支持但抽象过重 |
| Tool Calling | 原生 Function Calling | 需封装 | 支持 |
| 流式响应 | 原生 Streaming | 需自研 | 部分支持 |
| 多 Agent 协作 | 原生 Handoff 机制 | 需自研 | 需额外框架 |
| 上下文管理 | RunContext 原生支持 | 需自研 | 需自研 |
| 可观测性 | 内置 Tracing | 需自研 | 需集成 |
| 维护成本 | OpenAI 官方维护 | 自行维护 | 社区维护 |

### 3.2 Agent 架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Agent Service (Python + openai-agents SDK)         │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │              Session Manager (会话管理器)                      │    │
│  │                                                               │    │
│  │  • 每个用户会话 → 独立的 Agent RunContext                     │    │
│  │  • 会话级上下文隔离（项目信息、Schema、血缘）                 │    │
│  │  • 会话生命周期管理（创建/恢复/归档）                         │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                              │                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │              Agent Registry (Agent 注册表)                    │    │
│  │                                                               │    │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐           │    │
│  │  │ Triage Agent│ │ Model Agent │ │ Debug Agent │           │    │
│  │  │ (意图路由)  │ │ (模型开发)  │ │ (问题诊断)  │           │    │
│  │  └─────────────┘ └─────────────┘ └─────────────┘           │    │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐           │    │
│  │  │Review Agent │ │ Deploy Agent│ │ Query Agent │           │    │
│  │  │ (代码审查)  │ │ (部署发布)  │ │ (数据查询)  │           │    │
│  │  └─────────────┘ └─────────────┘ └─────────────┘           │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                              │                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │              Tool Registry (工具注册表)                        │    │
│  │                                                               │    │
│  │  • schema_discovery    - 获取表结构                           │    │
│  │  • list_models         - 列出项目模型                         │    │
│  │  • get_lineage         - 获取血缘关系                         │    │
│  │  • create_file         - 创建/修改模型文件                    │    │
│  │  • preview_plan        - 预览 Plan 变更                       │    │
│  │  • apply_plan          - 应用 Plan                            │    │
│  │  • run_query           - 执行 SQL 查询                        │    │
│  │  • sample_data         - 数据采样                             │    │
│  │  • run_test            - 执行模型测试                         │    │
│  │  • git_commit          - 提交代码                             │    │
│  │  • git_diff            - 查看变更                             │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                              │                                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │              Context Builder (上下文构建器)                    │    │
│  │                                                               │    │
│  │  • 项目模型列表 + 血缘 DAG                                   │    │
│  │  • 数据源 Schema (表/列/类型/注释)                            │    │
│  │  • 用户工作区当前文件状态                                     │    │
│  │  • 业务术语表 (Glossary)                                      │    │
│  │  • 团队编码规范                                               │    │
│  │  • 历史对话摘要 (.ai/memory/ Markdown 文件)                    │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.3 Agent 协作模式（Handoff）

```mermaid
graph TD
    User[用户消息] --> Triage[Triage Agent<br/>意图识别与路由]
    Triage -->|创建/修改模型| Model[Model Agent]
    Triage -->|数据异常诊断| Debug[Debug Agent]
    Triage -->|代码审查| Review[Review Agent]
    Triage -->|部署发布| Deploy[Deploy Agent]
    Triage -->|数据查询/探索| Query[Query Agent]
    
    Model -->|需要审查| Review
    Model -->|需要部署| Deploy
    Debug -->|需要修改模型| Model
    Review -->|审查通过,部署| Deploy
```

**Handoff 机制**：openai-agents SDK 原生支持 Agent 间的 Handoff，Triage Agent 根据用户意图将对话转交给专业 Agent，专业 Agent 完成任务后可继续转交。

### 3.4 Agent 与用户隔离

每个用户的 Agent 运行在独立的 RunContext 中：

```
用户 A (tenant_1)                    用户 B (tenant_1)
┌──────────────────────┐            ┌──────────────────────┐
│ RunContext A          │            │ RunContext B          │
│                       │            │                       │
│ • workspace: /ws/t1/a│            │ • workspace: /ws/t1/b│
│ • git_branch: dev_a  │            │ • git_branch: dev_b  │
│ • session_history     │            │ • session_history     │
│ • project_context     │            │ • project_context     │
│ • user_preferences    │            │ • user_preferences    │
└──────────────────────┘            └──────────────────────┘
         │                                    │
         ▼                                    ▼
┌──────────────────────┐            ┌──────────────────────┐
│ Git Workspace A       │            │ Git Workspace B       │
│ /workspaces/t1/a/     │            │ /workspaces/t1/b/     │
│ (独立 clone + branch) │            │ (独立 clone + branch) │
└──────────────────────┘            └──────────────────────┘
```

---

## 4. Agent Client Protocol（ACP）

### 4.1 协议概述

Agent Client Protocol 定义了前端与 Agent Service 之间的标准化通信协议，确保：
- 前后端完全解耦
- 支持流式响应
- 支持 Agent 操作确认
- 支持多种 Artifact 类型
- 可扩展的事件机制

### 4.2 传输层

| 场景 | 协议 | 说明 |
|------|------|------|
| 对话交互 | WebSocket | 流式消息、双向通信 |
| 操作执行 | REST (POST) | 确认执行 Agent 建议的操作 |
| 会话管理 | REST (CRUD) | 创建/查询/删除会话 |
| 文件同步 | REST + WebSocket | 编辑器文件变更通知 |

### 4.3 消息类型定义

#### 4.3.1 客户端 → 服务端

| 消息类型 | 说明 |
|---------|------|
| `chat.message` | 用户发送对话消息 |
| `chat.cancel` | 取消当前 Agent 执行 |
| `action.confirm` | 确认执行 Agent 建议的操作 |
| `action.reject` | 拒绝 Agent 建议的操作 |
| `context.file_change` | 通知编辑器文件变更 |
| `context.file_open` | 通知当前打开的文件 |

#### 4.3.2 服务端 → 客户端

| 消息类型 | 说明 |
|---------|------|
| `agent.thinking` | Agent 正在思考（显示 loading） |
| `agent.text_delta` | 流式文本片段 |
| `agent.text_done` | 文本流结束 |
| `agent.tool_call_start` | 开始调用工具（显示工具名） |
| `agent.tool_call_result` | 工具调用结果 |
| `agent.artifact` | 生成的产物（代码文件等） |
| `agent.action_request` | 请求用户确认操作 |
| `agent.handoff` | Agent 切换通知 |
| `agent.error` | 错误信息 |
| `agent.done` | 本轮对话完成 |

### 4.4 核心消息结构

```yaml
# 客户端发送消息
ChatMessage:
  type: "chat.message"
  session_id: string          # 会话 ID
  content: string             # 用户消息文本
  attachments:                # 可选附件
    - type: "file" | "code" | "error_log" | "image"
      name: string
      content: string
      language: string        # 代码语言（可选）

# 服务端流式文本
TextDelta:
  type: "agent.text_delta"
  session_id: string
  delta: string               # 增量文本片段
  agent_name: string          # 当前响应的 Agent 名称

# 服务端生成产物
Artifact:
  type: "agent.artifact"
  session_id: string
  artifact:
    id: string                # 产物唯一 ID
    kind: "model_sql" | "test_yaml" | "audit_sql" | "config_yaml" | "doc_md"
    file_path: string         # 建议的文件路径
    content: string           # 文件内容
    language: string          # 语言标识
    action: "create" | "modify" | "delete"
    diff: string              # 修改时的 diff（可选）

# 服务端请求确认操作
ActionRequest:
  type: "agent.action_request"
  session_id: string
  action:
    id: string                # 操作唯一 ID
    kind: "apply_plan" | "git_commit" | "trigger_run" | "create_pr"
    label: string             # 显示给用户的按钮文本
    description: string       # 操作说明
    severity: "safe" | "caution" | "danger"  # 风险等级
    params: object            # 操作参数
    auto_expire_seconds: int  # 自动过期时间

# 客户端确认操作
ActionConfirm:
  type: "action.confirm"
  session_id: string
  action_id: string           # 对应 ActionRequest 的 id
```

### 4.5 协议交互时序

```
Client (前端)                    API Gateway                Agent Service
    │                               │                           │
    │── WS Connect ────────────────►│── Forward ───────────────►│
    │◄─ Connected ──────────────────│◄─────────────────────────│
    │                               │                           │
    │── chat.message ──────────────►│──────────────────────────►│
    │                               │                           │
    │◄─ agent.thinking ─────────────│◄──────────────────────────│
    │◄─ agent.tool_call_start ──────│◄──────────────────────────│ (Schema Discovery)
    │◄─ agent.tool_call_result ─────│◄──────────────────────────│
    │◄─ agent.text_delta ───────────│◄──────────────────────────│ (流式输出)
    │◄─ agent.text_delta ───────────│◄──────────────────────────│
    │◄─ agent.artifact ─────────────│◄──────────────────────────│ (生成的模型代码)
    │◄─ agent.action_request ───────│◄──────────────────────────│ (建议部署到 dev)
    │◄─ agent.done ─────────────────│◄──────────────────────────│
    │                               │                           │
    │── action.confirm ────────────►│──────────────────────────►│ (用户点击确认)
    │                               │                           │
    │◄─ agent.thinking ─────────────│◄──────────────────────────│
    │◄─ agent.tool_call_start ──────│◄──────────────────────────│ (Apply Plan)
    │◄─ agent.tool_call_result ─────│◄──────────────────────────│
    │◄─ agent.text_delta ───────────│◄──────────────────────────│ (部署结果)
    │◄─ agent.done ─────────────────│◄──────────────────────────│
```

### 4.6 Protocol 扩展点

| 扩展 | 说明 |
|------|------|
| 自定义 Artifact Kind | 支持注册新的产物类型（如 Dashboard JSON） |
| 自定义 Action Kind | 支持注册新的操作类型 |
| 进度事件 | 长时间操作的进度上报 |
| 协作事件 | 多人协作时的冲突通知 |

---

## 5. 用户级工作区隔离设计

### 5.1 隔离需求

在多租户多用户场景下，必须确保：

| 隔离维度 | 说明 |
|---------|------|
| **Git 工作区** | 每个用户独立的本地 clone + 独立分支，互不干扰 |
| **Agent 上下文** | 每个用户的对话历史、偏好、当前编辑状态独立 |
| **SQLMesh 环境** | 每个用户的 dev 环境独立（`dev_{user_id}`） |
| **文件系统** | 用户只能访问自己工作区内的文件 |

### 5.2 Workspace Manager 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                   Workspace Manager (Go Service)                  │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Workspace Pool (工作区池)                     │    │
│  │                                                           │    │
│  │  • 按需创建用户工作区（Lazy Init）                        │    │
│  │  • 空闲超时回收（可配置 TTL）                             │    │
│  │  • 最大并发工作区数限制                                   │    │
│  │  • 工作区状态持久化                                       │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │                                    │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Git Manager (Git 操作管理)                    │    │
│  │                                                           │    │
│  │  • Clone / Pull / Push / Branch / Merge                   │    │
│  │  • 用户分支策略: dev/{user_id}/{feature}                  │    │
│  │  • 冲突检测与通知                                         │    │
│  │  • 自动 rebase 主分支变更                                 │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │                                    │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Lifecycle Manager (生命周期管理)              │    │
│  │                                                           │    │
│  │  • 创建: clone repo → checkout branch → 初始化配置        │    │
│  │  • 激活: 用户登录时恢复工作区状态                         │    │
│  │  • 休眠: 空闲超时后释放资源（保留 Git 状态）             │    │
│  │  • 销毁: 用户离职/项目删除时清理                          │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### 5.3 工作区目录结构

```
/data/workspaces/
├── {tenant_id}/
│   ├── {user_id}/
│   │   ├── {project_id}/              # 用户的项目工作区
│   │   │   ├── .git/                  # 独立的 Git clone
│   │   │   ├── config.yaml            # 动态生成的 SQLMesh 配置
│   │   │   ├── models/
│   │   │   ├── audits/
│   │   │   ├── tests/
│   │   │   ├── .ai/                   # AI Agent 上下文目录
│   │   │   │   ├── memory/            # 对话记忆（Markdown）
│   │   │   │   │   ├── session_001.md # 会话摘要
│   │   │   │   │   ├── session_002.md
│   │   │   │   │   └── index.md       # 记忆索引（关键决策/偏好）
│   │   │   │   ├── context/           # 上下文快照
│   │   │   │   │   ├── project_summary.md  # 项目概要
│   │   │   │   │   ├── conventions.md      # 编码规范
│   │   │   │   │   └── glossary.md         # 业务术语
│   │   │   │   └── .agent_config.yaml # Agent 偏好配置
│   │   │   └── .workspace.json        # 工作区元数据
│   │   └── {project_id_2}/
│   └── {user_id_2}/
└── {tenant_id_2}/
```

### 5.4 工作区生命周期

```
┌──────┐     ┌──────┐     ┌──────┐     ┌──────┐     ┌──────┐
│ 未创建│────►│ 初始化│────►│ 活跃  │────►│ 休眠  │────►│ 已销毁│
└──────┘     └──────┘     └──────┘     └──────┘     └──────┘
                               │              │
                               │◄─────────────┘
                               │   (用户重新访问时唤醒)
```

| 状态 | 触发条件 | 资源占用 | 说明 |
|------|---------|---------|------|
| 未创建 | 用户首次访问项目 | 无 | - |
| 初始化 | 首次访问触发 | 磁盘 (clone) | git clone + checkout branch |
| 活跃 | 用户正在使用 | 磁盘 + 内存 | Agent 上下文加载在内存中 |
| 休眠 | 空闲 > 30min | 仅磁盘 | 释放内存，保留 Git 状态 |
| 已销毁 | 手动清理 / TTL 过期 | 无 | 删除本地文件（远程分支保留） |

### 5.5 Git 分支策略

```
main (生产)
├── staging (预发布)
├── dev/user_alice/feature_user_activity    ← 用户 Alice 的开发分支
├── dev/user_bob/fix_order_model            ← 用户 Bob 的开发分支
└── dev/user_charlie/add_payment_metrics    ← 用户 Charlie 的开发分支
```

**规则**：
- 每个用户在自己的 `dev/{user_id}/` 命名空间下创建分支
- AI Agent 生成的代码直接提交到用户分支
- 发布到 staging/prod 需要通过 PR + 审批
- 主分支变更自动通知相关用户 rebase

### 5.6 AI 上下文记忆方案（Markdown 文件）

**设计原则**：不引入向量数据库，所有 Agent 上下文记忆以 Markdown 文件形式存储在用户工作区的 `.ai/` 目录中，随代码一起版本管理。

#### 目录结构

```
.ai/
├── memory/                        # 对话记忆
│   ├── index.md                   # 记忆索引（关键决策、用户偏好摘要）
│   ├── session_20240601_001.md    # 会话摘要（按日期+序号）
│   ├── session_20240601_002.md
│   └── ...
├── context/                       # 项目上下文快照
│   ├── project_summary.md         # 项目概要（模型列表、分层、依赖）
│   ├── conventions.md             # 编码规范（命名、分层、最佳实践）
│   └── glossary.md                # 业务术语表
└── .agent_config.yaml             # Agent 偏好配置（模型、温度等）
```

#### 记忆文件格式

**index.md**（记忆索引 — Agent 每次启动时加载）：
```markdown
# Agent Memory Index

## 用户偏好
- 偏好增量模型，避免全量重跑
- 命名风格：{layer}_{domain}_{entity}
- 时间列统一用 event_date

## 关键决策
- 2024-06-01: 决定 user_events 按天分区
- 2024-06-03: GMV 口径确认为已支付订单金额（不含退款）

## 常用模型
- raw.user_events → staging.user_events_clean → analytics.user_daily_activity
```

**session_xxx.md**（会话摘要 — 对话结束时由 Agent 自动生成）：
```markdown
# Session 2024-06-01 #001

## 摘要
用户创建了用户活跃度模型 analytics.user_daily_activity

## 关键操作
- 创建模型: models/analytics/user_daily_activity.sql
- 创建测试: tests/test_user_daily_activity.yaml
- 部署到 dev 环境成功

## 上下文
- 基于 raw.user_events 表
- 按天聚合，INCREMENTAL_BY_TIME_RANGE
- grain: user_id + event_date
```

#### 上下文压缩策略

| 策略 | 触发条件 | 操作 |
|------|---------|------|
| **会话摘要** | 每次对话结束 | Agent 生成当前会话摘要写入 session_xxx.md |
| **索引更新** | 每 5 次会话 | Agent 重新整理 index.md（合并重复、删除过时） |
| **历史裁剪** | memory/ 文件 > 50 个 | 保留最近 20 个 + index.md 中的关键信息 |
| **上下文窗口** | 每次对话开始 | 加载 index.md + 最近 3 个 session + project_summary.md |

#### 优势

- **无额外基础设施**：不需要向量数据库，降低运维复杂度
- **版本可追溯**：记忆文件随 Git 管理，可回溯任意时间点的上下文
- **可人工编辑**：用户可以直接修改 .ai/ 下的文件来纠正 Agent 记忆
- **跨设备同步**：通过 Git push/pull 自动同步到其他设备
- **透明可审计**：所有 Agent 记忆对用户完全透明

---

## 6. 多租户隔离设计

### 6.1 隔离层次

```
┌─────────────────────────────────────────────────────────────┐
│                    租户隔离 (Tenant Isolation)                │
│                                                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 计算隔离                                               │  │
│  │ • SQLMesh Gateway: 每租户独立 connection 配置          │  │
│  │ • Airflow: 每租户独立 DAG + 资源池                     │  │
│  │ • Agent: 共享服务，RunContext 级别隔离                  │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 数据隔离                                               │  │
│  │ • 数据仓库: Schema 隔离（默认）/ 实例隔离（高安全）    │  │
│  │ • SQLMesh State: 独立 Schema                           │  │
│  │ • 平台元数据: tenant_id 字段逻辑隔离                   │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 代码隔离                                               │  │
│  │ • Git: 每租户独立仓库                                  │  │
│  │ • 工作区: /workspaces/{tenant_id}/{user_id}/           │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ AI 隔离                                                │  │
│  │ • Agent 上下文: 仅包含本租户的 Schema/模型/术语        │  │
│  │ • 对话历史: 按 tenant_id + user_id 隔离                │  │
│  │ • LLM 配置: 租户可自带 API Key                         │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 SQLMesh Gateway 动态配置

每个租户对应一个 SQLMesh Gateway 配置，由 Platform Service 动态生成并写入用户工作区：

```yaml
# 动态生成到 /workspaces/{tenant}/{user}/{project}/config.yaml
gateways:
  {tenant_id}:
    connection:
      type: postgres          # 租户的数据仓库类型
      host: tenant-db.rds.amazonaws.com
      database: warehouse
      schema_: "{tenant_schema}"
    state_connection:
      type: postgres
      host: state-db.internal
      schema_: "state_{tenant_id}"

default_gateway: "{tenant_id}"

model_defaults:
  dialect: postgres
  start: "2024-01-01"
  cron: "@daily"
```

---

## 7. Git 仓库授权与绑定设计

### 7.1 需求概述

平台需要支持用户绑定自己的 Git 仓库（GitHub / GitLab / Gitea / 自建 Git），使 AI 生成的代码能够推送到用户授权的仓库中。支持以下授权方式：

| 授权方式 | 适用场景 | 安全等级 |
|---------|---------|---------|
| **OAuth 2.0** | GitHub / GitLab / Gitea | ⭐⭐⭐ 高（推荐） |
| **Personal Access Token** | 所有 Git 服务（含自建） | ⭐⭐ 中 |
| **SSH Key** | 所有 Git 服务 | ⭐⭐⭐ 高 |

### 7.2 整体架构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         前端 (Vue + TypeScript)                              │
│  ┌────────────────────┐    ┌────────────────────┐                     │
│  │ Git 授权页面       │    │ 项目创建/绑定页面    │                     │
│  │ • OAuth 授权       │    │ • 选择授权账号       │                     │
│  │ • Token 配置       │    │ • 选择仓库           │                     │
│  │ • SSH Key 上传     │    │ • 配置分支策略       │                     │
│  └────────────────────┘    └────────────────────┘                     │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │ REST API
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      Platform Service (Go)                                   │
│  ┌─────────────────────────────────────────────────────────────────┐       │
│  │              Git Credential Manager (Git 凭证管理)                 │       │
│  │                                                                   │       │
│  │  • OAuth 授权流程（GitHub / GitLab / Gitea）                    │       │
│  │  • Token 加密存储（AES-256）                                    │       │
│  │  • SSH Key 生成与管理                                           │       │
│  │  • 凭证刷新（OAuth Token 过期刷新）                             │       │
│  │  • 仓库访问权限验证                                             │       │
│  └─────────────────────────────────────────────────────────────────┘       │
│                              │                                            │
│  ┌─────────────────────────────────────────────────────────────────┐       │
│  │              Git Integration Service (Git 集成服务)               │       │
│  │                                                                   │       │
│  │  • 仓库列表获取（通过 OAuth/Token）                             │       │
│  │  • 仓库 Webhook 配置（推送事件 → 触发 Plan）                    │       │
│  │  • 分支策略管理                                                 │       │
│  │  • 仓库健康检查                                                 │       │
│  └─────────────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Workspace Manager (Go)                                  │
│  • 使用用户凭证 clone/pull/push 仓库                                        │
│  • 凭证通过环境变量/SSH Agent 注入                                          │
│  • 每个用户独立凭证，互不干扰                                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 7.3 数据模型设计

#### 7.3.1 Git 授权凭证表（`git_credentials`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `credential_id` | UUID | 主键 |
| `tenant_id` | UUID | 所属租户 |
| `user_id` | UUID | 所属用户（NULL = 租户级共享凭证） |
| `provider` | ENUM | `github` / `gitlab` / `gitea` / `custom` |
| `auth_type` | ENUM | `oauth` / `token` / `ssh` |
| `name` | VARCHAR(255) | 用户自定义的凭证名称 |
| `oauth_access_token` | ENCRYPTED | OAuth Access Token（加密存储） |
| `oauth_refresh_token` | ENCRYPTED | OAuth Refresh Token（加密存储） |
| `oauth_expires_at` | TIMESTAMP | OAuth Token 过期时间 |
| `pat_token` | ENCRYPTED | Personal Access Token（加密存储） |
| `ssh_private_key` | ENCRYPTED | SSH 私钥（加密存储） |
| `ssh_public_key` | TEXT | SSH 公钥 |
| `git_host` | VARCHAR(500) | Git 服务地址（自定义时填写） |
| `git_username` | VARCHAR(255) | Git 用户名 |
| `is_active` | BOOLEAN | 是否启用 |
| `last_verified_at` | TIMESTAMP | 最后一次验证通过时间 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

**唯一约束**：`(tenant_id, user_id, provider, auth_type, name)`

#### 7.3.2 项目仓库绑定表（`project_repositories`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `binding_id` | UUID | 主键 |
| `tenant_id` | UUID | 所属租户 |
| `project_id` | UUID | 关联项目 |
| `credential_id` | UUID | 关联凭证 |
| `repo_url` | VARCHAR(500) | 仓库 URL |
| `repo_owner` | VARCHAR(255) | 仓库所有者 |
| `repo_name` | VARCHAR(255) | 仓库名称 |
| `default_branch` | VARCHAR(255) | 默认分支（如 `main`） |
| `branch_strategy` | ENUM | `direct` / `fork` / `branch_per_user` |
| `fork_repo_url` | VARCHAR(500) | Fork 仓库 URL（Fork 模式） |
| `webhook_id` | VARCHAR(255) | Webhook ID（用于清理） |
| `webhook_secret` | ENCRYPTED | Webhook Secret |
| `sync_status` | ENUM | `active` / `error` / `syncing` |
| `last_sync_at` | TIMESTAMP | 最后一次同步时间 |
| `created_by` | UUID | 创建人 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

### 7.4 OAuth 2.0 授权流程

#### 7.4.1 授权时序图

```
┌──────┐   1. 点击"授权 GitHub"    ┌──────────────┐
│ 前端  │ ────────────────────────► │ Platform API  │
└──────┘                            └──────────────┘
                                       │
                                       │ 2. 生成 state，重定向到 OAuth 授权页
                                       ▼
                                ┌──────────────┐
                                │ GitHub/GitLab │
                                │  OAuth 授权页  │
                                └──────────────┘
                                       │
                                       │ 3. 用户授权，回调
                                       ▼
┌──────┐   4. 携带 code+state 回调   ┌──────────────┐
│ 前端  │ ◄────────────────────────  │ Platform API  │
└──────┘                              └──────────────┘
                                           │
                                           │ 5. 用 code 换取 access_token
                                           ▼
                                    ┌──────────────┐
                                    │ GitHub/GitLab │
                                    │   Token 接口   │
                                    └──────────────┘
                                           │
                                           │ 6. 返回 access_token + refresh_token
                                           ▼
                                    ┌──────────────┐
                                    │ Platform API  │
                                    │ • 加密存储 token                            │
                                    │ • 获取用户信息和仓库列表                    │
                                    └──────────────┘
```

#### 7.4.2 OAuth 配置参考

| 配置项 | GitHub | GitLab | Gitea（自建） |
|-------|--------|--------|--------------|
| **Authorization URL** | `https://github.com/login/oauth/authorize` | `https://gitlab.com/oauth/authorize` | `{GITEA_URL}/login/oauth/authorize` |
| **Token URL** | `https://github.com/login/oauth/access_token` | `https://gitlab.com/oauth/token` | `{GITEA_URL}/login/oauth/access_token` |
| **User API** | `https://api.github.com/user` | `https://gitlab.com/api/v4/user` | `{GITEA_URL}/api/v1/user` |
| **Repo List API** | `https://api.github.com/user/repos` | `https://gitlab.com/api/v4/projects` | `{GITEA_URL}/api/v1/user/repos` |
| **Scopes** | `repo`, `read:user` | `api`, `read_user` | `repo`, `read:user` |

#### 7.4.3 Token 刷新机制

```
1. 每次调用 Git 操作前，检查 OAuth token 是否即将过期
   （expires_at - now < 5min → 触发刷新）

2. 使用 refresh_token 换取新的 access_token

3. 更新数据库中的 token 和 expires_at

4. 如果 refresh_token 也过期 → 通知用户重新授权

5. 后台定时任务：每小时检查所有 OAuth token 的过期状态
```

### 7.5 Personal Access Token 方式

适用于：自建 Git 服务、企业内网 GitLab、或对 OAuth 有顾虑的用户。

#### 前端类型定义（TypeScript）

```typescript
// types/git.ts
export interface TokenCredentialForm {
  provider: 'github' | 'gitlab' | 'gitea' | 'custom'
  name: string                    // 凭证名称
  token: string                   // Personal Access Token
  gitHost?: string                // 自定义 Git 服务地址
  username?: string               // Git 用户名（可选）
}

// Token 权限要求提示
export const TOKEN_SCOPE_REQUIREMENTS = {
  github: ['repo', 'read:user'],
  gitlab: ['api', 'read_repository'],
  gitea: ['repo', 'read:user']
}
```

#### 验证流程

```
1. 用户提交 Token → Platform API
2. Platform API 用 Token 调用 Git API 验证：
   - GitHub: GET /user + GET /repos (验证 repo 权限)
   - GitLab: GET /user + GET /projects (验证 api 权限)
   - Gitea: GET /user + GET /user/repos
3. 验证通过 → 加密存储 Token（AES-256）
4. 返回凭证 ID，供项目绑定时使用
```

### 7.6 SSH Key 方式

适用于：需要更高安全性的场景，或企业防火墙只允许 SSH 的情况。

#### SSH Key 管理流程

```
[模式 A] 平台生成 Key Pair（推荐）
1. Platform API 生成 Ed25519 密钥对
2. 返回公钥内容给用户
3. 用户将公钥添加到 Git 服务（GitHub Deploy Key / GitLab SSH Key）
4. 用户确认添加完成后，点击"验证连接"
5. Platform API 测试 SSH 连接（ssh -T git@host）
6. 验证通过 → 加密存储私钥

[模式 B] 用户上传自己的 Key Pair
1. 用户在本地生成密钥对
2. 用户将公钥添加到 Git 服务
3. 用户在前端上传私钥文件（或粘贴私钥内容）
4. Platform API 加密存储私钥
5. 验证 SSH 连接
```

#### 密钥存储安全

| 安全措施 | 说明 |
|---------|------|
| **加密算法** | Ed25519（推荐）或 RSA 4096 |
| **传输加密** | 前端上传私钥时，使用平台公钥加密后传输（RSA-OAEP） |
| **存储加密** | AES-256-GCM 加密后存入数据库 |
| **密钥隔离** | 每个用户的密钥独立加密，密钥派生使用 user_salt |
| **访问控制** | 私钥只能通过 Workspace Manager 注入到 Git 操作，不允许 API 读取 |

### 7.7 项目绑定仓库流程

```
1. 用户创建项目
   ├── 输入项目名称、描述
   ├── 选择 Git 凭证（从已授权的凭证列表中选择）
   ├── 选择仓库（下拉列表，从 Git API 获取）
   └── 配置分支策略

2. 平台验证权限
   ├── 用凭证测试 clone 权限（git ls-remote）
   └── 验证用户对仓库有 write 权限

3. 配置 Webhook（可选）
   ├── 平台生成 webhook_secret
   ├── 调用 Git API 创建 webhook
   └── 监听 push / pull_request 事件

4. 初始化工作区
   ├── Workspace Manager 用用户凭证 clone 仓库
   ├── 创建用户分支（dev/{user_id}）
   └── 初始化 .ai/ 目录结构

5. 绑定完成，用户可开始使用
```

#### 分支策略配置

| 策略 | 说明 | 适用场景 |
|-----|------|---------|
| `direct` | 直接在默认分支上开发（不推荐） | 个人项目 |
| `fork` | Fork 仓库后开发，通过 PR 合并 | 开源项目 / 跨团队协作 |
| `branch_per_user` | 每个用户创建独立分支 `dev/{user_id}/*` | **推荐**，平台默认策略 |

### 7.8 Workspace Manager 凭证注入

当用户进行操作（clone / pull / push）时，Workspace Manager 需要将用户凭证注入到 Git 操作中：

| 授权方式 | 凭证注入方式 |
|---------|-------------|
| **OAuth Token** | Git URL: `https://oauth2:{token}@github.com/owner/repo` 或配置 git credential helper |
| **Personal Access Token** | Git URL: `https://{username}:{token}@gitlab.com/...` 或写入 `~/.git-credentials` |
| **SSH Key** | 将私钥写入临时文件（权限 600），设置 `GIT_SSH_COMMAND="ssh -i /path/to/key"` 或使用 SSH Agent（`ssh-add`） |

**安全注意事项**：
- 私钥文件仅对当前用户工作区可见（文件系统权限隔离）
- 每次 Git 操作完成后，清理环境变量中的凭证信息
- 不允许将凭证信息写入日志

### 7.9 Webhook 与同步

| Webhook 事件 | 处理逻辑 |
|-------------|---------|
| `push`（默认分支） | 通知相关用户 rebase 自己的分支 |
| `pull_request.opened` | 触发 SQLMesh 自动验证（Plan Preview） |
| `pull_request.merged` | 触发 staging/prod 部署流程 |
| `push`（用户分支） | 更新用户工作区（如果在线） |

### 7.10 安全与合规

| 风险 | 缓解措施 |
|-----|---------|
| Token 泄露 | AES-256 加密存储 + 传输加密 + 定期轮换 |
| SSH 私钥泄露 | 加密存储 + 临时文件权限控制 + 操作后立即清理 |
| 仓库越权访问 | 每次操作前验证用户对项目仓库的访问权限 |
| Webhook 伪造 | 验证 webhook_secret 签名 |
| OAuth CSRF | state 参数 + PKCE（如果有移动端） |
| 凭证共享风险 | 支持 user-level 凭证（非租户共享），确保隔离 |

---

## 8. Platform Service 设计 (Go)

### 8.1 模块划分

```
Platform Service (Go)
├── tenant/          # 租户管理
│   ├── CRUD 租户
│   ├── 租户配置（DB连接、LLM配置、资源配额）
│   └── 租户状态管理
├── user/            # 用户管理
│   ├── CRUD 用户
│   ├── 角色分配（admin / developer / viewer）
│   └── 用户偏好
├── project/         # 项目管理
│   ├── CRUD 项目
│   ├── 项目配置（Git仓库、Gateway、Dialect）
│   └── 项目成员管理
├── environment/     # 环境管理
│   ├── 环境列表（dev_{user}/staging/prod）
│   ├── 环境 TTL 管理
│   └── 环境清理
├── plan/            # Plan 管理
│   ├── Plan 创建/查询
│   ├── Plan 审批流
│   └── Plan 执行记录
├── schedule/        # 调度管理
│   ├── Airflow DAG 生成
│   ├── 调度状态查询
│   ├── 手动触发/暂停/恢复
│   └── 调度回调处理
├── approval/        # 审批流
│   ├── 审批规则配置
│   ├── 审批单管理
│   └── 通知推送
├── audit/           # 审计日志
│   ├── 操作记录
│   └── 查询/导出
└── glossary/        # 业务术语表
    ├── CRUD 术语
    └── 术语搜索
```

### 8.2 API 路由概览

```
/api/v1/
├── /auth
│   ├── POST   /login
│   ├── POST   /refresh
│   └── POST   /logout
├── /tenants                    (Admin)
│   ├── GET    /
│   ├── POST   /
│   ├── GET    /{id}
│   ├── PUT    /{id}
│   └── DELETE /{id}
├── /projects
│   ├── GET    /
│   ├── POST   /
│   ├── GET    /{id}
│   ├── PUT    /{id}
│   ├── DELETE /{id}
│   └── GET    /{id}/members
├── /projects/{id}/environments
│   ├── GET    /
│   └── DELETE /{name}
├── /projects/{id}/plans
│   ├── POST   /                (创建 Plan)
│   ├── GET    /{planId}
│   ├── POST   /{planId}/apply
│   ├── POST   /{planId}/approve
│   └── POST   /{planId}/reject
├── /projects/{id}/schedules
│   ├── GET    /
│   ├── PUT    /
│   ├── POST   /trigger
│   ├── POST   /pause
│   └── GET    /runs
├── /chat                       (Agent Client Protocol)
│   ├── WS     /stream          (WebSocket 对话)
│   ├── GET    /sessions
│   ├── GET    /sessions/{id}
│   └── DELETE /sessions/{id}
├── /actions
│   └── POST   /execute         (执行 Agent 建议的操作)
├── /glossary
│   ├── GET    /
│   ├── POST   /
│   ├── PUT    /{id}
│   └── DELETE /{id}
└── /audit-logs
    └── GET    /
```

---

## 9. SQLMesh Service 设计 (Python)

### 9.1 职责

SQLMesh Service 是对 SQLMesh Python API 的 gRPC 封装，提供以下能力：

| 接口 | 说明 |
|------|------|
| `CreatePlan` | 创建 Plan，返回变更摘要 |
| `ApplyPlan` | 应用 Plan（执行 Backfill / Virtual Update） |
| `Run` | 日常增量运行 |
| `GetLineage` | 获取模型血缘关系 |
| `GetModelInfo` | 获取模型详情（Schema、配置、SQL） |
| `ListModels` | 列出项目所有模型 |
| `RunAudit` | 执行数据审计 |
| `RunTest` | 执行模型测试 |
| `DiscoverSchema` | 获取数据源表结构 |
| `ExecuteQuery` | 执行 SQL 查询（带限制） |
| `ValidateModel` | 验证模型 SQL 语法和语义 |
| `FormatSQL` | 格式化 SQL |
| `RenderModel` | 渲染模型（展开宏/引用） |

### 9.2 关键设计

- **工作区感知**：每次调用需传入 `workspace_path`，指向用户的独立工作区
- **进程隔离**：每次操作在独立的 SQLMesh Context 中执行，避免状态污染
- **超时控制**：Plan/Apply 操作设置合理超时（默认 10min）
- **并发限制**：同一工作区同时只允许一个写操作（Plan/Apply/Run）

---

## 10. Airflow 调度集成

### 10.1 职责划分

| 操作 | 触发方式 | 执行者 | 场景 |
|------|---------|--------|------|
| `sqlmesh plan + apply` | AI Agent / 用户手动 | Agent Service → SQLMesh Service | 模型变更部署 |
| `sqlmesh run` | Airflow 定时调度 | Airflow Worker | 日常增量运行 |

### 10.2 DAG 管理

- Platform Service 负责生成/更新/删除 Airflow DAG 文件
- 每个项目对应一个 DAG：`sqlmesh_{tenant_id}_{project_id}`
- DAG 内容：调用 SQLMesh CLI 执行 `sqlmesh run`
- 通过 Airflow REST API 管理 DAG 状态（暂停/恢复/触发）
- DAG 执行完成后通过回调通知 Platform Service

### 10.3 调度监控

- Airflow Webserver 作为调度监控 UI（可选嵌入平台）
- Platform Service 定期同步 DAG Run 状态
- 失败时通过 Platform Service 推送告警通知

---

## 11. 技术选型

| 层级 | 技术 | 版本 | 说明 |
|------|------|------|------|
| **前端** | Vue 3 + TypeScript | 3.4+ | Composition API + Vite |
| **SQL 编辑器** | Monaco Editor | latest | 集成 SQLMesh LSP |
| **AI 对话 UI** | 自研 (WebSocket + Vue) | - | 流式渲染，基于 Agent Client Protocol |
| **后端 (Platform)** | Go | 1.22+ | Chi + GORM + Zap |
| **后端 (Agent)** | Python | 3.11+ | openai-agents SDK |
| **后端 (SQLMesh)** | Python | 3.11+ | FastAPI / gRPC |
| **后端 (Workspace)** | Go | 1.22+ | go-git + 文件系统管理 |
| **服务通信** | gRPC + Protobuf | - | 内部服务间通信 |
| **Agent Protocol** | WebSocket + JSON | - | 前端 ↔ Agent 通信 |
| **LLM** | OpenAI / Anthropic / DeepSeek | - | 通过 openai-agents SDK 统一接入 |
| **上下文记忆** | Markdown 文件 (工作区内) | - | .ai/memory/ 目录，随代码版本管理 |
| **调度引擎** | Apache Airflow | 2.8+ | 生产调度 |
| **元数据库** | PostgreSQL | 16+ | 平台元数据 + SQLMesh State |
| **缓存** | Redis | 7+ | 会话缓存、限流、分布式锁 |
| **版本管理** | Gitea | latest | 模型代码版本化 |
| **数据仓库** | PostgreSQL / BigQuery / Snowflake | - | 实际数据存储 |
| **监控** | Prometheus + Grafana | - | 指标 + 可视化 |
| **日志** | Zap (Go) + structlog (Python) + Loki | - | 结构化日志 |
| **容器化** | Kubernetes + Docker | - | 部署与弹性伸缩 |

---

## 12. 部署架构

### 12.1 Kubernetes 部署拓扑

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Kubernetes Cluster                                │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Namespace: dataops-platform                                  │    │
│  │                                                               │    │
│  │  ┌────────────┐ ┌────────────┐ ┌────────────┐               │    │
│  │  │ Go API GW  │ │ Go Platform│ │ Go Workspace│              │    │
│  │  │ (x3, HPA)  │ │ (x3, HPA) │ │ Manager(x2)│              │    │
│  │  └────────────┘ └────────────┘ └────────────┘               │    │
│  │                                                               │    │
│  │  ┌────────────┐ ┌────────────┐                               │    │
│  │  │ Py Agent   │ │ Py SQLMesh │                               │    │
│  │  │ (x3, HPA)  │ │ (x3, HPA) │                               │    │
│  │  └────────────┘ └────────────┘                               │    │
│  │                                                               │    │
│  │  ┌────────────┐ ┌────────────┐                               │    │
│  │  │ Redis (HA) │ │ Gitea      │                               │    │
│  │  └────────────┘ └────────────┘                               │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Namespace: dataops-airflow                                   │    │
│  │                                                               │    │
│  │  ┌────────────┐ ┌────────────┐ ┌──────────────────────┐     │    │
│  │  │ Webserver  │ │ Scheduler  │ │ Worker (HPA 2~20)    │     │    │
│  │  │ (x2)       │ │ (x2 HA)   │ │                      │     │    │
│  │  └────────────┘ └────────────┘ └──────────────────────┘     │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Namespace: monitoring                                        │    │
│  │  ┌────────────┐ ┌────────────┐ ┌────────────┐               │    │
│  │  │ Prometheus │ │ Grafana    │ │ Loki       │               │    │
│  │  └────────────┘ └────────────┘ └────────────┘               │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  PersistentVolume: workspace-storage                          │    │
│  │  (NFS / EFS / Ceph — 存储用户 Git 工作区)                    │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                        外部依赖                                       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐              │
│  │PostgreSQL│ │ LLM API  │ │ 数据仓库  │ │ Object   │              │
│  │ (RDS HA) │ │(OpenAI/..)│ │(BQ/SF/PG)│ │ Storage  │              │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘              │
└─────────────────────────────────────────────────────────────────────┘
```

### 12.2 工作区存储策略

| 方案 | 适用场景 | 优点 | 缺点 |
|------|---------|------|------|
| **NFS/EFS** | 中小规模 (< 100 用户) | 简单、多 Pod 共享 | 性能一般 |
| **本地 SSD + 亲和性** | 高性能需求 | IO 快 | Pod 迁移困难 |
| **Ceph RBD** | 大规模 | 高性能、可扩展 | 运维复杂 |
| **对象存储 + 本地缓存** | 超大规模 | 无限扩展 | 冷启动慢 |

**推荐**：中小规模用 EFS/NFS，大规模用 Ceph + 本地缓存。

### 12.3 扩缩容策略

| 服务 | 扩缩容依据 | 最小/最大实例 |
|------|-----------|-------------|
| API Gateway | CPU + 连接数 | 2 / 10 |
| Platform Service | CPU + QPS | 2 / 8 |
| Agent Service | 并发会话数 + GPU 利用率 | 2 / 10 |
| SQLMesh Service | 并发 Plan/Run 数 | 2 / 8 |
| Workspace Manager | 活跃工作区数 | 1 / 4 |
| Airflow Worker | 队列深度 | 2 / 20 |

---

## 13. 安全与治理

### 13.1 RBAC 权限模型

| 角色 | 权限 |
|------|------|
| **Platform Admin** | 管理所有租户、全局配置 |
| **Tenant Admin** | 管理本租户用户、项目、配置 |
| **Developer** | 开发模型、Plan dev、提交 PR |
| **Reviewer** | 审批 Plan、Code Review |
| **Viewer** | 只读查看模型、血缘、数据 |

### 13.2 AI 操作分级

| 级别 | 操作类型 | 执行方式 | 示例 |
|------|---------|---------|------|
| L1 只读 | 查询/分析 | Agent 自动执行 | Schema 查看、血缘分析、数据采样 |
| L2 开发 | 代码生成 | Agent 自动执行 | 生成模型、修改 SQL、创建测试 |
| L3 提交 | 代码提交 | 用户确认后执行 | git commit、git push |
| L4 部署(dev) | 开发环境变更 | 用户确认后执行 | Plan dev、Apply dev |
| L5 部署(prod) | 生产变更 | 审批后执行 | Plan prod、Apply prod |
| L6 管理 | 系统配置 | 禁止 AI 执行 | 删除项目、修改权限 |

### 13.3 AI 生成代码安全检查

Agent 生成的代码在写入文件前，自动执行以下检查：
- **SQL 安全**：禁止 DROP/TRUNCATE/无条件 DELETE
- **模型规范**：必须有 grain、description、合规命名
- **性能规范**：禁止 SELECT *、增量模型必须有分区过滤
- **SQLMesh 验证**：调用 `ValidateModel` 确保语法正确

---

## 14. 关键设计决策

| # | 决策 | 原因 | 替代方案 |
|---|------|------|---------|
| 1 | **openai-agents-python SDK** | 原生 Handoff/Guardrails/Tracing，Agent 编排能力强 | Go 自研 (工作量大) / LangChain (抽象重) |
| 2 | **Agent Service 用 Python** | openai-agents SDK 是 Python 库，且 SQLMesh 也是 Python | Go 调 Python (复杂) |
| 3 | **Platform/Gateway 用 Go** | 高并发 API 服务、WebSocket 代理、文件系统操作 | Python (性能差) / Java (重) |
| 4 | **Vue 3 + TypeScript 前端** | 轻量、Composition API 灵活、Vite 构建快 | Next.js (SSR 对 AI 对话场景非必要) / React (生态相当) |
| 5 | **Markdown 文件存储 Agent 记忆** | 无额外基础设施、随代码版本管理、用户可编辑、透明可审计 | 向量数据库 (运维复杂、不透明) / Redis (无持久化语义) |
| 6 | **用户级 Git 工作区隔离** | 避免多用户同时编辑冲突，Agent 操作不影响其他人 | 共享工作区 + 锁 (体验差) |
| 7 | **Agent Client Protocol** | 标准化前后端通信，支持流式/操作确认/多 Artifact | 自定义 REST (不支持流式) |
| 8 | **gRPC 内部通信** | 强类型、高性能、流式支持 | REST (松散) / 消息队列 (复杂) |
| 9 | **Workspace Manager 独立服务** | 工作区生命周期管理复杂，需独立扩缩容 | 嵌入 Platform Service (耦合) |
| 10 | **Schema 级别租户隔离** | 平衡安全性与成本 | 实例隔离 (贵) / 逻辑隔离 (不安全) |
| 11 | **Airflow 仅负责 sqlmesh run** | Plan/Apply 是交互式操作，不适合定时调度 | Airflow 管全部 (复杂) |
| 12 | **PV 存储用户工作区** | Git 工作区需要持久化，Pod 重启不丢失 | 每次重新 clone (慢) |

---

## 15. 分阶段实施计划

### Phase 1: AI MVP (6~8 周)

**目标**：单租户单用户，AI 对话式创建模型 + 部署到 dev

- [ ] 基础设施搭建（K8s + PostgreSQL + Redis + Gitea）
- [ ] Go API Gateway 骨架（认证 + 路由 + WebSocket 代理）
- [ ] Go Platform Service 骨架（项目 CRUD + 用户管理）
- [ ] Python Agent Service（openai-agents SDK + Triage/Model Agent）
- [ ] Python SQLMesh Service（Plan/Apply/Lineage/Schema）
- [ ] Agent Client Protocol 实现（WebSocket 流式对话）
- [ ] Agent 工具：schema_discovery / create_file / preview_plan / apply_plan
- [ ] 基础前端（Vue 3 + Monaco Editor + AI Chat Panel）
- [ ] 单用户 Git 工作区管理
### Phase 2: 多用户隔离 + 完整 AI (8~10 周)

**目标**：多用户隔离，AI 全流程覆盖

- [ ] Workspace Manager 服务（用户级工作区生命周期）
- [ ] Git 分支策略实现（用户分支 + PR 流程）
- [ ] Agent 上下文隔离（RunContext per user）
- [ ] 更多 Agent：Debug Agent / Review Agent / Deploy Agent / Query Agent
- [ ] 更多工具：data_sampling / run_test / git_commit / git_diff
- [ ] 对话记忆（.ai/memory/ Markdown 文件）
- [ ] 业务术语表
- [ ] Airflow 集成（DAG 生成 + sqlmesh run）
- [ ] 环境管理（dev_{user} / staging / prod + TTL）

### Phase 3: 多租户 + 企业级 (8~12 周)

**目标**：多租户隔离，生产级可靠性

- [ ] 多租户隔离（Gateway 动态配置 + Schema 隔离）
- [ ] RBAC 权限体系
- [ ] 生产发布审批流
- [ ] 多 LLM Provider 支持
- [ ] 租户自带 API Key
- [ ] 调度监控 + 告警
- [ ] 审计日志
- [ ] 性能优化（工作区缓存、Agent 响应优化）
- [ ] 可观测性（Prometheus + Grafana + Loki）

### Phase 4: 智能化深化 (持续迭代)

**目标**：AI 深度集成，差异化竞争力

- [ ] Inline AI（编辑器内 Cmd+I）
- [ ] AI 异常检测 + 根因分析
- [ ] AI 性能优化建议
- [ ] AI 自动文档生成
- [ ] 数据资产目录 + 语义搜索
- [ ] CI/CD 集成（PR 自动创建预览环境）
- [ ] 成本分析（LLM Token + 计算资源）

---

## 16. 风险与应对

| 风险 | 影响 | 概率 | 应对措施 |
|------|------|------|---------|
| LLM 生成错误 SQL | 数据质量 | 高 | 安全检查 + Plan 预览 + dev 验证 |
| LLM API 不可用 | AI 功能降级 | 中 | 多 Provider + 降级为手动模式 |
| 用户工作区磁盘爆满 | 服务不可用 | 中 | 配额限制 + TTL 回收 + 监控告警 |
| Git 冲突 | 用户体验差 | 中 | 自动 rebase + 冲突检测通知 |
| Agent 并发过高 | 响应变慢 | 中 | HPA + 队列限流 + 优先级 |
| openai-agents SDK 版本变更 | 兼容性 | 低 | 锁定版本 + 适配层封装 |
| 工作区 Pod 迁移 | 短暂不可用 | 低 | PV 持久化 + 快速恢复 |
| 租户间资源争抢 | 性能下降 | 高 | 资源配额 + 优先级队列 |

---

## 17. 领域模型与业务术语

> 详见独立文档：[领域模型与业务术语设计](./domain-model.md)
>
> 包含内容：
> - 限界上下文划分（7 个域）
> - 核心实体关系（ER 模型）
> - 聚合根与实体层级
> - 领域事件定义
> - 状态机（Plan / Workspace / ChatSession）
> - 业务术语表（核心概念 / SQLMesh 专有 / 平台专有）

