# 08 · 服务接口契约速查

> P0 阶段需冻结的接口清单。此处只列**契约边界**，不含完整字段定义（各服务实现时用 Pydantic / Go struct 明确）。
>
> 通用约定：
> - 所有时间戳 ISO 8601 UTC 带 `Z`
> - 所有错误响应含 `{code, message, trace_id}`，`code` 为平台错误码（如 `ENVIRONMENT_BUSY`）
> - `/internal/*` 端点由 `X-Internal-Token` 保护，**未配置密钥则端点整体禁用**（fail closed，继承现有约定）
> - 所有服务间调用透传 `X-Trace-ID`、`X-Cell-ID`、`X-User-ID`、`X-Tenant-ID`

---

## 1. identity-service（Go，全局）

### 对外（经 edge-gateway 代理）

| Method | Path | 说明 |
|--------|------|------|
| POST | `/auth/register` | 注册（公开） |
| POST | `/auth/login` | 登录（公开）→ 返回含 `cell_id` / `perms_version` 的 JWT |
| POST | `/auth/select-tenant` | 多租户选择（接受 pre-auth token） |
| POST | `/auth/refresh` | 刷新（公开） |
| POST | `/auth/logout` `/auth/change-password` | 认证后 |
| GET | `/auth/memberships` | 我的租户列表 |
| GET/POST | `/auth/members` | 租户成员管理（`tenant:member:*`） |
| GET | `/auth/tenant-members` | 成员选择器用的最小目录 |
| GET | `/me` | 当前用户（含 `timezone` / `locale`） |
| PATCH | `/me/preferences` | 修改时区与语言 |
| GET/POST/PUT/DELETE | `/rbac/permissions` `/rbac/roles` | 权限目录（只读）与角色 |
| GET/POST/DELETE | `/workspaces/{id}/members` | 工作区成员绑定 |

### 内部

| Method | Path | 调用方 | 返回 |
|--------|------|--------|------|
| GET | `/internal/authz/resolve?user_id&tenant_id` | edge-gateway、control-service、任何非 HTTP 入口 | `{permissions[], workspace_roles{}, perms_version}` |
| GET | `/internal/tenants/{id}/placement` | 全局边缘、control-service | `{cell_id, target_cell_id, migration_state}` |
| POST | `/internal/tenants/{id}/placement` | 运维/放置策略 | 设置或迁移 Cell |
| GET | `/internal/cells` | 运维 | Cell 清单与容量 |

**JWT claims 契约（冻结）**：

```
{
  "sub":            user_id,
  "tenant_id":      string,
  "cell_id":        string,          // 全局边缘路由依据，热路径零查库
  "perms_version":  int,             // 授权缓存失效依据
  "iat": ..., "exp": ...
}
```

---

## 2. control-service（Python，每 Cell）

### 对外

| Method | Path | 说明 |
|--------|------|------|
| GET/POST/DELETE | `/api/v1/workspaces` `/api/v1/workspaces/{id}` | 工作区注册表 |
| POST | `/api/v1/workspaces/bind-git` `/sync` `/{id}/open` | Git 绑定与打开 |
| GET | `/api/v1/workspaces/{id}/remote-branches` | 远端分支 |
| GET/POST | `/api/v1/workspaces/{id}/environments` | 环境列表 / 创建 |
| GET/DELETE | `/api/v1/environments/{envId}` | 环境详情 / 销毁 |
| POST | `/api/v1/environments/{envId}/open` | 打开环境（触发租约） |
| GET | `/api/v1/environments/{envId}/events` | 环境事件流（审计） |
| GET/POST/PUT/DELETE/PATCH | `/api/v1/git/credentials/*` | Git 凭证（加密存储） |
| GET | `/api/v1/sqlmesh/apply/history` `/apply/history/{recordId}` | Plan/Apply 历史 |
| GET | `/api/v1/knowledge/documents/list` | 知识库元数据 |
| GET | `/api/v1/quotas` | 我的租户配额与用量 |
| POST | `/api/v1/reconcile` | 手动触发对账（管理员） |

### 内部

| Method | Path | 调用方 | 说明 |
|--------|------|--------|------|
| POST | `/internal/leases/acquire` | edge-gateway | `{lease_key}` → `{runtime_id, addr, warmup}`；容量不足返回 429 |
| POST | `/internal/leases/release` | workspace-runtime、运维 | 主动释放 |
| GET | `/internal/leases` | 运维、reconciler | 全 Cell 租约快照 |
| POST | `/internal/tasks/submit` | workspace-runtime | 提交任务给 task-runner（也可由 runtime 直接调 K8s，取决于实现） |
| GET | `/internal/tasks/{task_id}` | 提交方 | 任务状态 |
| GET | `/internal/deployments/desired` | orchestrator 的 deployment 控制器 | 应该 warm 的 deployment 列表 |

