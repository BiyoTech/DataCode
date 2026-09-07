# 01 · 现状评估与问题诊断

> 数据采集时间：2026-08-26。行数为实测（`wc -l`，排除测试与缓存）。

---

## 1. 服务盘点

### 1.1 代码规模与角色

| 服务 | 语言 | 端口 | 代码行数 | 占比 | 状态性质 | 部署形态（现状） |
|------|------|------|---------|------|---------|---------------|
| `gateway` | Go / Chi | 8000 | 11,775 | 22% | 无状态 + **私有 IAM 数据库** | HPA 2-10 |
| `agent-service` | Python / FastAPI | 8001 | 13,036 | 24% | 无状态（会话协调已外置 Redis） | HPA 2-10 |
| `sqlmesh-service` | Python / FastAPI | 8002 | **22,365** | **41%** | **混合**：无状态 DB + 内存 Context 池 + 本地 Git 工作副本 | HPA 2-8（声明） |
| `ontology-service` | Python / FastAPI | 8003 | 2,733 | 5% | 无状态（读工作区 YAML） | 1-2 |
| `dagster-service` | Python / FastAPI | 8004 | 4,710 | 9% | **半状态**：本地 gRPC code location 子进程 | HPA 2-8（声明） |
| `dagster-control-plane` | Python（无 HTTP） | — | 689 | 1% | sidecar | 与控制面同 Pod |
| `shared/python` | Python 库 | — | 2,065 | — | 库 | — |
| **合计（后端）** | | | **57,372** | | | |
| `frontend` | Vue 3 / TS | 5173 | 43,039 | — | — | 静态 |

### 1.2 路由暴露面（网关视角）

来源：`gateway/internal/router/router.go`

| 网关路径 | 后端 | 说明 |
|---------|------|------|
| `/api/v1/auth/*`、`/me`、`/rbac/*` | **gateway 自身** | 注册/登录/选租户/刷新/成员/角色 |
| `/api/v1/sessions/*`、`/models` | agent-service | 含 SSE `/{sid}/events` |
| `/api/v1/sqlmesh/*` | sqlmesh-service（剥前缀） | models/lineage/plan/apply/test/table-diff/SSE |
| `/api/v1/{gateways,schemas,external-models}` | sqlmesh-service | 引擎连接与 schema |
| `/api/v1/workspaces/*` | sqlmesh-service（+网关注入身份） | 工作区 CRUD、成员、environments |
| `/api/v1/environments/*` | sqlmesh-service | 环境注册中心 |
| `/api/v1/git/*` | sqlmesh-service | 全量 Git + 凭证 |
| `/api/v1/files/*` | sqlmesh-service | 文件读写 |
| `/api/v1/logs/*` | sqlmesh-service | 工作区日志 + SSE |
| `/api/v1/knowledge/*` | sqlmesh-service | 知识库文档 |
| `/api/v1/ontology/*` | ontology-service | 语义层（全部要求 `workspace_path`） |
| `/api/v1/dagster/*` | dagster-service | assets/deployments/runs/schedules/pipelines |

**观察**：8 个网关路由组指向同一个 `sqlmesh-service`。这个服务实际上是 **"除 AI 与编排以外的一切"**，其名字与职责已经严重不符。

---

## 2. 耦合分析

### 2.1 服务间调用图（实测）

```
                    ┌──────────────────┐
                    │  edge: gateway   │
                    └────┬────┬───┬────┘
             ┌───────────┘    │   └──────────────┐
             ▼                ▼                  ▼
    ┌────────────────┐  ┌──────────┐   ┌──────────────────┐
    │ agent-service  │  │ ontology │   │ dagster-service  │
    └───┬──────┬─────┘  └────┬─────┘   └────┬────────▲────┘
        │      │             │              │        │
        │      └─────────────┼──────────────┘        │
        ▼                    ▼                       │
    ┌──────────────────────────────────┐             │
    │        sqlmesh-service           │─────────────┘
    └──────────────────────────────────┘   ⚠ 双向环路
```

实测调用边（`grep *_service_url`）：

