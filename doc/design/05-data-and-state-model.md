# 05 · 数据与状态模型

---

## 1. 状态归属总表

**原则：每个状态只有一个拥有者（owner），其他服务只能经 owner 的 API 访问。**

| 状态 | Owner | 存储 | 作用域 | 现状 owner |
|------|-------|------|--------|-----------|
| 用户 / 租户 / 成员关系 | identity-service | Global PG | 全局 | ⚠️ gateway 裸 SQL |
| 角色 / 权限 / 权限绑定 | identity-service | Global PG | 全局 | ⚠️ gateway 裸 SQL |
| refresh token | identity-service | Global PG | 全局 | ⚠️ gateway |
| 租户 → Cell 映射 | identity-service | Global PG | 全局 | ❌ 不存在 |
| 工作区注册表 | control-service | Cell PG | Cell | sqlmesh-service |
| 工作区成员 | identity-service（授权） | Global PG | 全局 | ⚠️ gateway 建表 |
| 环境注册表 + 事件 | control-service | Cell PG | Cell | sqlmesh-service ✅ |
| Plan/Apply 历史 | control-service | Cell PG | Cell | sqlmesh-service ✅ |
| Git 凭证（加密） | control-service | Cell PG | Cell | sqlmesh-service ✅ |
| 知识库文档元数据 | control-service | Cell PG | Cell | sqlmesh-service ✅ |
| 租户配额与用量 | control-service | Cell PG | Cell | ❌ 不存在 |
| **工作区租约** | control-service（写）| Redis（读） | Cell | ❌ 不存在 |
| 任务记录与审计 | control-service | Cell PG | Cell | ❌ 不存在 |
| 会话 / 消息 | agent-service | Cell PG | Cell | agent-service ✅ |
| timeline / memory 事件 | agent-service | Cell PG 分区表 | Cell | agent-service ✅ |
| Dagster run / event / schedule | orchestrator | Cell PG（独立 schema） | Cell | ✅ |
| **工作副本文件树** | **workspace-runtime（独占）** | 实例私有目录（`mount_kind`：emptyDir / per-replica PVC） | 实例 | ⚠️ 三服务共享 PV |
| SQLMesh Context（元数据视图） | workspace-runtime | 内存 | 实例 | sqlmesh-service |
| SQLMesh Context（可执行） | **sandbox 容器** | 内存 | 租约 | ⚠️ 平台进程内 |
| Ontology registry | workspace-runtime | 内存 | 实例 | ontology-service |
| Code location 进程 | 独立 Pod | — | deployment | dagster-service |
| SQLMesh state（虚拟环境元数据） | SQLMesh 自身 | 客户仓库 `sqlmesh__{ws}` schema | 工作区 | ✅ |
| 工作区日志 NDJSON | workspace-runtime | Pod 本地盘 + Loki | 实例 | ✅ |

---

## 2. 数据库拆分

### 2.1 Global DB（identity-service 独占）

```sql
-- 全局唯一，跨 Cell。规模：10^6 users, 10^3 tenants
-- All timestamps are UTC.

users              (id, email, password_hash, status, created_at, updated_at)
tenants            (id, name, slug, plan, dedicated_cell, status, created_at)
tenant_members     (tenant_id, user_id, role, created_at)          -- 10^6 行
workspace_members  (workspace_id, user_id, role, granted_by, created_at)  -- 10^5 行
refresh_tokens     (id, user_id, token_hash, expires_at, revoked_at)
permissions        (code, scope, resource, action, description)     -- 静态目录
roles              (id, tenant_id, name, scope, builtin, created_at)
role_permissions   (role_id, permission_code)
rbac_audit_log     (id, actor_user_id, action, target, detail, created_at)  -- append-only

-- 2.0 新增
tenant_placement   (tenant_id, cell_id, target_cell_id, migration_state, ...)
cells              (cell_id, region, endpoint, status, capacity_tenants, assigned_tenants)
```

**为什么 `workspace_members` 在 Global 而 `workspaces` 在 Cell**：成员关系是**授权数据**，必须在鉴权路径上可用（gateway 的授权缓存来源）。而工作区本体是 Cell 内业务数据。

代价：`workspaces` 与 `workspace_members` 跨库，无法 FK。约束：
- 工作区创建流程：先在 Cell PG 建 `workspaces` 行 → 成功后调 identity-service 建 owner 的 `workspace_members` 行 → 失败则补偿删除（Saga）
- 工作区删除：先撤 `workspace_members`（立即失去访问）→ 再删 Cell 数据
- 后台对账任务检测孤儿（有 members 无 workspace / 反之），归入 reconciler