---

## 3. workspace-runtime（Python，工作区亲和）

**所有 `/rt/v1/*` 端点必须携带能解析出 `lease_key` 的参数**（`workspace_path` 或 `workspace_id`+`environment`），且**只服务本实例持有租约的工作副本**；否则返回 409 `LEASE_NOT_HELD`（网关据此重新路由）。

### SQLMesh

| Method | Path |
|--------|------|
| GET | `/rt/v1/models` `/lineage` `/schemas` `/gateways` `/gateways/ping` `/engine/environments` `/meta` `/modules` |
| POST | `/rt/v1/plan` `/apply` `/apply/async` `/cancel` `/render` `/validate` `/evaluate` `/query` `/test` `/format` |
| GET | `/rt/v1/table-diff` |
| POST | `/rt/v1/schema/discover` `/external-models/import` |
| POST | `/rt/v1/sqlmesh/environments/{env}/{create,destroy,promote}` |

### Git / 文件 / 日志 / 知识库 / 语义层

| Method | Path |
|--------|------|
| GET | `/rt/v1/git/{status,init-status,branches,log,commit-files,commit-file-diff}` |
| POST | `/rt/v1/git/{init,stage,unstage,commit,push,pull,checkout,publish,merge}` |
| GET/POST | `/rt/v1/files/{read,list}` · `/files/{write,delete,mkdir,rename}` |
| GET | `/rt/v1/logs` `/logs/errors` `/logs/errors/{id}` `/logs/stream`（SSE） |
| GET/POST | `/rt/v1/knowledge/documents/{read}` · `/documents/{create,upload,delete}` |
| GET | `/rt/v1/ontology/*` （语义层全部读端点） |

### Dagster（数据面）

| Method | Path |
|--------|------|
| GET | `/rt/v1/dagster/{assets,deployments,deployments/diff,mode,health}` |
| POST | `/rt/v1/dagster/deployments/{ensure,reload}` · `DELETE /deployments/{name}` |
| GET/POST | `/rt/v1/dagster/runs/{id}` `/runs/{id}/logs` · `/pipelines/run` `/runs/{id}/cancel` |
| GET/POST | `/rt/v1/dagster/schedules` `/schedules/{name}/{start,stop}` |

### Agent 工具（agent-service 专用）

| Method | Path | 隔离级别 |
|--------|------|---------|
| POST | `/rt/v1/tools/read` `/tools/list` `/tools/search` | L1（runtime 内） |
| POST | `/rt/v1/tools/write` `/tools/patch` `/tools/create` | L1（runtime 内） |
| POST | `/rt/v1/tools/shell` | **L3**（转 task-runner） |

### 租约与内部

| Method | Path | 调用方 |
|--------|------|--------|
| POST | `/rt/v1/leases` | control-service（授予租约） |
| DELETE | `/rt/v1/leases/{lease_key}` | control-service（回收） |
| GET | `/rt/v1/leases` | 运维（本实例租约清单） |
| POST | `/rt/v1/drain` | LeaseController（优雅下线） |
| GET | `/internal/code-locations` | orchestrator 的 workspace-sync |
| POST | `/internal/code-locations/heartbeat` | 同上（回执已加载 revision） |
| POST | `/rt/v1/tasks/{task_id}/progress` `/result` | task-runner 回调 |
| GET | `/health` `/metrics` | K8s probe / Prometheus |

---

## 4. agent-service（Python，无状态）

对外契约**不变**（前端与 LLM 工具 schema 保持兼容）：

| Method | Path |
|--------|------|
| GET/POST | `/api/v1/sessions` `/sessions/{id}` |
| GET | `/api/v1/sessions/{id}/messages` `/sessions/{id}/events`（SSE） |
| POST | `/api/v1/sessions/{id}/{chat,mode,approve,cancel}` |
| GET | `/api/v1/models` |

**变化**：所有文件/执行类工具的实现从进程内改为调 `workspace-runtime` 的 `/rt/v1/tools/*`。工具的 LLM 侧 schema（名称、参数、描述）**不变**。

---

## 5. task-runner（Python，一次性）

不是 HTTP 服务，是**一次性进程**。契约通过启动参数与回调定义：

### 输入（任务 spec，经 stdin JSON 或挂载文件）