| 调用方 | 被调方 | 位置 | 用途 |
|--------|--------|------|------|
| agent-service | sqlmesh-service | `tools/query.py:88`、`list_models.py:32`、`get_lineage.py:42`、`schema_discovery.py:33` | Agent 工具调用 |
| agent-service | *（子进程 env 注入）* | `tools/shell.py:194,203` | CLI shim 反向调 sqlmesh/dagster API |
| ontology-service | sqlmesh-service | `sqlmesh_metadata.py:43` | 交叉引用校验（fail-soft） |
| dagster-service | sqlmesh-service | `bridge/sqlmesh_bridge.py:52`、`bridge/sqlmesh_resource.py:48` | asset 内触发 SQLMesh run |
| dagster-service | *（子进程 env 注入）* | `core/code_location_manager.py:577` | code location 子进程持 `SQLMESH_SERVICE_URL` |
| **sqlmesh-service** | **dagster-service** | `environments/deployment_binder.py:61` | ensure/destroy deployment |

**`sqlmesh-service ⇄ dagster-service` 是一个真实的双向 HTTP 环路**：环境创建时 sqlmesh 调 dagster 建 deployment；asset 执行时 dagster 调 sqlmesh 跑模型。这个环路无法用"依赖倒置"解开，因为它反映的是一个事实：**environment 这个概念同时是 SQLMesh 虚拟环境和 Dagster deployment**，它们本来就该在一个进程里。

### 2.2 工作区目录：三服务共享的可变状态

单一真源：`sqlmesh-service/src/workspace/paths.py`

```
{base_path}/{tenant_id}/{workspace_id}/{user_id}/
    ├── .repo/                      裸库（对象共享）
    └── envs/{environment}/         Git worktree = 一个"工作副本"
        ├── sql-warehouse/          ← sqlmesh-service 读写（models/audits/config.yaml）
        ├── ontology/               ← ontology-service 只读（loader.py:60）
        ├── asset-dag/              ← dagster-service 读 + spawn gRPC（code_location_manager）
        ├── knowledge/              ← sqlmesh-service 读写
        ├── errors/                 ← sqlmesh-service 写（error_archive）
        └── logs/service/*.ndjson   ← 各服务写（WorkspaceLogSink）
```

`workspace_path` 作为 query 参数在**全平台所有服务间传递**（`proxy_routes.go` 里几乎每个 validated route 都 `Required: ["workspace_path"]`）。

**这是当前架构最深的问题**：三个独立伸缩的服务对同一个可变目录树有写权限。直接后果：

1. **必须共享可变 PV**（NFS/EFS/CephFS）。设计文档 §4.4 已明确要求。
2. **无法沙箱化**。工作区目录被三个服务的容器挂载，任何一个服务的沙箱化都不完整；Agent 的 `shell` 工具（`agent-service/src/tools/shell.py:94` `create_subprocess_exec`）直接在宿主进程里跑用户命令，仅靠正则黑名单（`conf/shell-command-policy.yaml`）拦截——策略文件自己承认：*"Base64/eval obfuscation is NOT detected — these are defense-in-depth; the sandbox isolation is the primary guard"*。**而那个"primary guard" 目前并不存在。**
3. **PV 并发写风险在 1.x 生产架构设计中已被记为已知风险**，缓解措施是"约定"而非机制。

### 2.3 身份数据被埋在网关里

`gateway/internal/auth/store.go:147-247` 直接 `CREATE TABLE IF NOT EXISTS`：

```
users, tenants, tenant_members, refresh_tokens,
permissions, roles, role_permissions, workspace_members, rbac_audit_log
```

而 RBAC 权限点在 Go 代码里硬编码（`gateway/internal/rbac/catalog.go:35-87`，7 个权限点 + 6 个角色）。

后果：
- Python 侧服务只能从 HTTP header 读 `X-User-ID`/`X-Tenant-ID`（`sqlmesh-service/src/api/deps.py` 的 `CallerContext`），**无法自己做授权判断**——所有鉴权集中在网关（`middleware/authorize.go` 单点），这对边缘层是对的，但意味着任何**非 HTTP 入口**（Dagster schedule 触发、reconciler 后台任务、CLI shim）都**完全绕过 RBAC**。
- `workspace_members` 表被 gateway 建、也被 gateway 查，但 `workspaces` 表由 sqlmesh-service 的 ORM 建（`workspace/models.py:39`）。**同一个聚合根的两半分属两个语言两个代码库**。

### 2.4 表归属现状