### 2.2 Cell DB（每 Cell 一套）

```sql
-- Schema: platform  (control-service 拥有，alembic 管理)
workspaces          (id, tenant_id, name, slug, git_remote_url, default_gateway, created_at)
environments        (id, workspace_id, name, env_type, state, branch, deployment,
                     ttl_expires_at, created_by, created_at, updated_at)
environment_events  (id, environment_id, event_type, from_state, to_state,
                     detail, actor, created_at)                     -- append-only
plan_apply_history  (id, workspace_id, environment, status, plan_json, started_at, ...)
user_git_credentials(id, user_id, kind, encrypted_blob, is_default, created_at)
knowledge_documents (id, workspace_id, title, path, kind, created_at)

-- 2.0 新增
tenant_quotas       (tenant_id, resource, limit_value, updated_at)
tenant_usage        (tenant_id, resource, period_start, used_value)  -- 按天/月聚合
task_records        (id, kind, isolation_level, tenant_id, user_id, workspace_id,
                     lease_key, state, submitted_at, started_at, finished_at,
                     exit_code, runner_pod)                          -- 任务审计
lease_history       (id, lease_key, runtime_id, acquired_at, released_at, reason)

-- Schema: agent  (agent-service 拥有)
sessions            (id, user_id, tenant_id, workspace_path, agent_id, mode, created_at)
messages            (id, session_id, role, content, created_at)
timeline_events     (id, session_id, kind, payload, created_at)      -- 分区表，见 §2.3
memory_events       (id, session_id, ...)                            -- 分区表
memory_summaries    (id, session_id, ...)

-- Schema: dagster  (Dagster OSS 自管，不由我们迁移)
runs, event_logs, schedules, instigators, ...
```

**三 schema 同库起步，可独立拆库**：`platform` / `agent` / `dagster` 三个 schema 放在一个 Cell PG 实例里。当 `agent.timeline_events` 或 `dagster.event_logs` 成为瓶颈时，可无代码改动地把该 schema 迁到独立实例（只改连接串）。

### 2.3 高频 append 表的分区

```sql
-- timeline_events: 单 Cell 2×10^6/天。按天 RANGE 分区。
CREATE TABLE agent.timeline_events (
    id           BIGSERIAL,
    session_id   VARCHAR(64) NOT NULL,
    kind         VARCHAR(48) NOT NULL,
    payload      JSONB,
    created_at   TIMESTAMP NOT NULL,          -- UTC
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- pg_partman 自动维护：预建 7 天，detach 30 天前的分区
-- detach 后导出 Parquet 到对象存储，然后 DROP

CREATE INDEX ON agent.timeline_events (session_id, created_at DESC);
```

保留策略：

| 表 | 热保留（PG） | 冷归档（S3 Parquet） | 依据 |
|----|------------|-------------------|------|
| `timeline_events` | 30 天 | 1 年 | 会话回放需求 |
| `memory_events` | 90 天 | 永久 | 记忆是长期资产 |
| `environment_events` | 永久（量小，2×10^6/月） | — | 审计与 reconcile 依赖 |
| `task_records` | 90 天 | 1 年 | 安全审计 |
| `dagster.event_logs` | 30 天（官方 retention 配置） | 按需 | 官方推荐 |
| `rbac_audit_log` | 1 年 | 7 年（合规） | 合规 |

---

## 3. 时区与时间处理（遵循平台规则）

| 层 | 规则 |
|----|------|
| **数据库** | 全部 `TIMESTAMP`（无时区）存 **UTC**，或 `TIMESTAMPTZ` 显式 UTC。**绝不存本地时间** |
| **后端** | 全部业务逻辑（计算/比较/TTL/租约过期/调度窗口）用 UTC。`datetime.now(timezone.utc)`，禁止 `datetime.now()` |
| **API 传输** | ISO 8601 带 `Z` 后缀（`2026-08-26T12:28:00Z`） |
| **前端** | 按用户配置时区渲染。时区**自动检测**（`Intl.DateTimeFormat().resolvedOptions().timeZone`）+ **手动可选**（用户设置持久化） |
| **日志** | UTC ISO 8601，英文 |
| **Dagster 调度** | Dagster schedule 的 `execution_timezone` 由用户按环境配置（业务语义需要本地时间，如"每天上午 9 点北京时间"），但**存储的 tick 时间是 UTC** |