```jsonc
{
  "task_id": "tsk_...",
  "kind": "sqlmesh.apply",              // 决定 isolation profile
  "isolation": "L2_container",
  "workspace_path": "/workspace",       // 容器内的挂载点（唯一可写业务目录）
  "environment": "dev_alice",
  "params": { /* kind-specific */ },
  "callback_url": "http://{runtime_pod_ip}:8010/rt/v1/tasks/{task_id}",
  "timeout_seconds": 3600
}
```

### 输出

| 通道 | 内容 |
|------|------|
| 进度回调 | `POST {callback_url}/progress` — 增量日志与阶段（用于 SSE 透传） |
| 最终结果 | `POST {callback_url}/result` — `{status, exit_code, result, error}` |
| 退出码 | 0 = 成功；非 0 = 失败（runtime 兜底认定） |

### 环境变量（唯一注入项）

```
DATACODE_TASK_TOKEN               scoped：单任务 · 单工作副本 · TTL=任务时长
DATACODE_RUNTIME_CALLBACK_URL
DATACODE_WAREHOUSE_*              本工作副本配置的仓库凭证（起步方案；M2 改 proxy sidecar）
HTTPS_PROXY / HTTP_PROXY          指向 egress-proxy（唯一出网通道）
```

**明确不注入**：`DB_PASSWORD` / `REDIS_PASSWORD` / 任何 LLM key / `TOKEN_ENCRYPTION_SECRET` / 任何 `*_INTERNAL_TOKEN` / K8s ServiceAccount token。

---

## 6. edge-gateway 路由与目标映射（冻结）

| 路由前缀 | 目标 | lease-aware | 权限点来源 |
|---------|------|------------|-----------|
| `/api/v1/auth/*` `/me` `/rbac/*` | identity-service | ❌ | routemap（部分公开） |
| `/api/v1/workspaces/*`（除 environments 之外的元数据） | control-service | ❌ | `tenant:workspace:*` `workspace:member:*` |
| `/api/v1/workspaces/{id}/environments` | control-service | ❌ | `workspace:env:*` |
| `/api/v1/environments/*` | control-service | ❌ | `workspace:env:*` |
| `/api/v1/git/credentials/*` | control-service | ❌ | `workspace:code:*` |
| `/api/v1/quotas` | control-service | ❌ | 认证即可 |
| `/api/v1/sqlmesh/*` | **workspace-runtime** | ✅ | `workspace:code:*` `workspace:env:apply` |
| `/api/v1/git/*`（除 credentials） | **workspace-runtime** | ✅ | `workspace:code:*` |
| `/api/v1/files/*` | **workspace-runtime** | ✅ | `workspace:code:*` |
| `/api/v1/ontology/*` | **workspace-runtime** | ✅ | `workspace:code:read` |
| `/api/v1/knowledge/*`（文件内容） | **workspace-runtime** | ✅ | `workspace:knowledge:*` |
| `/api/v1/logs/*` | **workspace-runtime** | ✅ | `workspace:logs:read` |
| `/api/v1/dagster/*` | **workspace-runtime** | ✅ | `workspace:asset:*` |
| `/api/v1/sessions/*` `/models` | agent-service | ❌ | 会话归属校验 |

**lease-aware 路由的前置条件**：请求必须能解析出 `lease_key`。网关对 lease-aware 路由**强制要求** `workspace_path`（或 `workspace_id`+`environment`）参数，缺失即 400 —— 这同时是安全要求（继承现有 `LogsValidatedRoutes` 的判断：*"a log query without a resolvable workspace has no legitimate meaning"*）。

---

## 7. 新增/变更的平台错误码

| Code | HTTP | 场景 |
|------|------|------|
| `LEASE_NOT_HELD` | 409 | 请求打到了不持有该工作副本租约的 runtime；网关应重新路由 |
| `LEASE_UNAVAILABLE` | 429 | Cell 内无可用租约容量；带 `Retry-After` |
| `LEASE_SPLIT_BRAIN` | 409 | 文件锁仲裁失败（Redis 降级期）；调用方重试 |
| `WORKSPACE_MATERIALIZING` | 202 | 工作副本正在物化，稍后重试 |
| `WORKSPACE_RELOCATING` | 503 | runtime 正在 drain，工作副本迁移中；带 `Retry-After` |
| `QUOTA_EXCEEDED` | 409 / 429 | 租户配额超限（`detail.resource` 指明维度） |
| `SANDBOX_UNAVAILABLE` | 503 | 沙箱容器/executor 无法拉起（节点池容量 / 镜像拉取失败 / fork 失败） |
| `SANDBOX_TIMEOUT` | 504 | 任务超过 `timeout_seconds`（容器）或 `RLIMIT_CPU`（L3-proc）被 kill |
| `SANDBOX_OOM` | 507 | 任务被 OOMKill（容器）或触及 `RLIMIT_AS`（L3-proc） |
| `EGRESS_DENIED` | 403 | 沙箱出网被出口代理拒绝（`detail.destination`） |
| `CELL_MAINTENANCE` | 503 | 该 Cell 正在维护/迁移；带 `Retry-After` |
| `INSUFFICIENT_STORAGE` | 507 | 工作副本磁盘配额不足 |
| `STATE_SCHEMA_MISMATCH` | 409 | **保留现有契约**（顶层内联 `gateway`/`expected`/`actual`/`repair_endpoint`） |
| `ENVIRONMENT_BUSY` | 409 | **保留现有契约**（环境写锁被占；2.0 下变为进程内锁） |