| 表 | 创建者 | 语言 |
|----|--------|------|
| `users` `tenants` `tenant_members` `refresh_tokens` | gateway | Go 裸 SQL |
| `permissions` `roles` `role_permissions` `workspace_members` `rbac_audit_log` | gateway | Go 裸 SQL |
| `workspaces` `user_git_credentials` `plan_apply_history` `knowledge_documents` | sqlmesh-service | SQLAlchemy |
| `environments` `environment_events` | sqlmesh-service | SQLAlchemy |
| `sessions` `messages` `timeline_events` `memory_events` `memory_summaries` | agent-service | SQLAlchemy |
| Dagster `runs`/`event_logs`/`schedules` | Dagster OSS | 自管 |

**一个数据库，四套 schema 管理机制，两种语言，无统一迁移链。**

---

## 3. 1000 租户 × 1000 子用户下的瓶颈量化

### 3.1 规模假设

| 参数 | 值 | 推导 |
|------|---|------|
| 租户数 | 1,000 | 给定 |
| 每租户用户数 | 1,000 | 给定 |
| 总用户数 | 1,000,000 | |
| 日活率 | 5% | 保守（B 端数据平台） |
| 日活用户 | 50,000 | |
| 峰值并发在线 | 5,000 | 日活的 10% |
| 每租户工作区数 | 5 | 假设 |
| 工作区总数 | 5,000 | |
| 每工作区活跃环境 | 4（prod + 3 分支） | 依 `environments.max_per_workspace: 20` 上限保守取 |
| **活跃工作副本（worktree）** | **~20,000** | 工作区 × 环境（多用户共享同一环境时 worktree 归属活跃成员） |
| 峰值同时"打开工作区"的会话 | ~2,000 | 并发在线的 40% 在编辑态 |

### 3.2 瓶颈 1：SQLMesh Context 内存与亲和路由缺失

**现状**：`sqlmesh-service/src/core/context_pool.py`

```python
ContextPool(max_size=20, idle_ttl_seconds=1800, max_age_seconds=7200)
# key = (workspace_path, gateway)
```

设计文档 §13 决策 7 声明「ContextPool 分片键 = `workspace_id`，由网关路由保证同 workspace_id 落同一副本」。

**实测结论：网关没有实现这个亲和路由。** `router.go` 全部使用 `handler.GenericProxy` / `MakeValidatedProxy`，其 `BaseURL` 是单个 `cfg.Services.SQLMeshServiceURL`，转发依赖 K8s Service 的**随机负载均衡**。没有一致性哈希、没有 session affinity、`proxy.go` 里没有任何按 `workspace_path` 选择上游的逻辑。

于是 N 副本下的真实行为：

| 现象 | 后果 |
|------|------|
| 同一工作区的连续请求落到不同副本 | 每个副本各自 load 一份 Context ⇒ **内存放大 N 倍** |
| `max_size=20` 在每副本独立生效 | 2,000 并发编辑会话 / 8 副本 = 250 工作区/副本，远超 20 ⇒ **LRU 疯狂驱逐** |
| 每次驱逐后重新 `Context(paths=...)` | SQLMesh 要重解析全部 SQL 模型，秒级～十秒级 ⇒ **p99 爆炸** |
| `dirty` 标志、`write_lock` 是进程内状态 | 副本 A 写文件后置 dirty，副本 B 的 Context 仍是旧的 ⇒ **读到过期模型** |

**量化**：假设一个中等仓库 Context 常驻 200 MB。

- 目标：2,000 并发编辑会话
- 需要常驻 Context：2,000 × 200 MB = **400 GB**
- 8 副本 × `max_size=20` = 160 个 Context 槽位 = 32 GB
- **缺口 12.5 倍**，且因随机路由实际有效槽位更少

**结论**：`max_size=20` 不是可调参数问题，是**路由模型缺失**。必须先有工作区→实例的显式绑定，容量才可计算。

### 3.3 瓶颈 2：共享可变 PV 上的 Git 操作

`sqlmesh-service/src/workspace/git_ops.py` 有 **2,164 行**，是全平台最大单文件，全部是 `git` CLI 子进程操作。

规模下的问题：

| 维度 | 数量 | 问题 |
|------|------|------|
| 目录 inode | 20,000 worktree × ~500 文件 = **10^7 文件** | NFS/EFS 元数据操作是网络往返；`git status` 要 stat 全树 |
| `.repo/` 裸库 | 每用户一个 = 最多 10^6 个裸库 | 每个都是完整对象库；`git gc` 无人管理 |
| 并发 git 进程 | 峰值 2,000 编辑会话 | 每个 `git status`/`git diff` 是 fork + 全树 stat |