**用户时区存储**：

```sql
-- Global PG（跟随用户，跨 Cell 一致）
ALTER TABLE users ADD COLUMN timezone VARCHAR(64) DEFAULT 'UTC';   -- IANA name
ALTER TABLE users ADD COLUMN locale   VARCHAR(16) DEFAULT 'en';    -- 'zh' | 'en'
```

前端在 `/api/v1/me` 响应中拿到 `timezone` 与 `locale`，作为渲染基准；用户可在设置页修改。

---

## 4. 迁移链管理

### 4.1 现状问题

四套机制、两种语言、无统一链：

| 机制 | 位置 | 问题 |
|------|------|------|
| Go 裸 `CREATE TABLE IF NOT EXISTS` | `gateway/internal/auth/store.go:159-247` | 无版本、无回滚、列变更需手工 |
| SQLAlchemy `create_all` + alembic | sqlmesh-service `alembic/` | 0001 是空基线，建表归 `create_all`，迁移只演化 |
| SQLAlchemy `create_all` + alembic | agent-service `alembic/` | 同上 |
| Dagster 自管 | — | 不干预 |

### 4.2 2.0 规则

| 库 | Owner | 工具 | 规则 |
|----|-------|------|------|
| **Global PG** | identity-service | **golang-migrate**（SQL 文件） | 版本化 up/down；启动时校验版本，不自动升级（由部署流水线执行） |
| **Cell PG · `platform`** | control-service | **alembic** | 单一链；0001 为空基线，基础表由 control 的 `create_all` 建立（迁移只演化）；生产由迁移步骤执行 `python -m src.db_migrate`（`create_all` + `upgrade head`，建表权收敛于 control 一家，runtime 只建自己拥有的三张表）；副本 `AUTO_MIGRATE=0` |
| **Cell PG · `agent`** | agent-service | **alembic** | 独立链（独立 `version_table`） |
| **Cell PG · `dagster`** | Dagster | 官方 | 不干预 |

**关键变更**：identity-service 的表从「裸 SQL 建表」改为「受管迁移」。这是必须的——`users` 表加一列在当前架构下没有任何安全的做法。

**多 Cell 的迁移编排**：

```
部署流水线：
1. 迁移 Global PG（一次）
2. 逐 Cell 滚动：对每个 Cell
   a. 标记 Cell status=maintenance（identity-service 拒绝签发该 Cell 的新 token）
   b. 等 drain（已有请求完成，~60s）
   c. alembic upgrade head（platform + agent schema）
   d. 滚动更新 Cell 内服务镜像
   e. 健康检查通过 → status=active
3. 任一 Cell 失败 → 停止后续 Cell（限制爆炸半径为 1 个 Cell）
```

**向后/向前兼容要求**：因为是逐 Cell 滚动，Global DB 的 schema 必须同时兼容新旧版本的 Cell 服务。迁移必须遵循 expand-contract：

```
1. Expand:   加列（nullable）/ 加表，新旧代码都能跑
2. Migrate:  部署新代码（写新列，读时兼容旧列缺失）
3. Backfill: 回填历史数据
4. Contract: 下一个版本才删旧列
```

---

## 5. 状态一致性

### 5.1 五方对账（Reconciler 扩展）

现有 reconciler 做四方对账（`environments/reconciler.py`，1,061 行）：注册表 / Git / SQLMesh state / Dagster deployment。

2.0 增加第五方：**租约**。

| 方 | 真源 | 读取方式 |
|----|------|---------|
| 1. 环境注册表 | Cell PG `environments` | 直读 |
| 2. Git 分支 | Git Server | `git ls-remote` |
| 3. SQLMesh 虚拟环境 | 客户仓库 `sqlmesh__{ws}` | 经 runtime 查询 |
| 4. Dagster deployment | orchestrator workspace.yaml + code location Pod | GraphQL + K8s API |
| 5. **工作区租约** | Redis `lease:*` + runtime 心跳 | 直读 Redis |

新增漂移类型：

| 漂移 | 检测 | 修复 |
|------|------|------|
| `orphan_lease` | Redis 有租约，但对应 environment 已删除 | 通知 runtime 释放 |
| `stale_lease` | 租约指向的 runtime 心跳已消失（TTL 未到但实例已死） | Lua CAS 删除租约 |
| `lease_split_brain` | 两个 runtime 都声称持有同一 lease_key（Redis 降级期可能发生） | 以文件锁（`.repo/.datacode-lease`）为仲裁，落败方立即释放并告警 |
| `orphan_codeloc_pod` | code location Pod 存在但环境已删/租约已释放 | 删除 Pod |
| `zombie_task` | `task_records` 状态为 running 但 Pod 不存在 | 标记 failed，通知提交方 |