> **注**：原草案中的 `PYTHON_MODEL_NOT_SUPPORTED` **已删除** —— Q1 决策明确保留 SQLMesh Python model / 自定义 macro 能力，不存在"校验期拒绝"这条路径。含 Python model 的工作副本改为路由到 L3 档位执行（§04.3.6）。

---

## 8. P0 已冻结的配置键

| 键 | 默认 | 可配置性 | 影响 |
|----|------|---------|------|
| `lease.max_per_instance` | 16 | ✅ **可配置**（Q3） | 硬上限；实际准入还受 `memory_budget_bytes` 与真实 RSS 约束 |
| `lease.memory_budget_bytes` | 6.5 GiB | ✅ 可配置 | **真正的容量驱动项**；改 Pod 规格即改容量，无需改码 |
| `lease.estimated_lease_bytes` | 210 MiB | ✅ 可配置 + **运行期 EWMA 自适应** | P0 需按小/中/大三种仓库实测设定初值 |
| `lease.admission_rss_high_watermark` | 0.80 | ✅ 可配置 | 估计值失准时的兜底防 OOM |
| `lease.ttl_seconds` | 120 | ✅ | crash 后租约接管延迟 |
| `lease.idle_release_seconds` | 1800 | ✅ | 资源回收积极度 vs 重新物化开销 |
| `lease.drain_apply_grace_seconds` | 600 | ✅ | 优雅下线时长上限 |
| `materialize.disk_high_watermark_percent` | 80 | ✅ | LRU 回收触发点 |
| `materialize.wip_snapshot_interval_seconds` | **600** | ✅ 可配置，但**是对外契约**（Q6） | 非优雅崩溃的最大数据丢失窗口；改动即改变产品承诺 |
| `materialize.wip_snapshot_skip_if_clean` | true | ✅ | 空闲租约零开销 |
| `executor.mode` | `process`（P3）→ `container`（P6） | ✅ | L3 执行体形态；协议不变 |
| `executor.isolation_profile` | `L3_proc` | ✅ | P3 的隔离档位 |
| `executor.idle_stop_seconds` | 900 | ✅ | executor 回收积极度 |
| `sandbox.enabled` | false（P3）→ true（P6） | ✅ | 是否启用 per-lease warm sandbox 容器 |
| `sandbox.profile` | `L3_base`（**非** `L3_strong`，Q2） | ✅ | 容器隔离档位 |
| `codeloc.mode` | `subprocess`（dev）/ `pod`（prod） | ✅ | code location 部署形态 |
| `codeloc.warm_idle_stop_seconds` | 300 | ✅ | code location Pod 回收积极度 |
| `cell.capacity_tenants` | 200 | ✅ | Cell 规格 |
| `default_profile` | `strongest_enabled` | ⚠️ **不可改为固定弱档** | 安全默认：解析顺序 L3_strong → L3_base → L3_proc |
| `profiles.L3_strong.enabled` | **false**（Q2） | ✅ | 启用需集群支持 gVisor/Kata + 独立节点池 |

**P0 决策对配置的三条硬约束**：

1. **`default_profile` 必须是 `strongest_enabled`**，不得配成 `L1` 或 `L2` —— 那两档假设输入无法执行宿主代码，用于任意代码即无隔离。
2. **`L2_container` 是唯一允许 `warehouse: scoped_readwrite` 的档位**（Q5 派生）。任何 L3 档位配成可写凭证即破坏"用户代码不能绕过 SQLMesh 改仓库"这一保证。
3. **`wip_snapshot_interval_seconds` 变更需同步更新产品文档**（Q6）—— 它是用户可见的持久性承诺，不是纯性能调参。