EFS 的元数据延迟约 1-3 ms/op。一次 `git status` 在 500 文件的树上约 500-1500 次 stat ⇒ **单次 0.5-4.5 秒**。前端每次打开面板都调 `/api/v1/git/status`。

**结论**：Git 工作副本**必须在实例私有目录**（节点 NVMe / emptyDir / per-replica PVC 均可，只要不被多实例共享写），共享存储只能作为冷层。这直接推导出「工作区必须归属单一实例」。

### 3.4 瓶颈 3：Dagster code location 端口与进程数

`dagster-service/src/core/port_allocation.py` + `conf/dagster-service.yaml`：

```yaml
code_location_port: 4000
code_location_port_segment_size: 50    # 每副本 50 个端口
code_location_port_max_segments: 64    # 最多 64 副本
```

**上限 = 50 × 64 = 3,200 个并发 code location**。

而目标是 20,000 活跃工作副本，每个活跃 deployment 需要一个 code location gRPC 子进程：

| 项 | 值 |
|----|---|
| 需要的 code location | 20,000 |
| 端口空间上限 | 3,200 |
| **缺口** | **6.25 倍** |
| 单个 code location 常驻内存 | ~150-300 MB（要 import 用户 asset-dag + dagster） |
| 20,000 个的内存 | **3-6 TB** |

即使把端口段调大，**每个活跃环境常驻一个 Python 进程这个模型本身在这个规模下不成立**。

同时：**Dagster daemon 是全局单副本硬约束**。一个 daemon 要为 20,000 个 code location 做 schedule tick + sensor evaluation。官方 daemon 的 tick 是串行遍历 workspace 中所有 location 的。这是**架构级单点**，不是配置问题。

**结论**：必须 ① code location 按需拉起 + 空闲回收（从"常驻"变"租约"）；② 控制面**按 Cell 水平切分**，每个 Cell 一个 daemon，租户 shard 到 Cell。

### 3.5 瓶颈 4：单一 Postgres

| 表 | 行数估算 | 增长源 |
|----|---------|--------|
| `users` | 10^6 | 给定 |
| `workspace_members` | 5,000 ws × ~20 成员 = 10^5 | |
| `environments` | 20,000 | |
| `environment_events` | append-only，20,000 × 100/月 = **2×10^6 / 月** | 状态机每次转移 |
| `messages` | 50,000 DAU × 20 消息/天 = **10^6 / 天** | **最快增长** |
| `timeline_events` | 消息的 5-10 倍 = **10^7 / 天** | SSE 事件持久化 |
| Dagster `event_logs` | 20,000 env × N run × 事件数 | 官方建议 30 天保留 |

`timeline_events` 10^7/天 = **3×10^9/年**。单实例 Postgres 放不下也扫不动。

**结论**：需要 ① 按租户分片（Cell 内一套 DB）；② 高频 append 表（timeline/events/logs）与 OLTP 表分离存储。

### 3.6 瓶颈 5：Agent 长连接与 LLM 并发

`agent-service` 一次 goal loop：`max_iterations: 50`、`timeout_seconds: 600`。峰值 5,000 并发在线，假设 10% 在跑 agent = **500 个并发 goal loop**，每个占用一个 asyncio task + 一条 SSE 连接 + 一个 LLM 流。

这与 `control-service` 型请求（毫秒级 DB 读写）的扩容曲线完全不同：
- Agent：按**并发对话数**扩，瓶颈是 LLM 配额与连接数
- Control：按**QPS**扩，瓶颈是 DB 连接池

**这正是不能把 agent 合并进 control 的原因。**

### 3.7 瓶颈汇总

| # | 瓶颈 | 现状上限 | 目标 | 倍数缺口 | 性质 |
|---|------|---------|------|---------|------|
| 1 | SQLMesh Context 内存 | 160 槽位（8 副本） | 2,000 并发编辑 | **12.5×** | 路由模型缺失 |
| 2 | 共享 PV 上 git 操作 | `git status` 0.5-4.5 s | < 100 ms | **10-45×** | 存储层错配 |
| 3 | Dagster code location | 3,200 端口 / 数 TB 内存 | 20,000 | **6.25×** | 常驻模型不成立 |
| 4 | Dagster daemon | 全局 1 副本 | 20,000 location 的 tick | **架构单点** | 需要单元化 |
| 5 | 单一 Postgres | `timeline_events` 3×10^9/年 | — | **不可行** | 需要分片 |
| 6 | Agent 并发对话 | 与 control 混合扩容 | 500 并发 loop | — | 扩展维度混淆 |