### 5.2 租约的正确性论证

**不变式 I1：任一时刻，一个 lease_key 至多被一个 runtime 挂载写入。**

三道防线：

| 层 | 机制 | 覆盖场景 |
|----|------|---------|
| **1. Redis SET NX** | 租约获取是原子的 | 正常情况 |
| **2. 文件锁 `flock`** | runtime 物化 worktree 前对 `{node_workdir}/{lease_key}/.datacode-lease` 加排他 `flock`；失败则拒绝服务该租约 | Redis 降级/脑裂；同节点冲突 |
| **3. Git worktree 自身约束** | Git 强制一个分支至多被一个 worktree checkout（`paths.py` 文档已指出这点） | 同 `.repo` 内的冲突 |

**关键：防线 2 是跨节点无效的**（不同节点的 flock 互不可见）。因此跨节点场景依赖防线 1。Redis 降级时的正确性保证：

```
Redis 不可用 → 网关退化为稳定哈希路由 hash(lease_key) % replicas
  • 副本数不变 → 路由确定，正确
  • 副本数变化（扩缩容/Pod 重启）→ 可能路由到不同实例
    ⇒ 缓解：Redis 不可用期间 LeaseController 冻结扩缩容
    ⇒ 兜底：runtime 物化前向 Git 请求 worktree lock；Git 侧 `.repo` 的
            worktree 元数据（.git/worktrees/）会检测冲突并报错
    ⇒ 最坏情况：两实例挂载同一 worktree → Git 自身报 "already checked out"
                → 落败方返回 409，网关重试到另一实例
```

**明确接受的残余风险**：Redis 长时间不可用 + 副本数变化，可能出现短暂的双挂载。缓解是 Git 层报错而非静默损坏，且 reconciler 的 `lease_split_brain` 检测会告警。**这个风险是可接受的，因为损坏的最坏后果是一个用户的未提交改动冲突，而非跨租户数据泄露。**

**第四道防线：介质本身（Q7）。** 上述三道防线都是运行期机制，可能因配置失误被绕过。`materialize.mount_kind` 把介质纳入 I1 的论证：

| `mount_kind` | 双挂载在物理上是否可能 | 处理 |
|-------------|--------------------|------|
| `ephemeral` / `instance_local` | ❌ 不可能——两个副本的 `materialize.root` 是**不同的卷**，各自物化各自的副本 | 最坏后果降为"两份工作副本内容分叉"（缓存不一致），而非目录损坏 |
| `shared` | ✅ 可能，且防线 2（flock）跨节点无效 | **启动期硬拒绝**——不允许这种配置存在 |

这就是为什么 `shared` 不能只是"不推荐"：**它会让 I1 从"由三道防线保证"退化为"由两道防线尽力而为"**，而其中最强的那道（Redis SET NX）恰恰是降级时最先失效的。同理，`env_lock` 的 Redis 后端只能在介质切换到实例私有之后才退化为进程内锁（见 §06.P4 4b）。

### 5.3 未提交改动的持久性

工作副本在实例本地目录 ⇒ 该目录消失则未提交改动丢失。这是 2.0 引入的**新风险**，必须显式处理。

| 机制 | 时机 | 覆盖 |
|------|------|------|
| **前端草稿自动保存** | 编辑器每 5s / 失焦时 | 编辑器内未保存的内容 → control-service `file_drafts` 表 |
| **优雅下线时 WIP push** | runtime 收到 SIGTERM / draining | `git stash` → push 到 `refs/datacode-wip/{user}/{env}` → 新实例物化时自动恢复 |
| **周期性 WIP 快照** | 每 10 分钟，若有改动 | 同上（防非优雅崩溃） |
| **物化时恢复** | 租约建立后 | 检测 `refs/datacode-wip/{user}/{env}` 存在 → 提示用户"发现未提交改动，是否恢复" |

`refs/datacode-wip/*` 是隐藏 ref（不在 `refs/heads/` 下），不污染用户分支列表。

**保证等级**：优雅下线 = 零丢失；非优雅崩溃 = 最多丢 10 分钟（快照周期）+ 编辑器草稿已在 DB。可接受。

**`mount_kind` 如何缩小风险面**（Q7）：介质可配置不是"临时回滚开关"，而是**永久的持久化缓存能力**，它实质降低了上表要兜底的场景数量：

| 事件 | `ephemeral`（emptyDir） | `instance_local`（per-replica PVC） |
|------|----------------------|-----------------------------------|
| 滚动更新 / Pod 重启 | 工作副本丢失，靠 WIP ref 恢复 | **原地保留，零丢失、零 re-clone** |
| OOMKill | 同上 | **同上** |
| 节点故障 / PVC 不可用 | 丢失 | 丢失（**此时才需要 WIP 快照**） |
| 大仓库首次物化 | 每次重启都要重 clone | **`.repo` 缓存存活** |

因此生产默认 `instance_local`：10 分钟契约仍然是对外承诺（覆盖节点级故障），但**日常运维动作（部署、缩容）不再触碰这个窗口**。

---

## 6. 缓存一致性

现状问题：`ContextPool` 的 `dirty` 标志是进程内状态，多副本下副本 A 写文件、副本 B 的 Context 仍旧 ⇒ 读到过期模型。

2.0 解法：**租约让这个问题消失。**

```
同一工作副本的所有读写都路由到同一个 runtime 实例
  ⇒ 写文件 → 同进程置 dirty
  ⇒ 读模型 → 同进程见 dirty → 刷新
  ⇒ 无需跨进程失效广播
```

这是租约模型的一个重要副产品：**消除了一整类缓存一致性问题**，也消除了对应的 Redis 广播机制。

| 缓存 | 现状机制 | 2.0 |
|------|---------|-----|
| SQLMesh Context dirty | 进程内（多副本下失效） | 进程内（**租约保证正确**） |
| Ontology registry | 进程内（跨服务不可见） | 进程内（同进程） |
| Agent session LRU | Redis 广播失效（`session_coordination.py`） | **保留**（agent-service 仍是无状态多副本） |
| Gateway 授权缓存 | `rbac/cache.go` | **保留** + 增加 `perms_version` 失效（JWT 里带版本，identity 侧变更时 bump） |
| Gateway 租约路由缓存 | ❌ 新增 | 本地 TTL 10s + 转发失败即失效 |

---

## 7. 配置模型演进

沿用双层结构（`conf/platform.yaml` + `conf/{service}.yaml`），增加 Cell 维度：

```
conf/
├── platform.yaml                  # 跨服务共享（DB/Redis/日志/拓扑）
├── platform.dev.yaml              # DATACODE_ENV=dev 覆盖
├── cells/
│   ├── cell-0.yaml                # Cell 特定：DB 端点、Redis 端点、orchestrator URL
│   └── cell-1.yaml
├── isolation-profiles.yaml         # 新增：L1/L2/L3 沙箱配置（§04.5）
├── edge-gateway.yaml
├── identity-service.yaml
├── control-service.yaml
├── agent-service.yaml
├── workspace-runtime.yaml          # 新增：租约容量、物化策略、sandbox 配置
└── shell-command-policy.yaml       # 保留（降级为 UX 层）
```

**Cell 身份注入**：每个服务实例通过 env `DATACODE_CELL_ID` 知道自己属于哪个 Cell，加载 `conf/cells/{cell_id}.yaml`。这个值同时进入：
- 所有日志的 `cell_id` 字段
- 所有 Prometheus 指标的 `cell` label
- Redis key 前缀：`{key_prefix}:{cell_id}:...`（同一 Redis 服务多 Cell 时隔离；生产每 Cell 独立 Redis 则冗余但无害）

**凭证仍然 env-only**，并按 Cell 分离（Cell A 的 `DB_PASSWORD` 与 Cell B 不同；`TOKEN_ENCRYPTION_SECRET` 按 Cell 独立，见 §03.5.3）。

新增 `workspace-runtime.yaml` 关键段：