---

## 4. 安全与隔离现状

| 维度 | 现状 | 问题 |
|------|------|------|
| **进程隔离** | 无。用户 shell 命令由 `agent-service/src/tools/shell.py:94` 在服务进程内 `create_subprocess_exec` | 用户代码与平台服务同 UID、同网络命名空间、同文件系统视图 |
| **命令拦截** | 正则黑名单 `conf/shell-command-policy.yaml` + L1-L6 审批 | 策略文件自述无法防 base64/eval/变量展开；无 seccomp/无 capability drop |
| **文件路径** | 双层校验：网关 `rbac.OwnsWorkingCopy`（`proxy_routes.go:96`）+ 后端 `parse_workspace_path`（`paths.py:142`，强制 5 段布局） | 路径校验做得**扎实**，但仅防路径穿越，不防"在自己工作区里跑恶意进程" |
| **用户代码执行** | Dagster code location 子进程 import 用户 `asset-dag/definitions.py` | **任意 Python 代码执行**在 dagster-service 进程的子进程里，无沙箱 |
| **网络出口** | 无限制 | 用户代码可访问集群内任意服务（含 Postgres、Redis、其他租户的 runtime） |
| **资源限制** | 无 per-user cgroup/quota | 一个用户的 `SELECT * FROM huge_table` 或死循环可打满整个 Pod |
| **RBAC 覆盖** | 仅 HTTP 入口（`middleware/authorize.go`） | Dagster schedule 触发、reconciler、CLI shim **全部绕过** |

**核心结论**：当前平台**允许用户在平台进程空间内执行任意代码**，而防线是正则表达式。在 1000 租户共享集群下这是不可接受的。设计文档自己把沙箱称为 "primary guard"，但它没有被实现。

---

## 5. 值得保留的设计资产

重构不是推翻。以下已实现的机制是 2.0 的基石，**必须原样继承**：

| 资产 | 位置 | 为什么保留 |
|------|------|-----------|
| **命名代数** | `sqlmesh-service/src/environments/naming.py` + `dagster-service/src/core/naming.py` | `env ⇄ branch ⇄ deployment` 纯函数可逆映射。这是 reconciler 能工作的前提 |
| **工作区路径布局** | `workspace/paths.py` | 5 段强制布局 + tenant 归属校验，是路径安全的基础，2.0 只是**改变谁挂载它** |
| **环境注册中心 + 状态机** | `environments/registry.py`（1,297 行）+ `reconciler.py`（1,061 行） | 四方对账（注册表/Git/SQLMesh state/Dagster deployment）。这是平台最有价值的部分 |
| **Redis 协调原语** | `shared/python/datacode_common/redis/`、`env_locks.py`、`session_coordination.py` | 每个原语自带降级 + TTL 是 crash 恢复界。2.0 里锁的**范围会缩小**，但原语本身正确 |
| **Dagster 控制面/数据面分离** | `dagster-control-plane/workspace_sync.py` | PULL 单一写者模式是对的，2.0 里它成为 Cell 内组件 |
| **双层配置** | `conf/platform.yaml` + `conf/{service}.yaml` | 拓扑单一真源，凭证 env-only |
| **全链路 trace_id** | `LogContextMiddleware` + `WorkspaceLogSink` | 直接继承 |
| **RBAC 权限点声明** | `gateway/internal/rbac/catalog.go` | 权限模型是对的，只是**归属错了**（该在 identity-service） |

---

## 6. 诊断结论

三句话：

1. **边界切错了维度**：按外部引擎（SQLMesh/Dagster/Ontology）切，导致三个独立伸缩的服务共享同一个可变工作区目录，产生 HTTP 环路且无法沙箱化。应按**状态性质**切。
2. **有状态部分没有承认自己有状态**：`sqlmesh-service` 与 `dagster-service` 都声明"无状态可 HPA"，但前者持有内存 Context 池 + Git 工作副本，后者持有 code location 子进程。它们需要的是**显式的工作区亲和 + 租约**，而不是假装无状态然后靠 PV 兜底。
3. **规模模型缺失**：所有容量参数（`max_size=20`、`3200` 端口、单 daemon、单 DB）都是单集群假设。1000×1000 需要**单元化（Cell）** 而非单纯加副本。