```yaml
# Workspace runtime: the sole owner of a working copy.
lease:
  # Hard cap on concurrent working copies served by ONE instance. Configurable
  # (design 07 Q3): capacity is a function of the pod's memory budget, and the
  # per-lease cost scales with the USER'S repository size (a 50-model repo and a
  # 2000-model repo differ by an order of magnitude), so no single number is
  # right for every tenant. 0 = derive from memory_budget_bytes alone.
  max_per_instance: 16
  # The real admission driver: a new lease is accepted only while the projected
  # footprint stays under this budget. Re-sizing the pod (4/8/16 GiB) therefore
  # changes capacity with no code change.
  memory_budget_bytes: 6979321856        # 6.5 GiB = 8 GiB limit - 1.5 GiB baseline
  # Per-lease estimate, refined at runtime from measured RSS deltas (EWMA).
  estimated_lease_bytes: 220200960       # 210 MiB, to be calibrated at P0
  # Refuse new leases past this fraction of the pod's real RSS even when
  # max_per_instance is not reached — the estimate can be wrong.
  admission_rss_high_watermark: 0.80
  # TTL is a CRASH RECOVERY BOUND, not an operation timeout: a watchdog renews
  # it every ttl/3 while the holder lives.
  ttl_seconds: 120
  # A lease with no request for this long is released and its resources freed.
  idle_release_seconds: 1800
  # Grace period for in-flight apply operations during drain.
  drain_apply_grace_seconds: 600

materialize:
  # The ONE writable directory an instance materializes working copies into.
  # Git is the only source of truth, so this directory is disposable and always
  # re-derivable. There is exactly ONE materialization code path
  # (clone --bare -> .repo -> worktree add); the medium behind this path is a
  # deployment concern declared by mount_kind below and is invisible to the code.
  root: "/var/lib/datacode/workcopies"
  # Declares what the mount actually IS, so the runtime can REFUSE a
  # configuration that breaks the single-mount invariant instead of silently
  # corrupting working copies (design 07 ADR-003 / Q7).
  #   ephemeral      -> emptyDir. Nothing survives a pod restart; recovery is the
  #                     WIP ref. Cheapest, fastest start, lowest cache hit rate.
  #   instance_local -> a PVC bound to THIS replica only (StatefulSet
  #                     volumeClaimTemplates, RWO). Working copies and .repo
  #                     survive rolling updates and OOMKills => no re-clone, no
  #                     WIP recovery prompt. RECOMMENDED FOR PRODUCTION.
  #   shared         -> REJECTED AT STARTUP. A medium writable by several
  #                     replicas cannot coexist with in-process env locks: the
  #                     lease is what makes those locks sufficient, and a shared
  #                     mount lets a second writer in behind the lease's back.
  # Startup checks: mount_kind == "shared" fails hard; mount_kind ==
  # "instance_local" additionally asserts the lease subsystem is enabled.
  mount_kind: "instance_local"
  # Reclaim least-recently-used worktrees when the disk crosses this mark.
  disk_high_watermark_percent: 80
  disk_low_watermark_percent: 60
  # Shallow clone depth for first materialization; deepened on demand.
  shallow_depth: 50
  # Bounds how much uncommitted work a NON-GRACEFUL crash can lose. This is a
  # user-facing durability contract (design 07 ADR-003 / Q6 accepted), not a
  # tuning knob: changing it changes what the product promises. Shortening it
  # costs Git-server write amplification (one stash + hidden-ref push each).
  wip_snapshot_interval_seconds: 600     # 10 min
  wip_snapshot_skip_if_clean: true       # idle leases cost nothing

executor:
  # SHIPS IN P3 (design 07 ADR-005). The executable SQLMesh Context lives in a
  # PERSISTENT CHILD PROCESS, never in this service's process: constructing it
  # execs the user's Python models and macros, which stay supported (Q1).
  #
  # One executor per working copy keeps the Context hot, so isolation costs one
  # UDS round trip (<1 ms) instead of a 10-30 s Context rebuild.
  mode: "process"                        # "process" (P3) | "container" (P6)
  isolation_profile: "L3_proc"
  socket_dir: "/run/datacode/executors"
  # Stop an executor whose working copy has been idle this long; the lease may
  # outlive it and it is re-forked on the next request.
  idle_stop_seconds: 900
  # Bounded restarts: a crash-looping executor is abandoned and surfaced via
  # /health rather than respawned forever.
  max_restarts: 3
  restart_window_seconds: 300

sandbox:
  # SHIPS IN P6. Per-lease warm sandbox CONTAINER — the container form of the
  # executor above. The wire protocol is identical (UDS + length-prefixed JSON),
  # frozen in P3, so this is a deployment change rather than a rewrite.
  enabled: false                         # flipped on in P6
  profile: "L3_base"                     # L3_strong deferred, see design 04 §8
  warm_on_lease_acquire: true
  idle_stop_seconds: 900

codeloc:
  # Dagster code locations run as SEPARATE Pods, not subprocesses of this
  # service: 1,100 warm locations per cell would otherwise blow the runtime
  # memory budget (design 03 §3.6).
  mode: "pod"                       # "pod" | "subprocess" (dev only)
  warm_idle_stop_seconds: 300
```
