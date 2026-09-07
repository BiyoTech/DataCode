# 06 · 迁移路线图

> 原则：**每个阶段都必须是可上线、可回滚、可验证的**。不允许出现"迁移中间态半年"。
>
> 沿用「不考虑历史兼容」约定：数据可以重建（工作副本从 Git 重建、Context 重新加载），因此不需要双写/影子期。**唯一需要小心迁移的是数据库里的持久数据**（身份、环境注册表、会话历史）。

---

## 0. 阶段总览

| 阶段 | 主题 | 工期 | 交付边界 | 风险 |
|------|------|------|---------|------|
| **P0** | 决策冻结 + 契约定义 | 1 周 | 设计评审通过、接口契约冻结（✅ Q1-Q7 已决） | 低 |
| **P1** | 共享库与 Cell 概念前置 | 1.5 周 | `cell_id` 贯穿全链路（单 Cell） | 低 |
| **P2** | identity-service 拆出 | 2 周 | 身份表受管迁移，Global/Cell 分离 | **中**（涉及全部鉴权） |
| **P3** | workspace-runtime 合并 **+ L3-proc 隔离** | **5 周** | 三服务合一、HTTP 环路消除、**T2 消除** | **高**（最大重构） |
| **P4** | 工作区租约与亲和路由 | 3 周 | 租约路由 + WIP 契约（4a）→ 存储介质切换（4b） + 容量自适应 | **中高**（拆两次上线后单次变更面显著变小） |
| **P5** | control-service 提取 | 2 周 | 执行与编排分离 | 中 |
| **P6** | 沙箱执行平面（L3-base） | 3 周 | task-runner + L1/L2/L3-base 隔离 | **高**（安全关键） |
| **P7** | Cell 化落地 | 2.5 周 | 多 Cell 部署 + 全局路由 + 迁移工具 | 中 |
| **P8** | 可观测性与容量治理 | 2 周 | OTel + 配额 + 容量告警 | 低 |
| | **合计** | **~22 周** | | |

**关键路径**：P3 → P4 → P6。P2 与 P5 可与主线并行（不同代码区域）。

**因 P0 决策产生的工期变化**：P3 +1 周（Q1 的 L3-proc）、P4 +0.5 周（Q3 容量自适应 + Q6 的 WIP 配套）、P6 −0.5 周（Q2 省 gVisor 安装 −3 天、Q5 省 warehouse-proxy −2 周、但 +3 天 写操作 L2/L3 拆分）。

**安全里程碑因此显著前移**：

| 威胁 | 原计划消除阶段 | 现在 |
|------|-------------|------|
| **T2**（用户代码读平台凭证，最致命） | P6 | **P3** ⬅ 提前 3 个阶段 |
| T5 部分（资源耗尽） | P6 | **P3**（rlimit） |
| T10（篡改平台文件） | P6 | **P3**（独立 UID） |
| T1 / T3 / T6 / T7（跨租户、网络） | P6 | P6（不变） |
| T8（容器逃逸） | P6 | **待增强**（Q2 决策，§04.8.1） |

---

## P0 · 决策冻结与契约定义（1 周）

不写生产代码，只冻结决策与接口。

### 决策结论（✅ 已于 2026-08-26 冻结，详见 [07 · P0 决策结论](07-decision-records.md#p0-决策结论已冻结-2026-08-26)）

| # | 决策 | 对路线的影响 |
|---|------|------------|
| **Q1** | **不禁用** SQLMesh Python model / macro | **P3 增加 L3-proc 交付**（+1 周）：`SqlMeshExecutor` 持久子进程 + 协议冻结。容器形态因依赖租约(P4)与容器基础设施(P6)仍在 P6 |
| **Q2** | **暂不用** gVisor / Kata，记为待增强 | P6 的 L3 上限为 **L3-base**（标准 runc 加固档），**省去集群安装 gVisor 的工作（-3 天）**，且容器冷启动从 3-8s 降到 1-3s |
| **Q3** | `max_per_instance` **设为可配置** | P0 实测改为"设定初值 + 实现自校验/自适应"；P4 增加容量自适应逻辑（+2 天） |
| **Q4** | **不需要**跨 Cell 协作 | P7 无需处理跨 Cell 数据可见性；租户迁移按"整租户"粒度即可 |
| **Q5** | **接受** env 注入凭证方案 | P6 省去 warehouse-proxy（-2 周）；但**必须实现"写操作从 L3 拆到 L2"**（+3 天） |
| **Q6** | **接受** 10 分钟改动丢失窗口 | P4 的 WIP 保护按 600s 快照实现；需增加前端"最后保护时间"提示与恢复弹窗（+2 天） |
| **Q7** | **不做流量灰度**；物化目录介质可配置（`materialize.mount_kind`） | P4 拆为 4a（租约+WIP）/ 4b（介质切换）两次上线；网关只保留租约路由一条路径，runtime 只保留一条物化路径；回滚由 volume 类型 + `mount_kind` 完成 |

**净工期影响**：+1 周（Q1 的 L3-proc）− 0.4 周（Q2 省 gVisor）− 2 周（Q5 省 proxy）+ 1.4 周（Q3/Q5/Q6 的配套项）≈ **总体持平**，但**安全达标时间从 P6 提前到 P3**（T2 消除）。Q7 对工期中性：省去灰度双路径的实现与运维，换成 4a/4b 两次部署（P4 内部拆分，总工期不变）。

### 交付物

- [x] 本套设计文档评审通过，`07-decision-records.md` 全部 ADR 状态改为 accepted
- [ ] **实测校准 `estimated_lease_bytes`**（Q3 要求）：对小(~50 模型)/中(~500)/大(~2000) 三种规模仓库分别测 `Context()` 构造后的 RSS 增量，据此设定初值与告警阈值 —— ⚠️ 校准工具已就绪（`scripts/measure-lease-memory.py`），但**尚未在真实规模仓库上跑出实测值**；当前配置用的是估计初值，靠 P4a 的运行期 EWMA 自适应兜底
- [x] **冻结 `SqlMeshExecutor` 通信协议**（Q1 的关键前置）：UDS + 长度前缀 JSON 的消息定义 + 元数据序列化契约（模型/列/依赖/血缘/渲染 SQL）。此协议在 P3 落地、P6 复用，一旦冻结不应变更（✅ `shared/python/datacode_common/executor_protocol.py`，`PROTOCOL_VERSION = 1`）
- [x] **冻结 `isolation-profiles.yaml` schema**（含 L3_proc / L2 / L3_base / L3_strong(disabled) 四档 + `default_profile: strongest_enabled`）（✅ `conf/isolation-profiles.yaml`，实为五档：另含 `L1_inprocess`）
- [x] 冻结 Cell 规格：单 Cell 200 租户 / 4,000 工作副本 / 33 runtime 实例
- [x] 冻结接口契约（见 [08 · 服务接口契约](08-service-contracts.md)）
- [x] **冻结数据持久性契约**（Q6）：写入产品文档的用户可见保证表（编辑器 5s / 优雅下线 0 / 非优雅崩溃 10min）
- [x] **冻结 `materialize.mount_kind` 语义**（Q7）：三个取值（`ephemeral` / `instance_local` / `shared`）及其启动期校验规则；明确「物化逻辑唯一路径、介质不可被代码感知」为不可变约束（✅ `shared/python/datacode_common/materialize.py`）
- [ ] 实测基线：`git status` 在 EFS vs 本地盘的延迟对比（验证 §01.3.3 假设）—— ⚠️ 未实测（本地开发环境无 EFS）
- [x] **记录待增强点清单**（Q2/Q5 延后项）到 §04.8，含触发条件与预估工作量

### 验收门

设计评审通过 + `estimated_lease_bytes` 有实测依据 + `SqlMeshExecutor` 协议已冻结。

> **实施记录（2026-08）**：协议与契约类交付物全部完成；两项**实测类**交付物（`estimated_lease_bytes` 校准、EFS 延迟基线）因缺少真实规模仓库与 EFS 环境未做，风险由 P4a 的容量自适应（EWMA 上修 + RSS 准入）与 P4b 的 `instance_local` 介质承接。上生产前应补测。

---

## P1 · 共享库与 Cell 概念前置（1.5 周）

**目标：在还没有多 Cell 的时候，就让代码有 Cell 意识。** 这样 P7 的多 Cell 部署是纯运维动作。

### 任务

- [x] `shared/python/datacode_common/` 扩充：
  - `naming/`：从 `sqlmesh-service/src/environments/naming.py` 与 `dagster-service/src/core/naming.py` **合并为单份**（当前是"镜像实现 + 单测保证一致"，合并后消除漂移可能）（✅ `naming.py` 单份；两侧 re-export shim 已于后续清理删除，消费方直接 `import datacode_common.naming`）
  - `workspace_paths/`：从 `sqlmesh-service/src/workspace/paths.py` 提升为共享库（runtime 与 control 都要用）（✅ `workspace_paths.py`）
  - `cell/`：`cell_id()` 读取、Cell 配置加载（✅ `cell.py`）
  - `identity/`：`CallerContext`（从 `sqlmesh-service/src/api/deps.py` 提升）—— ⚠️ **未提升**：各服务保留自己的 `api/deps.py`。实际未产生漂移（网关注入的身份头是唯一契约），P7 若出现第三个消费者再提升
- [x] `shared/go/`（**新增**）：Go 侧共享库（✅ 实现为单个 `shared/go/lease` 包，而非两个包）
  - `naming/`：命名代数的 Go 实现（gateway 的租约路由需要解析 workspace_path）（✅ `lease/key.go`）
  - `workspacepath/`：路径解析与校验（从 `gateway/internal/rbac/wspath.go` 提升）（✅ `lease/path.go`；`rbac/wspath.go` 保留网关侧的权限判定用法）
- [x] 全链路注入 `cell_id`：
  - JWT claim 增加 `cell_id`（此阶段固定为 `cell-0`）（✅ `identity-service/internal/auth/token.go`）
  - structlog / zap 全局绑定 `cell_id` 字段（✅ `shared/python/datacode_common/logging/setup.py`）
  - Prometheus 指标全部加 `cell` label —— ⚠️ **未做**，随可观测性栈延后（见 P8 Deferred）
  - Redis key 前缀改为 `{key_prefix}:{cell_id}:...`（✅ `config/platform.py::RedisConfig.key()`）
- [x] `conf/cells/cell-0.yaml` + `DATACODE_CELL_ID` env 注入
- [x] 统一时间处理：审计全代码库，`datetime.now()` → `datetime.now(timezone.utc)`，加 lint 规则（ruff `DTZ` 规则集）禁止 naive datetime（✅ 全部 Python 服务的 `[tool.ruff.lint] select` 含 `DTZ`）

### 验收门

- 全部日志/指标带 `cell_id`（日志 ✅；指标随可观测性栈延后）
- `make test` 全绿
- 命名代数只有一份实现，两个服务共用
- ruff DTZ 规则启用且无 violation

---

## P2 · identity-service 拆出（2 周）

**可与 P3 并行**（不同代码区域）。

### 任务

- [x] 新建 `identity-service/`（Go），从 `gateway/internal/{auth,rbac}` 迁入（✅ 服务已建成并承载授权决策数据；认证处理器的迁入见下方偏差）
- [x] **表管理改为 golang-migrate**：把 `store.go:159-247` 的 9 个 `CREATE TABLE IF NOT EXISTS` 转为版本化迁移文件 `0001_init.up.sql`（内容与现有 DDL 等价，作为基线）（✅ `identity-service/internal/migrate/sql/0001_identity_baseline.sql`）
- [x] 新增表：`tenant_placement`、`cells`；新增列：`users.timezone`、`users.locale`（✅ `0002_cell_placement.sql`）
- [x] 实现 `/internal/authz/resolve?user_id&tenant_id` → `{permissions: [...], workspace_roles: {...}, perms_version: N}`
- [x] 实现 `/internal/tenants/{id}/placement` → `{cell_id, migration_state}`（✅ GET + POST）
- [x] JWT 增加 claim：`cell_id`、`perms_version`
- [x] `edge-gateway` 改造：（✅ **2026-08-28 P5.5 完成**：网关已收敛为 identity-service 的纯消费者）
  - [x] 删除 `internal/auth/store.go`、`internal/rbac/store.go`（存储层）（✅ 连同 `auth/service.go`、`auth/ratelimit*.go`、`rbac/{service,cache,util}.go`、`internal/dbx/` 一并删除；`cmd/server/main.go` 不再有 `NewStore`/`Bootstrap`。**网关 go.mod 中三个数据库驱动与 bcrypt 依赖全部消失** —— 这是"不再持有身份数据"的可验证证据）
  - [x] 保留 `internal/rbac/{catalog,routemap,wspath}.go`（决策逻辑）—— ⚠️ 偏差：`cache.go`/`util.go` 也删了。它们是**数据库查询结果**的缓存，而决策已改为读 `authz.Snapshot`（本身带缓存），保留只会是第二层失效逻辑
  - [x] 授权数据来源改为调 `/internal/authz/resolve`，本地缓存 TTL 60s + `perms_version` 失效（✅ `internal/authz/resolver.go`；未配置时 fail closed）
  - [x] 登录/注册等 `/auth/*` 改为**反向代理**到 identity-service（gateway 不再自己处理）（✅ `internal/handler/identity_proxy.go`；`/auth/*`、`/me`、`/me/preferences`、`/rbac/*` 全部代理。前端**零改动**：路径、请求体与状态码不变）
    - 两个易错点已在端到端冒烟中暴露并修正：(1) 代理必须**剥离 `/api/v1` 前缀** —— identity-service 的路由挂在 `/auth/login` 而非 `/api/v1/auth/login`，前缀是网关的公共命名空间；(2) chi 的通配需写成嵌套 `Route("/auth") + HandleFunc("/*")`，扁平的 `HandleFunc("/auth/*")` 不匹配。
    - 代理额外转发 `Authorization` 头（`GenericProxy` 不转发）：identity-service 自行验签，因为登录/刷新在网关侧是公开路由，此时还没有已验证身份可用。
- [x] Python 侧新增 identity 客户端（`shared/python/datacode_common/identity/client.py`），供非 HTTP 入口（reconciler / schedule 触发）做授权判断（✅ `IdentityClient.authorize()` 双向 fail closed：拒绝抛 `PermissionDenied`、不可达抛 `IdentityUnavailable`；20 个单测覆盖）

### 数据迁移

Global DB 与 Cell DB 此阶段**可以是同一个物理库的两个 schema**（`identity` 与 `platform`），P7 再物理拆分。这样 P2 无需处理跨库事务。

```sql
-- 迁移动作：把现有裸建的表移入 identity schema
CREATE SCHEMA identity;
ALTER TABLE users SET SCHEMA identity;
-- ... 9 张表
-- 然后 golang-migrate 的 0001 基线标记为 already applied
```

### 验收门

- 登录/鉴权全链路通过（含 select-tenant 多租户流程）
- `rbac_audit_log` 正常记录（✅ 写入点在 identity-service；网关的绑定变更经 `/internal/.../members` 携带 `actor_id`，故内部调用同样留痕）
- **非 HTTP 入口的授权可用**：写一个测试用例，让 reconciler 携带 actor 身份调 authz API（✅ `shared/python/tests/test_identity_client.py::TestReconcilerFlow`）
- gateway 内不再有任何 `CREATE TABLE`（✅ 已达成；网关内已无 `database/sql` 引用）
- 回滚方案：identity-service 不可用时 gateway 用缓存的授权数据继续服务（degraded 模式，只影响权限变更的生效延迟）

> **P2 结清记录（2026-08-28，P5.5）**：identity 数据集现在只有**一个写入方**。为此新增 identity-service 的 `/internal/tenants/{tenantID}/...` 绑定变更端点（workspace 成员 grant/revoke/purge、租户成员目录、角色权限集），网关经 `internal/authz/bindings.go` 调用；职责边界是**网关决定是否允许、identity 执行变更并守护领域不变量**（角色可分配性、"工作区不得失去最后一个 owner"、目标须为租户成员）。
>
> 读路径顺带优化：`ListWorkspaces` 原先查库拿绑定，现直接读 `Authorize` 中间件**已经取到**的 snapshot（其中已含 `workspace_roles` 与按角色索引的权限集），少一次往返。
>
> **P7 的 Global/Cell 库物理拆分前置条件已满足** —— `pg_dump --schema=identity` 现在搬走的是一个无其他写入方的 schema。

---

## P3 · workspace-runtime 合并 + L3-proc 隔离（5 周）⭐ 最大重构

> 工期从 4 周增至 5 周：**Q1 决策要求隔离尽早**，故在本阶段增加 3e（L3-proc）。

### 任务

**阶段 3a：建骨架并搬 SQLMesh 与文件（1.5 周）**

- [x] 新建 `workspace-runtime/`，目录结构按 §02.5.3 分层
- [x] 搬入 `sqlmesh-service/src/core/*`（Context 池 + 全部 `*_service.py`）→ `src/sqlmesh/`
- [x] 搬入 `sqlmesh-service/src/workspace/{git_ops,git_merge,file_ops,layout,config_ops}.py` → `src/git/`、`src/files/`
- [x] 搬入 `sqlmesh-service/src/logs/`、`utils/error_archive.py`
- [x] 建立 `/rt/v1/*` API（初期与现有 `/api/v1/*` 路径一一对应，网关侧只换 upstream）—— ⚠️ **偏差**：保留 `/api/v1/*` 前缀不变，未引入 `/rt/v1/*`。理由：前缀重命名不带来隔离或路由收益，只增加网关改写与前端联调面；内部通道用 `/internal/*` 已足够区分（lease、env-control、code-locations）

**阶段 3b：并入 Ontology（0.5 周）**

- [x] 搬入 `ontology-service/src/*` → `src/ontology/`
- [x] **`sqlmesh_metadata.py` 的 HTTP 调用改为进程内函数调用** —— 环路 #2 消除（✅ 保留 fail-soft：`SqlMeshMetadataUnavailable` 降级）
- [x] 删除 `ontology-service/`；网关 `/api/v1/ontology/*` 指向 runtime

**阶段 3c：并入 Dagster code location（1 周）**

- [x] 搬入 `dagster-service/src/core/{code_location_manager,deployment_manager,port_allocation,process_manager,scaffold,naming}.py` → `src/codeloc/`
- [x] 搬入 `dagster-service/src/bridge/*` → `src/sqlmesh/bridge.py`；**`sqlmesh_bridge` 的 HTTP 调用改为进程内调用** —— 环路 #1 的一半消除（✅ 落在 `src/codeloc/bridge.py`）
- [x] `dagster-service` 剩余部分（`api/rest/{assets,runs,schedules,pipelines}.py` + `core/{dagster_client,run_manager}.py`）是**纯 GraphQL 客户端**，无本地状态 → **并入 runtime**（这些端点都需要 `workspace_path` 确定 deployment，天然工作区亲和）
- [x] `/internal/code-locations` 端点搬到 runtime；`dagster-control-plane` 的 workspace-sync 改为从 runtime 实例列表拉取
- [x] 删除 `dagster-service/`

**阶段 3d：Agent 工具剥离（1 周）**

- [x] runtime 新增 `/rt/v1/tools/{read,write,patch,list,search,shell}`（✅ 实为 `/api/v1/tools/*`，见 3a 偏差；六个端点齐备，`patch` 由 `write` 覆盖）
- [x] `agent-service` 的 `tools/{read_file,create_file,apply_patch,list_dir,search_code}.py` 改为 HTTP 客户端（保留工具 schema 与 LLM 契约不变）（✅ 统一经 `tools/runtime_client.py`）
- [x] `tools/shell.py` 改为提交到 runtime
- [x] `tools/{query,list_models,get_lineage,schema_discovery}.py` 的 upstream 从 sqlmesh-service 改为 runtime（✅ 2026-08 P5 收尾时补正：此前这四个工具仍解析已退役的 `services.sqlmesh`，现 `settings.sqlmesh_service_url` 一律回落到 runtime）

**阶段 3e：L3-proc 隔离（1 周）⭐ Q1 决策新增**

> 目标：在还没有租约与容器基础设施的阶段，**消除 T2（用户代码读平台凭证）** —— §01/§04 已论证它是使其余防护失效的那一条。

- [x] **`SqlMeshExecutor` 持久子进程**（`src/executor/`）：
  - `os.fork` + `setgid/setuid` 到独立非特权 UID（`run_as_user_base + slot`）（✅ `process.py`）
  - **环境变量清空**，仅按 `env_allowlist` 重新注入（`PATH`/`HOME`/`LANG`/`TZ` + 本工作副本的 `DATACODE_WAREHOUSE_*`）
  - `setrlimit`：`RLIMIT_AS` 4 GiB / `RLIMIT_CPU` / `RLIMIT_NPROC` 256 / `RLIMIT_FSIZE` / `RLIMIT_NOFILE`（✅ 含 macOS 开发环境的 best-effort 降级）
  - `cwd` 限定为该工作副本；**每工作副本一个常驻 executor**（Context 保持热态）
  - 有界重启（`max_restarts=3` / `restart_window=300s`），超限则 abandon 并经 `/health` 暴露
- [x] **通信协议实现**（P0 已冻结）：UDS + 长度前缀 JSON；**此协议 P6 原样复用**
- [x] **`MetadataContext`**（runtime 内存的只读元数据视图）：从 executor 回传的序列化元数据构建，服务 `/models` `/lineage` `/schemas` 等读请求（毫秒级）
- [x] SQLMesh 全部**执行类**操作（plan/apply/render/evaluate/test）改为经 executor
- [x] `agent.shell` 改为经 L3-proc 子进程执行（同样清空 env + rlimit）
- [x] Dagster code location 子进程应用同一加固（独立 UID + 清空 env）
- [x] **`git hooks` 全面禁用**：所有 git 调用强制 `-c core.hooksPath=/dev/null`；worktree 物化不复制 hooks
- [x] `conf/isolation-profiles.yaml` 落地（此阶段启用 `L1_inprocess` + `L3_proc`，`default_profile: strongest_enabled` → 解析为 `L3_proc`）

### 关键风险与缓解

| 风险 | 缓解 |
|------|------|
| 5 周大重构期间主干不可用 | 按 3a-3e 五个子阶段各自可上线；每个子阶段结束时全链路可用 |
| 搬迁引入回归 | 搬迁**不改逻辑**，只改 import 路径与调用方式（HTTP→函数）。每个子阶段跑全量测试 + 手工 E2E |
| 环路改为函数调用后语义变化 | 原 HTTP 调用有超时/重试/fail-soft 语义（如 ontology 的 fail-soft 交叉引用校验）。函数调用要**显式保留 fail-soft**：`try/except` + 降级返回 |
| **3e 引入的进程边界导致行为差异** | Context 从"同进程对象"变成"跨 UDS 的远端对象"。**必须逐个审计**返回值中是否有不可序列化对象（SQLGlot AST、连接对象）泄漏到 API 层；元数据契约要覆盖前端已用的全部字段（用现有前端 E2E 兜底） |
| **3e 的 UID/权限配置错误导致无法写工作副本** | worktree 目录归属 runtime 的 fsGroup，executor 以同 group 运行；P3 验收门包含"executor 能读写本工作副本、不能读其他工作副本"用例 |
| `dirty` 标志语义 | 合并后文件写与 Context 在同进程（P3 起 Context 在子进程，需**经协议同步 dirty 通知**），要确认 ontology 缓存也接入同一失效机制 |

### 验收门

- 独立代码库从 6 降到 4（✅ 现为 gateway / identity-service / agent-service / control-service / workspace-runtime —— 5 个，因 P2 把 identity 拆成了独立服务，属设计内的增量）
- **服务间 HTTP 环路 = 0**（依赖分析工具验证）（✅ ontology→sqlmesh、sqlmesh⇄dagster 两条环路均改为进程内调用）
- 全量测试绿 + E2E：创建工作区 → 建环境 → 编辑模型 → plan → apply → 查血缘 → 查 ontology → 建 asset → 跑 run
- ontology 交叉引用校验从 HTTP 变函数调用，延迟应**下降**
- **T2 消除验证（安全验收，Q1 决策的核心目标）**：
  - 在 SQLMesh Python model 里写 `import os; print(os.environ)` → 输出**不含**任何 `*PASSWORD*` / `*SECRET*` / `*_KEY` / `*TOKEN*`
  - 经 `agent.shell` 执行 `env`、`cat /proc/self/environ` → 同上
  - 尝试 `cat /proc/1/environ`（runtime 主进程）→ **权限拒绝**（不同 UID）
- **rlimit 生效验证**：`python -c "while True: pass"` 被 `RLIMIT_CPU` 终止；`a=' '*(8*10**9)` 被 `RLIMIT_AS` 拒绝；fork bomb 被 `RLIMIT_NPROC` 限制 —— 且**runtime 主进程存活**
- **git hooks 验证**：提交含恶意 `post-checkout` hook 的仓库并 checkout → hook **不执行**
- **plan 性能不退化**：Context 在常驻 executor 内保持热态，plan p50 仍在 2-10 s（对比 P0 基线）
- ⚠️ **明确不可声明**："租户间强隔离"（同 Pod 文件系统与网络仍共享，需 P6 的 L3-base）

> **实施记录（2026-08）**：3a-3e 全部代码交付完成（代码层面的隔离机制已就位）。上述**安全验收与性能验收项为实测清单，需在具备 Linux 部署环境时逐条跑过** —— macOS 开发环境下 `RLIMIT_AS`/`RLIMIT_NPROC` 走 best-effort 降级路径，无法代表生产行为。这些实测项已并入 P6 的 `scripts/verify-p6.py`（T2/T5/T10 与 git hooks 在该脚本中作为回归项）。

---

## P4 · 工作区租约与亲和路由（3 周）⭐

> 工期从 2.5 周增至 3 周：Q3 的容量自适应与 Q6 的 WIP 契约配套项。
>
> **Q7 决策：不做流量灰度，改为「单代码路径 + 介质可配置 + 分两次上线」。** 理由见 [ADR-003 · Q7 修订](07-decision-records.md#q7-修订不做流量灰度改为介质可配置与分阶段上线)：灰度要求网关同时维护「租约路由 / 随机路由」两条路径、runtime 同时维护「共享 PV 直接用 / 本地物化」两套存储语义，而**并行代码路径本身就是本阶段的最大风险源**，用它来降风险是自相矛盾的。
>
> 本阶段按 **4a → 4b** 两次独立上线：4a 上租约路由与 WIP 保护（工作副本仍在原共享 PV 上），4b 只换 volume 类型。这样每次变更都能被独立验证，且**兜底机制（WIP）先于它要兜底的变更（介质切换）上线**。

### 关键原则：物化逻辑与存储介质正交

`materialize` 只有**一条代码路径**：租约到手 → 检查 `materialize.root/{lease_key}` 是否已物化 → 未物化则 `git clone --bare` → `.repo` → `git worktree add` → 已物化则直接用。**介质是什么，代码完全不感知**，只体现在命中率上。

| 维度 | 内容 | 是否可配置 |
|------|------|----------|
| **物化逻辑** | `git clone --bare` → `.repo` → `git worktree add` | ❌ 唯一路径，不可配置 |
| **存储介质** | `emptyDir` / per-replica PVC / `hostPath` | ✅ K8s volume 类型 + `materialize.mount_kind` |

因此**不存在"新旧两条路径"可供灰度**，只存在"同一条路径跑在不同介质上"——这正是可以砍掉流量灰度的根本原因。

### 阶段 4a：租约 + WIP 契约（2 周，独立上线）

- [x] **租约服务端**（✅ 2026-08 内置于 workspace-runtime `src/lease/`，非 control-service——placement 随 acquire 就近执行；`LeaseController` 扩缩容编排已于 P8 落地（control-service `src/capacity/`，drain-first 缩容经 runtime 的 `/internal/leases/drain`），并经 kind 集群实测（`scripts/verify-p8-k8s.py` 3/3；真实 workspace-runtime 镜像的集群内 drain 仍待生产验证））：
  - `POST /internal/leases/acquire {lease_key}` → 选实例 + 通知 runtime + 返回 addr
  - `POST /internal/leases/release {lease_key}`
  - `LeaseController` 后台控制器：容量监控 + 扩缩容编排 + drain
- [x] **租约客户端**（workspace-runtime `src/lease/`）：
  - 接受租约 → 物化 worktree → 预热 Context
  - watchdog 续期（每 `ttl/3`）
  - idle 释放 + 优雅 drain
  - **文件锁防线**（`flock` on `.datacode-lease`）
  - 心跳上报（`rt:{runtime_id}` HASH，含 addr/leases/capacity/load）
- [x] **网关租约路由**（`edge-gateway` 新中间件 `LeaseRouter`）：
  - 解析 lease_key（复用 `shared/go/workspacepath`）
  - Redis 查租约 → 直连 runtime Pod IP
  - miss → 调 acquire
  - 本地缓存 TTL 10s
  - 转发失败重试 + 租约失效
  - Redis 降级 → 稳定哈希
  - ⚠️ **不实现按比例分流**：租约路由是唯一路径，无 `hash(lease_key) % 100 < N` 旁路
- [x] **WIP 数据持久性契约落地（Q6 决策）** —— 必须在 4b 之前上线并验证：
  - 周期快照：每 `wip_snapshot_interval_seconds`(600s) 检测改动 → `git stash create` → push `refs/datacode-wip/{user}/{env}`；无改动则跳过
  - drain 时强制快照（保证优雅下线零丢失）
  - 物化时检测该 ref → **前端弹窗**"发现未提交改动（快照于 X 分钟前），恢复 / 丢弃"
  - **前端"最后保护时间"指示器**（让风险窗口对用户可见）
  - **快照失败告警 + 前端降级提示**"当前改动未受保护"（否则用户误以为有保护）
  - 前端编辑器 5s 草稿自动保存到 `file_drafts` 表（✅ 落在 runtime 侧，与工作副本同库）；**确认草稿与工作副本两个机制的接缝无空洞**
- [x] **租约容量自适应（Q3 决策）**：
  - 启动期自校验：读 cgroup 内存 limit，校验 `max_per_instance × estimated_lease_bytes + baseline ≤ limit`；不成立则按预算下调有效上限 + 告警 `lease_capacity_downgraded`
  - 准入控制：按 `memory_budget_bytes` 与真实 RSS（`admission_rss_high_watermark`）双重判断，而非只看计数
  - 运行期自适应：租约释放时记录实际 RSS 增量，EWMA 更新估计值；持续偏离 >20% 则下调有效上限 + 上报 `lease_estimate_drift`
- [x] runtime 改为 **StatefulSet**（稳定主机名 + 为 4b 的 `volumeClaimTemplates` 铺路）
- [x] ⚠️ **4a 期间不切 `env_lock`**：此时工作副本仍在共享 PV 上，`mount_kind` 尚未引入。租约已保证正常路径单写者，但**降级路径的后果不同** —— Redis 不可用 + 副本数变化时，共享 PV 上两个副本会写**同一个目录**（介质切换后它们写各自的目录）。故 `env_lock` 的 Redis 后端在 4a 保持启用，4b 介质切换完成后才退化为进程内锁

### 阶段 4b：存储介质切换（1 周，独立上线）

- [x] `materialize.mount_kind` 配置项落地（见 [§05.7](05-data-and-state-model.md#7-配置模型演进)），三个取值（✅ `shared` 在租约+Redis 锁齐备时作为 P4a 过渡形态放行并告警，见 storage contract）：
  - `ephemeral` —— `emptyDir`，Pod 重启即丢工作副本，靠 WIP ref 恢复
  - `instance_local` —— StatefulSet `volumeClaimTemplates` 的 **per-replica RWO PVC**，工作副本与 `.repo` 跨 Pod 重启存活
  - `shared` —— **启动期直接拒绝**（见下方约束）
- [x] **启动期校验**（`mount_kind` 是安全阀，不是文档提醒）：
  - `mount_kind == "shared"` → **启动失败**并输出可执行的错误信息
  - `mount_kind == "instance_local"` → 断言租约已启用，否则启动失败
- [x] volume 声明改造：共享 PV → `volumeClaimTemplates`（RWO，**每副本一个**）或 `emptyDir`
- [x] LRU 磁盘回收（水位驱动）—— `instance_local` 下才真正有意义（缓存跨重启存活）
- [x] 生产默认设为 `instance_local`；`ephemeral` 留给无 PV 或对启动速度敏感的环境
- [x] **移除现在不需要的机制**（介质切换生效后才做）：
  - `env_lock` 的 Redis 后端 → 退化为进程内锁（租约保证单写者，且介质已实例私有）。**保留 Redis 实现作为 Redis 降级期的兜底**，但正常路径不用
  - `task_tracker` 的跨副本 run registry（sqlmesh 侧）→ 进程内即可

> **两个不可让步的约束**
>
> 1. **租约不可配置为关闭。** 单挂载不变式由租约保证，与介质无关。若介质被配成共享 RWX 卷而租约被关，就回到并发写损坏风险，且此时 `env_lock` 的 Redis 后端已按设计退化为进程内锁——**比 1.x 更糟**。这就是 `mount_kind: shared` 必须在启动期硬拒绝的原因。
> 2. **PVC 必须 per-replica，不能是共享卷。** `volumeClaimTemplates`（每副本 RWO）是"持久化的本地盘"；一个 RWX PVC 挂到所有副本是"换了名字的共享 NFS"，会把 P4 的全部收益（git 性能、无分布式锁、租约不变式）一次性抵消。

### 灰度取消后的等价验证手段

灰度真正的价值不是降低介质切换风险（已由单代码路径消除），而是**用真实流量验证租约路由的正确性**。这部分不能凭空消失：

| 灰度原本要验证的 | 替代手段 | 归属 |
|-----------------|---------|------|
| 同 lease_key 100% 落同实例 | staging 压测 + trace 断言 | 4a 验收门（已有） |
| 租约 miss/acquire 竞态 | **并发 acquire 压测**：N 个并发请求同一 lease_key，断言只产生一个租约 | 4a 验收门（**新增**） |
| Redis 降级路径 | 混沌测试：断开 Redis，验证稳定哈希路由仍单写者 | 4a 验收门（已有） |
| drain 不丢数据 | 手工 drain + WIP 恢复验证 | 4a 验收门（已有） |
| 真实用户行为长尾 | **按环境分批上线**：dev 全量 → staging 全量 1 周 → prod 全量（代码单路径，风险由部署顺序而非流量比例控制） | 上线流程 |

### 验收门 · 4a（租约 + WIP）

- 同一工作副本的连续请求 100% 落到同一 runtime 实例（用 trace 验证）
- **并发 acquire 竞态（新增，替代灰度）**：对同一 lease_key 并发 100 次 acquire，断言只产生一个租约、只有一个实例物化
- SQLMesh Context 命中率 > 95%（新增指标 `lease_context_hit_ratio`）
- **优雅下线不丢数据**：手工触发 runtime drain，验证未提交改动经 WIP ref 恢复
- **Q6 契约验证（关键）**：写入改动 → 等待一个快照周期 → `kill -9` runtime → 新实例物化 → **验证改动可恢复且丢失量 ≤ 10 分钟**
- **快照失败路径验证**：断开 Git server → 验证告警触发且前端显示"未受保护"
- **crash 恢复**：kill -9 一个 runtime，验证 ≤ TTL(120s) 后租约被其他实例接管
- **Q3 容量验证**：用大仓库（~2000 模型）压测，验证准入控制在 RSS 触及 80% 时拒绝新租约而**不是 OOM**；验证 `estimated_lease_bytes` 被自适应上修
- Redis 断开后路由降级可用（稳定哈希），且 reconciler 报 `lease_*` 漂移

### 验收门 · 4b（介质切换）

- `git status` 延迟 < 50 ms（对比 P0 基线）
- **`mount_kind: shared` 启动拒绝（新增）**：配置为 `shared` 时进程启动失败且错误信息指明原因；`instance_local` + 租约关闭时同样启动失败
- **`instance_local` 缓存存活**：滚动更新一个副本，验证工作副本与 `.repo` 原地保留、**无 re-clone**、无 WIP 恢复弹窗
- **`ephemeral` 回退可用**：切回 `emptyDir` 后功能完整（仅命中率下降），验证介质确实对代码不可见
- LRU 回收在磁盘触及 `disk_high_watermark_percent` 时回收无租约 worktree 至低水位

---

## P5 · control-service 提取（2 周）

**可与 P4 部分并行**（P4 的租约服务端先放在残余的 sqlmesh-service 里，P5 一并搬走）。

### 任务

- [x] 新建 `control-service/`（✅ 轻依赖：无 sqlmesh 引擎、无 ContextPool、无文件系统访问）
- [x] 搬入 `sqlmesh-service/src/environments/{models,repository,registry,reconciler,reaper}.py`（✅ guard 留在 runtime——plan/apply 准入控制属于工作副本侧）
- [x] 搬入 `sqlmesh-service/src/workspace/{models,repository,manager,database,git_credentials}.py`（✅ manager 的 FS/Git 部分改经 runtime env-control 下发）
- [x] 搬入 P4 的租约服务端 + `LeaseController`（⚠️ 偏差：租约服务端已内置于 workspace-runtime（P4a 演化），placement 就近执行；`LeaseController` 已于 P8 在 control-service 落地（`src/capacity/`，心跳水位驱动、drain-first）。`lease_history` 写入已随 P8 接通（runtime `src/lease/audit.py`））
- [x] **三个 binder 改为"下发指令"**（✅ 通道为 runtime `/internal/env-control/*`，internal token 保护；状态机语义与幂等约定原样保留并有测试钉死）：
  - `git_binder` → 调 runtime `/internal/env-control/git/*`
  - `sqlmesh_binder` → 调 runtime `/internal/env-control/sqlmesh/*`
  - `deployment_binder` → 调 runtime `/api/v1/dagster/deployments/*`（不再跨服务，因为 codeloc 已在 runtime）
  - **保留其状态机语义与幂等约定**（这是最有价值的部分，不能在搬迁中丢失）
- [x] alembic 链合并：sqlmesh-service 的迁移链移交 control-service（`version_table` 保持 `alembic_version`，链的 head 0001-0004 原样接续，0005 新增）
- [x] 新增 `tenant_quotas` / `tenant_usage` / `task_records` / `lease_history` 表（✅ 0005 幂等迁移；配额执行归 P8）
- [ ] **删除 `sqlmesh-service/`**（✅ 全部活引用已清零并经收尾复查修正了 5 处指向已退役端口的**活代码**：agent-service 的 4 个 SQLMesh 工具、`SQLMeshResource` 的 8002 默认值、asset-dag 模板的 8002 回退、runtime 自身 URL 解析、shell 注入的 8002/8004 env；`conf/platform.yaml` 的 `services.{sqlmesh,ontology,dagster}` 三个退役条目已移除。删除指令待执行：`rm -rf sqlmesh-service conf/sqlmesh-service.yaml conf/sqlmesh-service.docker.yaml`。**前置条件已满足**：control-service 与 workspace-runtime 均已建立独立 venv，不再借用 sqlmesh-service 的环境）
- [x] 网关路由更新：元数据类路由 → control-service，工作区操作类 → runtime（lease-aware）（✅ 同时修复了环境注册表路由被 runtime-first 误伤 404 的缺陷；knowledge 元数据列表已于 P8 拆回 control——`KnowledgeMetadataValidatedRoutes` 走 `controlIdentity`）

### 路由划分表（网关需明确）

| 路由 | 目标 | lease-aware |
|------|------|------------|
| `/api/v1/workspaces`（列表/创建/删除/成员） | control-service | ❌ |
| `/api/v1/workspaces/{id}/environments` | control-service | ❌ |
| `/api/v1/environments/{envId}`（CRUD/open） | control-service | ❌ |
| `/api/v1/git/credentials/*` | control-service | ❌ |
| `/api/v1/sqlmesh/apply/history` | control-service | ❌ |
| `/api/v1/sqlmesh/*`（models/lineage/plan/apply/query/...） | **workspace-runtime** | ✅ |
| `/api/v1/git/*`（status/diff/commit/push/...） | **workspace-runtime** | ✅ |
| `/api/v1/files/*` | **workspace-runtime** | ✅ |
| `/api/v1/ontology/*` | **workspace-runtime** | ✅ |
| `/api/v1/knowledge/documents/*`（文件内容） | **workspace-runtime** | ✅ |
| `/api/v1/knowledge/documents/list`（元数据） | control-service | ❌ |
| `/api/v1/logs/*` | **workspace-runtime** | ✅ |
| `/api/v1/dagster/*` | **workspace-runtime** | ✅ |
| `/api/v1/sessions/*`、`/models` | agent-service | ❌ |
| `/api/v1/auth/*`、`/me`、`/rbac/*` | identity-service | ❌ |

### 验收门

- control-service 完全无文件系统访问（用 lint/审计验证：无 `open()` / `Path()` 对 workspace 路径的操作）
- control-service 可自由 HPA（杀任意副本不影响进行中的操作）
- Reconciler 五方对账工作（含新增 lease 漂移类型）（✅ 2026-08-29：五种漂移 + 周期 sweep 落地，见 P8「运营就绪度补全」；此前 `reconcile_workspace` 无生产调用方）
- 环境状态机全部转移路径测试覆盖

---

## P6 · 沙箱执行平面 — L3-base（3 周）⭐ 安全关键

> 工期从 3.5 周降至 3 周：Q2 省去 gVisor 安装与调优（−3 天）、Q5 省去 warehouse-proxy（−2 周），但增加"写操作从 L3 拆到 L2"（+3 天）。
>
> **本阶段的目标是把 P3 已消除的 T2 之外的威胁（T1/T3/T5/T6/T7）也消除**，从而达到「多租户共享集群可用」。T8（容器逃逸）按 Q2 决策延后（§04.8.1）。

### 任务

**阶段 6a：task-runner 骨架与 L2（1 周）** ✅ 已完成（2026-08-28）

- [x] 新建 `task-runner/`：任务协议（spec in / result out + 进度回调）（✅ 协议在 `shared/python/datacode_common/task_protocol.py`（TaskSpec/TaskResult/TaskProgress + stdout 编解码），执行体在 `task-runner/`（`src/main.py` 入口、`src/sqlmesh_ops.py` 操作、`src/handlers.py` 分派、`Dockerfile`））
  - **偏差（结果通道）**：设计 §04.3.2 草拟了 HTTP 回调回传结果，实际改为**结果走 stdout 结构化行**（`@@DATACODE_TASK@@ {...}`），HTTP 回调降级为**可选的进度通道**（`callback_url` 字段已预留）。理由三条：① 结果的可靠性不应依赖 P6b 才交付的 NetworkPolicy 与 scoped token；② Pod 日志在 Pod 之后仍存活，从日志读到的结果不会被"回调与容器销毁竞争"丢掉；③ dev 的本地后端因此与生产走**同一条**代码路径，而非第二条测试更少的路径。
- [x] `isolation-profiles.yaml` 扩展：启用 `L2_container` 与 `L3_base`（`L3_proc` 保留给 dev/单机；`L3_strong` 保持 `enabled: false`）（✅ 全部 profile 显式 `enabled`；**并补齐了 L2 缺失的 Pod 级硬化**：`automount_service_account_token: false` / `enable_service_links: false` / `host_*: false` / `dns_policy: None` / `denied_cidrs` / `node_pool: user-code` —— L2 虽只跑 SQL，但它是持**可写**凭证的档位，挂载 SA token 会让其可调 K8s API）
  - **新增基础设施（设计未列，但为必要前提）**：该文件此前**只被注释引用、无任何加载器**，安全姿态实际是各调用点硬编码的。现由 `shared/python/datacode_common/isolation.py` 落地为可执行策略：按 task_kind 选档、**重叠时取最强**（避免 dev 档位在生产胜出）、`strongest_enabled` fail-safe 解析（链固定为 L3_strong→L3_base→L3_proc，**绝不含 L1/L2**）、`inherits` 展开、`restricted_to(available_levels)` 把"部署想要什么"与"本进程能执行什么"分离。34 个单测。
  - **语义修正**：无可用 fail-safe 档时**不再启动即崩**（否则默认配置下纯 L1 的文件操作也会被拖垮），改为 `default_profile = None` + `profile_for()` 抛 `NoIsolationAvailable`。净效果：沙箱平面关闭的部署照常服务 L1，但需要沙箱的任务被**拒绝**而非降级到 L1，且启动日志给出 `isolation_sandbox_plane_unavailable` 告警。
- [x] runtime 的 `TaskSubmitter`：创建 K8s Job/Pod、传 spec、收结果、超时清理（✅ `workspace-runtime/src/tasks/`：`submitter.py` 编排、`backends/{base,local,kubernetes}.py`、`audit.py`。后端抽象为 P6c 的 warm sandbox 预留了第三种载体）
  - **反降级是硬约束**：`_resolve_profile` 在后端无法提供档位所需 level 时**拒绝提交**，绝不退到更弱载体。超时取"调用方 / profile / 部署上限"三者最严。
  - **dev 回退（设计未明确要求，但 dev 无 K8s 必需）**：`local` 后端以**一次性硬化子进程**跑同一个 task-runner，契约完全相同；它只声明 `PROCESS` level，因此策略层不会把 L2/L3-base 的活派给它。硬化原语（清空 env / rlimits / uid 切换）提取到 `src/isolation/hardening.py`，与常驻 executor **共用一份**（此前是 `executor/process.py` 私有实现；两份"清空环境"若漏一个变量，与没清空无异）。
  - **诚实性修正**：日志字段从 `uid_separation` 改为 `uid_separation_requested`，并在子进程 stderr 出现降级标记时补 `task_local_uid_separation_inactive` 告警 —— uid 切换发生在 fork 之后，父进程无法直接观测结果，运维不能把"配置了"读成"生效了"。
- [x] **L2 落地**：纯 SQL 的 `sqlmesh.plan` / `sqlmesh.apply` 移入 task-runner（标准 runc）（✅ **6c 完成**：task-runner 执行三 op + `assert_sql_only()` 前置校验（L2 是唯一持**可写**仓库账号的档位，其中 exec 用户 Python 正是该拆分要防的最坏组合，故不信任提交方路由、在执行体内自查）；**调用路径切换于 6c 落地**——preview→warm sandbox 保热 Context、apply 写→任务平面走 Q5 拆分（详见 6c 的「plan/apply 调用路径切换」条），fingerprint 校验保留在 runtime 侧锁内、取消语义以任务超时为准（socket 边界上的诚实等价，偏差已记录））
  - 6a 时的工程判断（保留作背景）：一次性容器/子进程每次重建 Context（冷启动 10-30s），而性能验收要求 plan p50<15s 且**前提是 warm sandbox**——故执行平面与载体在 6a 交付、调用路径与 warm sandbox 在 6c 一并切换，避免了中间一轮明知会退化的切换。

- [x] 工作副本挂载（S1：同节点 hostPath subPath + podAffinity）（✅ `backends/kubernetes.py`：`instance_local` 走 per-replica RWO PVC、`ephemeral`/hostPath 走节点目录，二者都用 **`sub_path` 收窄到单个工作副本**（T1 由"挂载点不存在"根治，而非路径校验）；Job 用 `nodeSelector: kubernetes.io/hostname` 钉在持租约的本副本节点上。工作副本不在存储根内时**拒绝提交**而非猜测 sub-path）
- [x] `task_records` 审计写入（✅ `src/tasks/audit.py`：提交前先落 `pending` 行——runtime 中途死掉会留下 pending，这正是让丢失可见的手段；用 SQLAlchemy Core Table 而非 ORM 模型，因该表由 control-service 的 0005 迁移**拥有**，再声明一个 ORM 模型就是第二份权威定义。**审计失败绝不改变任务结果**，全部异常吞掉记日志）

> **实测记录（2026-08-28，dev/macOS）**：端到端链路已跑通 —— 选档 → 审计 → 硬化子进程 → task-runner → 结果回传 → 审计，错误码正确传播。
> **T2 回归实测通过**：父进程存在 `DB_PASSWORD` / `GW_JWT_SECRET` / `OPENAI_API_KEY` 时，子进程环境为 `['DATACODE_ENVIRONMENT','DATACODE_WAREHOUSE_DSN','DATACODE_WORKSPACE_PATH','HOME','LANG','LC_ALL','PATH','PYTHONPATH','PYTHONUNBUFFERED','TZ']` —— **零平台凭证泄漏**，仅用户自有的 warehouse 变量按 Q5 决策保留。
> 未覆盖：uid 切换（macOS 无 CAP_SETUID，实测 uid 未变，已由上述告警如实暴露）、cgroup 限制、容器与网络边界 —— 这些属 L2/L3-base，须在 Linux/K8s 环境按 T1-T10 清单实测。
> 测试基线：shared 167（+89）/ task-runner 31（新）/ runtime 214（+34）/ control 61 / agent 42 全绿。
>
> **顺带修复的既存缺陷**：`workspace-runtime/src/executor/server.py` 的 `Context(paths=...)` 一直传的是**工作副本根**而非 `sql-warehouse/`。SQLMesh 会因此扫错目录树、报"无 models"而非配置错误。因 `OP_PLAN`/`OP_APPLY` 至今无调用方（见上）故未暴露；已改为显式解析项目根，并在 `context.load` 结果中回报 `sqlmesh_root` 便于排查。

**阶段 6b：网络与凭证（1 周）** ✅ 已完成（2026-08-28）

- [x] `egress-proxy` 组件：租户白名单 + 内网段拒绝 + 审计日志 + 流量配额（✅ 新建 `egress-proxy/`：`src/server.py` 主流程、`src/connect.py` 协议、`src/audit.py` 审计、`Dockerfile`。**只实现 HTTP CONNECT**，不转发普通 HTTP —— 后者需要一个暴露给不可信输入的 HTTP 解析器，而 warehouse 驱动、`git`、HTTPS 全都走 CONNECT）
  - **决策逻辑不在 proxy 内**，而在 `shared/python/datacode_common/egress.py`：白名单匹配、内网段拒绝、端口收窄一处实现，proxy 与测试共用同一份规则，无法漂移。46 个单测。
  - **DNS rebinding 是真正被关掉的**：策略判定发生在**解析后的地址**上，且一个域名解析出的**每个**地址都必须通过（一公一私 = 攻击，不是部分成功）；隧道随后连的是**已验证的那个地址**而非再解析一次（否则第二次查询可能返回内网地址，TOCTOU 回归）。基线拒绝段含 IPv6 与 `::ffff:` 映射地址 —— 只查 A 记录的策略会被一条 AAAA 绕过。
  - 基线拒绝段（`BASELINE_DENIED_CIDRS`）**配置无法移除，只能追加**：能缩小它的配置也能重新打开云元数据服务。另外用**类别判定**（multicast/reserved/非全局）兜住未被显式列出的段 —— 手写 CIDR 清单会老化，而漏掉一段就是 fail open。
- [x] `egress-dns`：受控 resolver（✅ `deploy/egress/egress-plane.yaml`：CoreDNS 对 `cluster.local` 直接返回 NXDOMAIN，只 forward 公共 resolver。沙箱**解析不到**内部服务名——不是被拦，而是这个 resolver 从未听说过它；`ndots:1` + 空 search 域使裸名 `postgres` 不会被补成集群 FQDN）
- [x] NetworkPolicy：task-runner default-deny + 白名单（✅ 同上文件：入站**完全为空**（结果经 Pod 日志回传，故沙箱不需要任何入站路径）；出站仅 egress-dns:53 与 egress-proxy:3128。proxy 自身的 policy 用 `ipBlock.except` 再兜一层内网段——策略代码若出错，包也出不去）
  - **偏差（设计 §04.3.4 的 8010 回调）**：清单中**没有**"回调 workspace-runtime"这条出站规则。P6a 已把结果通道改为 Pod 日志，因此这条规则连同它会打开的入口一并消失。
- [x] 用户代码节点池（`taint: datacode.io/user-code`）+ 节点亲和（✅ `deploy/k8s/nodepool-user-code.yaml`。附带记录一个**必须满足的调度约束**：任务 Pod 同时被钉在"持租约的节点"（S1 工作副本交接）与"用户代码池"，两者要有交集 ⇒ **持有工作副本的 workspace-runtime 副本本身必须跑在该池上**，否则任务永远 Pending）
  - 同时补了 `LimitRange`（漏配资源限制的 Pod 不能吃掉整个节点）与命名空间级 `ResourceQuota`（一个租户的任务突发不能拖垮全池 —— 集群层面的跨租户可用性问题；**按租户配额属 P8**）
- [x] 节点内核补丁基线纳入运维流程（Q2 缓解措施，需写入运维手册）（✅ 写入 `nodepool-user-code.yaml` 头部与 `nodepool-user-code-contract` ConfigMap：补丁 SLA、"任何平台负载不得容忍该 taint"、以及明确的 `does-not-cover: 容器逃逸本身`）
- [x] scoped task token：单任务、单工作副本、TTL=任务时长；Secret 随任务创建/销毁（✅ `shared/python/datacode_common/security/task_token.py`：`payload.signature`（HMAC-SHA256），**可离线校验** —— 出网决策因此不依赖 runtime 存活；作用域（tenant/workspace/工作副本/profile）在**签名内**，偷到一个 token 也越不出该沙箱；`expires_at` 就是任务自己的 deadline，故**无需吊销列表**。31 个单测，含"篡改 tenant 被拒""签名先于 payload 被信任""未配置密钥则拒绝而非放行"）
  - 与 `internal_token` **刻意分开**：后者是"服务证明自己属于平台"、不带身份；把它给沙箱等于把平台服务凭证交给用户代码。`security/__init__.py` 的模块注释把这条边界写明了。
  - K8s 侧以**每任务 Secret** 交付，且用 `envFrom` 而非卷挂载：环境变量根本不落盘，比 tmpfs 卷更严。清理失败按 **ERROR** 记录（残留 Secret 是活凭证，不只是孤儿对象）。
- [x] **凭证注入落地（Q5 决策）**：
  - [x] 只注入本工作副本对应的那一个 gateway 凭证（✅ 关键澄清：仓库凭证**本来就在工作副本自己的 `config.yaml` 里**（用户放的），所以真正要做的不是"注入"而是**收窄可达范围** —— 挂载只有本副本 ⇒ 只有该项目的凭证可达（T1 由挂载点不存在根治）。`src/tasks/egress.py` 从该配置**推导**出网白名单：只取**将被实际使用的那一个 gateway**，绝不把所有 gateway 都放通）
    - 白名单是**推导**而非声明：手工维护的清单会在用户新增 gateway 时漂移，而漂移表现为"我的 plan 连不上仓库"，运维的修法是放宽清单——最坏放成通行。读项目则使白名单**恰好等于项目所需**，项目收窄时它也收窄。
    - 只有 **host** 离开该模块；gateway 配置块里的凭证不进入 grant、不进入日志（日志只打 hosts）。
  - [x] **写操作从 L3 拆到 L2**（✅ `workspace-runtime/src/tasks/write_routing.py` + task-runner 的 `sqlmesh.render_project`）：
    - **路由由"检查项目内容"决定，绝不听调用方声明** —— 能声明"我是纯 SQL"的调用方，就是能为满是 Python 的项目要来可写账号的调用方。
    - phase 1（L3-base，只读）执行 Python model 并**渲染成纯 SQL**；phase 2（L2，可写）apply 渲染产物。**关键性质：phase 2 的 `sql_only` 检查保持开启** —— 渲染产物确实不含 Python，检查是靠自身通过的。若设计成"这种情况让 L2 跳过检查"，那只是把风险搬了个地方而非消除。
    - 渲染**部分失败即整体丢弃**：apply 一个静默少了 model 的项目，用户读到的是"数据消失"。渲染后仍残留 Python ⇒ 拒绝进入写阶段并指名文件。
    - 两阶段各拿**完整**超时预算：共用一份会让写阶段继承渲染阶段的剩余时间，正是大项目 apply 被写到一半掐断的成因。
  - [x] Secret 用 `emptyDir: medium: Memory`，任务结束销毁；凭证经 `safe_url()` 脱敏后才可入日志（✅ 实际比设计更严：走 `envFrom` 完全不落盘；`SandboxCredentials.safe_summary()` **只报 key 名不报 value**，代理 URL 经 `safe_url()` 脱敏，且测试断言 token 不出现在 summary 里）
    - `CredentialPolicyError`：profile 声明 `scoped_readwrite` 却又接受执行用户代码的 kind ⇒ **装配阶段直接拒绝**。一条测试对**真实的 `conf/isolation-profiles.yaml`** 逐 profile 跑这个断言，所以未来有人给 L3 加可写账号会被测试拦住，而不是靠注释约束。

> **实测记录（2026-08-28，dev/macOS，真实进程 + 真实 Redis）**：
> 启动 egress-proxy，用 runtime 侧真实铸的 token 与真实发布的 grant，经 `curl --proxy` 验证：
> | 场景 | 结果 |
> |---|---|
> | 无 token | 407，审计 `reason=no_token` |
> | 已声明的 `example.com:443` | **200，隧道成功**，审计 `sent=585 recv=4847 cum=5432` |
> | 未声明的 `pypi.org` | 403 `host_not_allowed` |
> | `10.0.0.1` / `192.168.1.1`（T3/T7） | 403 `internal_address`，指名命中的段 |
> | `169.254.169.254`（T6） | 403 `internal_address` |
> | `127.0.0.1:6379`（T3，平台 Redis） | 403 `internal_address` |
>
> **冒烟抓到一个真实部署缺陷**：egress-proxy 的 `pyproject.toml` 只声明了 `redis`，而 grant store 经共享 platform 配置（pydantic 模型）连 Redis ⇒ 镜像里缺 pydantic 时**每个请求都以 "grant store unreachable" 被拒**，症状离成因三层远。已修：显式声明 `pydantic`/`pyyaml`，并让 grant store **把 ImportError 与"Redis 不可达"分开报告**（前者 ERROR + 明确 hint）。
> 未覆盖：NetworkPolicy 与节点池（dev 无 K8s）、cgroup 限制、容器边界 —— 属 6c 与 P6 验收门，须在 Linux/K8s 环境按 T1-T10 实测。
> 测试基线：shared 270 / egress-proxy 17（新）/ task-runner 46 / runtime 269 / control 61 / agent 42 / gateway 全绿。

> **NetworkPolicy 上线纪律**（清单头部已写明）：Kubernetes 的 NetworkPolicy **没有 audit 模式**，所以路线图原先写的"先 log-only 观察一周"无法照字面执行。替代做法：先部署 proxy 与 resolver、让任务把流量指向 proxy 但**暂不 apply policy**，用出网审计确认真实负载用到的每个目标都被放行，再 apply。观察窗口由审计日志提供，而非由 policy 提供。


**阶段 6c：L3-base 容器化（1 周）** ✅ 核心完成（2026-08-28；运行时侧 codeloc Pod 切换与 warm pool 登记为剩余项）

> **6a 移交给本阶段的前置项**：plan/apply 的调用路径切换（`PlanService` → `TaskSubmitter`）与 warm sandbox 必须同时落地，否则会先经历一轮冷启动退化。详见 6a 的「L2 落地」条。
>
> **结清情况见下方「plan/apply 调用路径切换」条**——实际架构比 6a 预想的更细：preview 走 warm sandbox（保住热 Context），apply 的写走任务平面（Q5 禁止可写凭证进 L3），两者合起来才是"不退化"且"合规"的完整答案。

- [x] `agent.shell` 从 L3-proc 子进程迁到 **task-runner L3-base 容器**（✅ `task-runner/src/shell.py` 实现 handler（注册表显式登记，`sql_only=False`）；`tools.py` 的 `/rt/v1/tools/shell` 切换为经 `TaskSubmitter` 提交——生产=K8s L3-base 容器，dev=硬化一次性子进程，同一提交契约。结果形状与 executor 旧 `shell.exec` 完全一致，agent-service 工具 schema **零改动**）
  - handler 内不重复环境硬化（那是载体的职责：local 后端清空 env+rlimits，容器档位声明自己的）——第二份"清空环境"实现漏一个变量与没清空无异
  - git hooks 经 `GIT_CONFIG_PARAMETERS=core.hooksPath=/dev/null` 禁用（用户 push 的仓库可携带 post-checkout hook）
  - **端到端实测（dev/local 后端）**：提交 → 硬化子进程执行 → cwd 为工作副本 → 结果回传，全链路通
  - executor 的 `shell.exec` op 实现保留（协议内声明的 op，作为后备），但 tools 端点不再调用
- [x] Dagster `K8sRunLauncher` 配置 L3-base profile（run worker Pod）（✅ `deploy/k8s/dagster-container-plane.yaml`：Dagster instance YAML 的 `pod_template_spec`——零 RBAC SA、不 automount token、关 service links、user-code 节点池 taint/toleration、只读根文件系统、内存 scratch。run worker import 用户的 code location，故与任务沙箱同硬化包络）
- [x] Dagster code location 改为**独立 Pod**（`codeloc.mode: pod`）+ L3-base；workspace-sync 从 K8s Endpoints 发现（⚠️ **部分完成**：Pod **契约**已冻结为 `codeloc-pod-contract` ConfigMap（标签供 Endpoints 发现、工作副本 subPath 收窄、租约同节点调度、headless Service = 发现即注册表——Endpoints 对象本身就是 workspace-sync 消费的注册表，无第二真源）；**运行时侧**的 process manager → pod manager 切换未做（当前仍是子进程形态，`src/codeloc/process.py`），登记为 P6 收尾项）
- [x] **`SqlMeshExecutor` 从子进程切换为 per-lease warm sandbox 容器**（`executor.mode: container`）（✅ 双形态齐备：`server.py` 支持 `--tcp PORT` 传输（**帧协议逐字节相同**，P0 冻结兑现——`_dispatch` 单一分发实现，stdio/TCP 共用）；`src/executor/container.py` 的 `WarmSandboxBackend` 创建 Pod（复用任务后端的挂载/调度构造——**单一实现**，T1 的收窄逻辑不会出现两份）→ manager 按配置分流 `_start_process | _start_container`）
  - **TCP 形态实测**：真实起 `--tcp` server，握手（`transport: tcp`, uid_separated=true）→ ping 往返 → 未知 op → 空项目 plan 错误如实透传 → SIGTERM 优雅退出（exit 0）
  - **顺带修复 P6a 真实缺陷**：K8s 后端的 spec 携带 runtime 侧路径，容器内不存在（挂载点是 `/workspace`）——现在提交时改写（`container_spec_json`），warm sandbox 的 `DATACODE_WORKSPACE_PATH` 同理用挂载点
  - 容器模式下 Pod 生命周期归 runtime 管：连接 EOF **不会**停 Pod（与子进程不同），teardown 显式删除；shutdown 时 await 全部删除（排空中的服务不在退出路上抛弃活 Pod）
- [x] **plan/apply 调用路径切换**（6a 移交项，本阶段的核心交付）：
  - **preview → warm sandbox**（`PlanService._sandbox_plan` → executor 的 enriched `sqlmesh.plan` op）：tests、plan 构建、categories、lint、富格式化**全部在沙箱内**——平台进程不再构造任何 Context（T2 在最高频用户代码路径上关闭）。引擎编排提取为 `src/sqlmesh/plan_engine.py`，executor server 与服务层共用一份（两份 plan 语义漂移的方式与两份安全检查完全相同）
  - **apply → 沙箱 gate + 任务平面写**：gate（tests/lint/no-changes，结构化结果只能走 executor 的富通道）在只读沙箱跑；写经 P6b 的 `submit_apply`（Q5 拆分）。**plan 构建两次**（gate 一次热、写任务一次冷）是隔离的代价，如实记录
  - **executor 的 `sqlmesh.apply` op 已删除**：warm 沙箱执行用户代码、最多持只读账号（Q5），该 op 从未有过调用方，留着只会在未来某次"顺手调用"里违反 Q5。`sqlmesh.test` op 补上实现（协议内早已声明）
  - **Context 新鲜度**：文件写入点（files.py 三处）同步标记 executor 脏；git 切换/apply 完成（git.py 三处 + plan_service）同步失效——镜像 context pool 在相同调用点的语义，否则沙箱会对着旧树做 plan
  - **实测（进程模式，真实 fork）**：duckdb 项目 preview 全链路——子进程加载 Context、构建 plan、返回完整结构（has_changes/changes.added/backfills 区间/tests 状态），rlimit 与 uid 的降级**如实**告警
  - 叶子助手（kwargs 映射/测试与 lint 格式化/失败模型捕获/console 代理）下沉 `datacode_common/sqlmesh_support.py`——warm 沙箱、task-runner、runtime 三处消费同一实现；runtime 的 `format.py` 改为再导出
- [x] **Dagster code location Pod 车辆（P6 收尾，2026-08-28 完成）** —— 最后一个在平台进程内长期运行用户代码的缺口关闭：
  - 新模块 `workspace-runtime/src/codeloc/pod.py`：`build_pod_manifest` / `build_service_manifest` 产出**纯 dict**（而非 k8s 客户端对象），因此能直接与 `codeloc-pod-contract` ConfigMap 逐字段对比；`CodeLocationPodBackend` 负责创建/观测/删除，复用任务后端的 `_volumes_for_path` / `_scheduling` / `_container_security_context` / `_resources`（挂载收窄与加固只有一个真源）
  - **发现 = Endpoints**：每个 location 一个 headless Service（`codeloc-<env>`），Kubernetes 维护的 Endpoints 对象**就是注册表**（存在=可拨，消失=不在），没有第二份真源需要同步。Pod 模式固定 4000 端口（各 Pod 独立 netns），端口分段（ports.py）在该模式停用
  - 开关 `dagster.code_location_mode: process|pod`（默认 process）。**缺容器档时拒绝启动而非降级**为进程车辆（降级会让"pod 模式"变成谎话）
  - 存活状态刷新点：`CodeLocationManager.refresh_state()`（async，读 Endpoints），由 `/internal/code-locations` 两个端点在应答前 await —— 同步的 `is_alive()` 只返回最近观察值，避免在请求路径上做集群读
  - **测试 `tests/test_codeloc_pod.py`（32 项）**：契约对齐（labels/命令/挂载/加固/调度键）、命名 DNS 安全与确定性、Endpoints 存活语义、Pod 起不成就删除（不留半创建的 Pod 占着挂载）、无容器档时抛错、**契约 snake_case 与实现 camelCase 的桥接**、manager 的 ensure/reload/remove/advertise 分流
  - **诚实记录**：Pod 模式未经真实集群验证（dev 无 K8s），集群行为（调度、Endpoints 收敛、镜像）须在 `--k8s` 环境首跑时确认
  - **P0 缺陷已修（同日 review 发现）**：codeloc Pod 曾落在**没有任何 NetworkPolicy 覆盖的缝隙** —— 它建在 `datacode-tasks`（`task_namespace`）、标签 `app: dagster-code-location`，而 `task-runner-default-deny` 选的是 `app: task-runner`、`dagster-user-code-egress` 在 `datacode` 命名空间；NetworkPolicy 只在自己的命名空间内选择，于是两个都选不中它 ⇒ 出网**零管控**，而它恰恰是长期驻留用户代码的载体。修复分三处（缺一即断链）：
    1. 新增 `code-location-policy`（`deploy/egress/egress-plane.yaml`）：ingress 仅允许 `datacode` 命名空间的 `app: dagster` 访问 4000；egress = 受控 DNS + egress-proxy + 编排器 3030。**它不能复用 `task-runner-default-deny`**——那条规则的前提是"沙箱不接受任何入站"，而 code location 存在的意义就是被拨号
    2. `egress-proxy-policy` / `egress-dns-policy` 的 ingress 放行 `app: dagster-code-location`（否则放行前状态是"启动正常、gRPC 可拨、所有出网静默失败"）
    3. 契约 ConfigMap 的 namespace 与实现统一为 `datacode-tasks`（用户代码统一在受管平面：ResourceQuota / LimitRange / taint 池 / egress 基础设施都在那），并加注释说明 `dagster-user-code-egress` 只管 run worker
  - **守护（防回归）**：`tests/test_codeloc_pod.py::TestNetworkPolicyCoverage` 6 项断言——Pod 建在契约 namespace、默认 namespace 与契约一致、**必须存在 policy 选中 codeloc 标签**（否则报"would have unrestricted egress"）、该 policy 必须限制 egress（不含 0.0.0.0/0）、proxy 与 resolver 的 ingress 必须放行 codeloc。已**故障注入反证**：把契约 namespace 改回 `datacode` 后 3 项 FAIL 且错误信息精确指名
  - **配置项可见性（P3）**：`dagster.code_location_mode` / `code_location_container_image` / `code_location_pod_ready_timeout` / `sqlmesh.metadata_cache_ttl_seconds` 写入 `conf/workspace-runtime.yaml`（此前只存在于代码默认值里，运维无从配置）；同时删除该文件里随 ContextPool 一起退役的 `context_pool.*` 残留段

- [x] **测试缺口补齐（P2，2026-08-28）** —— 补上无守护的暴露面：
  - `tests/test_sqlmesh_binder.py`（21 项）：**唯一仍在进程内构造 Context 的模块此前零测试**。钉住零成本 plan 的四个选项（`create_from` / `skip_backfill` / `skip_tests` / `include_unmodified` / `no_prompts`——少一个就会把"新建环境"变成全量回填或视图缺失）、失败即上报为 drift 而非抛出、**失败路径也必须关闭 Context**（注入回归反证：把 `finally` 换成 `pass` 后 2 项 FAIL）、invalidate 必须两步都做（标记 state 记录 ≠ 释放视图）、`list_state_environments` 的 `None`（不可达）与空集（不存在）之别（这是 reconciler 在仓库中断时不会重建/失效一切的依据）、ConfigError/其它异常的错误翻译
  - `tests/test_codeloc_process.py`（18 项）：进程车辆与 manager 的 process 模式。重启预算是**滑动窗口**（稳定运行数天的进程应拿回重试额度，此刻在崩溃循环的才被放弃——用总数会把两者混为一谈）、显式 spawn 会替换存活进程并清空预算、存活状态经进程注册表而非记忆的标记、只广告活着的 location、readiness 在进程已死时立即放弃（不轮询满 30s）
  - `tests/test_codeloc_pod.py` 的 `TestNetworkPolicyCoverage`（6 项）见上一节
- [x] **死代码清理（P4，2026-08-28）**：删除 executor 的 `shell.exec` op 实现与注册、`OP_SHELL` / `OP_APPLY` 两个协议常量、`ShellResultDTO`（三者均无调用方；apply 与 shell 都已迁到任务平面，同名字符串在 **TaskSpec.kind** 命名空间仍然活跃，协议文件已加注释说明这不是同一个命名空间，避免后人误删），并同步更新 task-runner 中引用"旧 shell op"的注释
- [ ] **旧服务目录删除（P5 遗留，待执行）**：`sqlmesh-service/`（git 跟踪 95 文件）+ `conf/sqlmesh-service.yaml` + `conf/sqlmesh-service.docker.yaml`。前置条件已全部满足（五个服务各有独立 venv；包级活引用为 0，剩余命中全是 `sqlmesh_service_url` 这类配置名与注释）。**但删除前必须先保存未提交改动**：该目录当前有 16 个文件、+892/-256 行**从未提交**的改动（env_locks +440、event_bus +187、task_tracker +145、naming -207…）。已核实这些能力全部存在于 workspace-runtime 对应模块（env_locks 22KB 含 Redis 后端、event_bus 11KB 含 SSE），属历史迁移痕迹；仍建议先落盘补丁再删：
  ```
  git -C datacode diff sqlmesh-service > /tmp/sqlmesh-service-uncommitted.patch
  rm -rf datacode/sqlmesh-service \
         datacode/conf/sqlmesh-service.yaml \
         datacode/conf/sqlmesh-service.docker.yaml
  ```
- [x] **warm pool（P6c 收尾，2026-08-28 完成）** —— 常驻 task-runner，消除可池化任务的容器冷启动：
  - **偏离路线图原文「每租户 1-2 个预热 runner」，改为按工作副本**。理由是原方案与已落地的 T1 收窄不可兼得：一个服务该租户多个工作副本的 Pod 必须挂载包含它们的子树，而这恰好使一个成员的副本从另一个成员的任务里可读——正是 `subPath` 单副本挂载消除的暴露。代价是命中率更低（首次触达某副本仍要起 Pod），这是隔离的诚实成本，且有界：用户同时只在一两个副本里工作，**第二个任务起就是热的**，而这正是延迟抱怨的来源
  - **凭证按任务下发，不按 Pod**（保住 P6b 的「单任务作用域 + TTL=任务时长」）：常驻 Pod 活得比任何单个任务长，因此 token 随每个 `run` 请求经协议帧传入，只在该任务执行期间存在于 `os.environ`，**任务失败也照样清除**。刚跑完一个任务的 Pod 不持有任何仓库凭证
  - **只池化只读 kind**（默认 `agent.shell`）：L2 档持有可写仓库账号，让它在 Pod 里跨任务存活正是 Q5 禁止的共存
  - 新增 `shared/.../framing.py`：把 4 字节长度前缀 JSON 帧提取为**一份实现**，`executor_protocol` 与新增的 `warm_protocol` 共用（`executor/server.py` 仍保留自有副本，因为它必须能在裸解释器下启动——这是有意且已记录的重复）
  - 新增 `task-runner/src/serve.py`（`--serve PORT`）：与一次性形态**共用同一个 `run_task`**（热任务与一次性任务必须是同一个任务，否则热路径会变成第二套未经检验的执行语义）；**一次只跑一个任务**（并发会共享 Pod 的内存限额、CPU 配额与 SQLMesh 缓存目录），第二个任务收到 `busy` 而非排队；**按请求校验工作副本**，错路由的任务被拒绝而非跑在错项目上
  - 新增 `workspace-runtime/src/tasks/warm_pool.py`：池上界（超限即回落一次性 Job，**绝不排队**——排队会把容量问题变成无界延迟）、任务数上界后退役（一个任务可能损坏自己的解释器，下一个会继承）、空闲回收、`invalidate` 随 git 切换/apply 退役该副本的 Pod。**每一种「热路径不可用」都返回 None 落回一次性载体**，所以任务永远不会因为池而失败——池最差只是帮不上
  - **NetworkPolicy 分裂为两条**：一次性任务带 `datacode.io/kind: one-shot` 由 `task-runner-default-deny` 管（ingress 完全为空），常驻 runner 带 `warm-pool` 由新增 `warm-pool-policy` 管（**唯一入站路径**：只允许平台命名空间的 workspace-runtime、只允许 8020 端口；同命名空间其它沙箱不得连——否则一个沙箱能给别人的 runner 投喂任务，跑在它无权触碰的工作副本上）
  - **实测**：真实进程 + 真实 socket 冒烟 11/11 通过（握手/ping/真实 shell 任务读到工作副本文件/第二个任务同进程复用/token 可见/**下一个任务不继承 token**/错工作副本被拒/shutdown 退出）。故障注入双向反证：去掉凭证清除 → 4 项 FAIL；去掉 kind 标签 → 4 项 FAIL 并指名「would have unrestricted egress」
  - **未在真实集群验证**（dev 无 K8s）：Pod 创建、跨命名空间 ingress、性能门 `shell p50<300ms` 须在 `--k8s` 环境实测
  - 测试基线：runtime 422（+6 池覆盖守护，共 32 项池测试）/ task-runner 65（+19）/ shared 270 全绿
- [x] 确认 `default_profile: strongest_enabled` 解析为 `L3_base`（✅ 6b 已验证：生产解析为 L3_base；有测试钉住"默认档绝不在 L1/L2"）
- [x] **读侧元数据服务全部切进沙箱（P6 收尾，2026-08-28 完成）** —— §04.3.7 的 MetadataContext 分裂落地，T2 的最大剩余暴露面关闭：
  - **沙箱侧**：新模块 `src/executor/metadata_ops.py` 实现全部读/执行 op——`metadata.models`（列表 + 环境视图 + metrics）、`metadata.columns`（模型详情含 rendered SQL）、`metadata.lineage`（模型级含 focus/环境视图 + 列级血缘）、`metadata.environments`（UI 视图 + `raw_names` 对账视图）、`sqlmesh.query/evaluate/render/test/table_diff`。全部返回**纯数据**（最终 API 形状），无 AST/连接/活对象跨界。协议演进方式：**冻结 op 的 params 扩展**（协议明确允许）+ 仅 `sqlmesh.table_diff` 一个**新增 op**（migration log 记 additive note，runtime 与 executor 永远同树同发布，不存在交叉版本部署）
  - **runtime 侧**：新模块 `src/sqlmesh/metadata_context.py` 的 `MetadataContextStore`——纯元数据（models/metrics/environments/模型级 lineage）按 (workspace, gateway, op, params) 缓存（TTL 兜底外部 CLI 编辑），执行类（模型详情/列血缘/query/evaluate/render/test/table_diff）直通沙箱不缓存；`model_not_found` op 错误翻译为 404。**`mark_dirty`/`invalidate` 内部联动 executor manager**（新文件对沙箱 mtime 刷新不可见，两个持有者必须看到同一失效事件——冒烟实测抓到的真实缺陷）
  - **ContextPool 已删除**：进程内构造 `Context(load=True)` 的唯一入口消失。`load_workspace_config`/`ping_gateway` 提取到 `src/sqlmesh/config_access.py`，schema 发现与 external-model 导入改用 **config-built adapter**（不再为拿一个内省连接而 exec 用户模型）
  - **state_schema 校验前移**：原 pool 的 `reconcile_state_schema`（409 语义）移到 ExecutorManager 启动点——修复 P6c 遗留的"沙箱路径绕过校验"缺陷；repair 端点同步切换
  - **codeloc bridge 与 ontology 自动受益**：两者都经 model/lineage 服务间接消费，服务切沙箱后零改动
  - **端到端实测（dev/进程模式，真实 fork + duckdb 项目）**：models 列表 → 模型详情（rendered SQL）→ lineage → environments → query 全链路经沙箱；缓存命中（第二次读零沙箱往返）；mark_dirty 后新模型可见；validate 对好/坏项目双向正确
  - **测试基线：runtime 313（+20）/ shared 270 / task-runner 46 / control 61 / agent 42 全绿**

> **P6c 偏差与剩余项（如实记录）**：
> 1. **codeloc 运行时侧 Pod 切换未做**（契约已冻结）；2. **warm pool 未做**（依赖常驻接收通道）；3. **apply 的过程内取消语义弱化**：写入已交给任务平面后无法在 SQL 中途中断，以任务超时 kill 为准（socket 边界上诚实的取消）；4. **apply 期间 SSE 无逐模型进度**（边界事件保留：creation/backfill/promote）——两者都是隔离换来的、已记录的取舍；5. **读侧元数据服务仍进程内加载 Context** —— **已于 P6 收尾结清**（见上方「读侧元数据服务全部切进沙箱」条）。
>
> **P6 收尾后仍诚实登记的残余暴露（非 8 服务范围，如实记录）**：
> - **SQLMesh binder 的两个写操作仍在进程内构造 Context**（`ensure_virtual_environment` / `invalidate_virtual_environment`，`src/environments/sqlmesh_binder.py`）：虚拟环境创建的 plan 构建 exec 用户代码、apply 写 state schema，二者的 Q5 拆分（渲染在 L3 / 写在 L2）是独立工程。已收敛为带显式锁的两个低频控制面操作并在模块头登记暴露；读路径（`list_state_environments`）已切沙箱（`metadata.environments` 的 `raw_names` 参数）。
> - **config.py 解析边界**：`config_access.load_workspace_config` 接受 SQLMesh 的 `config.py` 形式，其内容是用户代码（会在平台进程 exec）。绝大多数项目用 YAML；把 config 解析移进沙箱是后续收窄项，已在模块 docstring 登记。

### 验收门（安全验收，必须逐条实测）

针对 §04.1.2 的 T1-T10 逐条渗透验证：

| # | 验证方式 | 期望 | 备注 |
|---|---------|------|------|
| T1 | 在 task-runner 内 `ls /` `find / -name "*.sql"` | 只见本工作副本 | P6 新增覆盖 |
| T2 | `env`、`cat /proc/1/environ` | 无任何平台凭证 | P3 已覆盖，此处回归 |
| T3 | `nc -zv <pg-host> 5432`、`redis-cli` | 连接被拒 | P6 新增覆盖 |
| T4 | `curl http://workspace-runtime:8010/rt/v1/...`（他人 lease） | 被拒（NetworkPolicy + token 校验） | P6 新增覆盖 |
| T5 | `python -c "while True: pass"`、`stress --vm 8` | cgroup 限制生效，仅本任务被 kill | P3 部分覆盖(rlimit)，P6 完整 |
| T6 | `curl 169.254.169.254/latest/meta-data/` | 被拒 | P6 新增覆盖 |
| T7 | `curl http://10.0.0.1/`、`curl http://9.1.1.1/` | 被拒 | P6 新增覆盖 |
| **T8** | 已知内核提权 CVE PoC | ⚠️ **可能成功** | **Q2 决策下未覆盖**：需记录实测结果作为风险基线；缓解是节点池隔离 + 内核补丁 |
| T9 | 大流量出网 | egress-proxy 配额拦截 + 告警 | P6 新增覆盖 |
| T10 | 尝试写平台目录 | `readOnlyRootFilesystem` 拒绝 | P3 部分覆盖(UID)，P6 完整 |
| **Q5 约束** | 在 L3-base 内读 `DATACODE_WAREHOUSE_*` 并尝试 `CREATE TABLE` | 凭证可读（已接受）但**账号只读，DDL 被仓库拒绝** | 验证"写操作已拆到 L2" |
| **git hooks** | 提交含恶意 `post-checkout` hook 的仓库并 checkout | hook 不执行 | P3 已覆盖，此处回归 |

**性能验收**：
- Agent shell 单命令 p50 < 300 ms（warm pool；Q2 决策下无 gVisor 开销，应更优）
- SQLMesh plan p50 < 15 s（warm sandbox）
- Dagster run 启动延迟 < 20 s（Q2 决策下无 runsc 初始化）

**安全声明门**：本阶段通过后**方可对外声明"租户间隔离（跨租户数据/网络/资源）达标"**；**仍不可声明"抗容器逃逸"**（需 §04.8.1 的 L3-strong）。

**验收脚本（已落地）**：`scripts/verify-p6.py` —— T1-T10 的可执行形式。设计原则：**能在本机真实执行的就用真代码路径执行，不能的明确报 `REQUIRES-K8S` / `MANUAL`，绝不假绿**。

- **真实执行**（走生产代码路径，非模拟）：T2 经 `TaskSubmitter` + local 后端提交真实 shell 任务，先在本进程植入 5 个哨兵凭证再用 `platform_secret_names()` 回读比对（**已故障注入反证**：让 `build_clean_env` 泄漏整个环境后，脚本精确报出全部 5 个泄漏项并 FAIL）；T1 任务内 `pwd` 断言 cwd 即工作副本；T5 任务内读 `getrlimit` 断言收紧到 profile 声明值；git hooks 用**对照实验**（无 `GIT_CONFIG_PARAMETERS` 时 hook 确会执行 → 载体内不执行，防止"fixture 坏了却看着绿"）；T10 用 `inspect.getsource` 定位到 `_container_security_context` / `_pod_security_context` 方法体断言加固项（比 grep 强，且不受 dev 缺少 k8s 客户端影响）
- **策略层验证**（T3/T6/T7 + Q5）：对真实 `conf/isolation-profiles.yaml` 断言出网决策（含 DNS rebinding：声明域名解析到 169.254.169.254 仍拒绝）与 Q5 凭证约束（`assert_write_grade_allowed` 全 profile 扫描 + 把 L3_base 翻成 `scoped_readwrite` 后断言 `build_sandbox_credentials` 拒绝装配）。**Q5 对"声明的策略"判定而非"本机可执行的策略"**——dev 无容器运行时会把 L3_base 降级为 L3_proc，降级事实单独如实打印
- **dev 实测结果（2026-08-28）**：23 项 PASS / 0 FAIL / 9 项待集群（8×REQUIRES-K8S + 1×MANUAL T8）。`--k8s` 分支已在脚本内实现（`kubectl run` 探测 Pod 执行渗透命令），但**本机无集群，该分支未经实测**，须在 K8s 环境首次运行时确认
- 用法：`python3 scripts/verify-p6.py [--k8s] [--only T2,Q5]`；退出码 1 表示有 FAIL；`REQUIRES-K8S`/`MANUAL` 不计失败但明列为**未闭合项**

---

## P7 · Cell 化落地（2.5 周）

### 前置任务（P2 未结清项）✅ 已于 P5.5 提前完成（2026-08-28）

> 拆库的前提是**每张表只有一个写入方**。当前身份表由网关与 identity-service 双方读写，直接 `pg_dump --schema=identity` 会把一个仍在被网关写入的 schema 搬走。
>
> **状态：已解除**。下列三项在 P5.5 完成，详见 §P2 的「P2 结清记录」。

- [x] **网关收敛为 identity-service 的纯消费者**：删除 `gateway/internal/auth/store.go` 与 `internal/rbac/store.go`，`cmd/server/main.go` 去掉 `auth.NewStore` / `rbac.NewService(rbac.NewStore(...))` 与 `Bootstrap`（建表动作随之消失）
- [x] **`/auth/*` 与 `/me` 改为反向代理**到 identity-service；`rbac` 只保留决策逻辑（catalog/routemap/wspath）
- [x] **Python identity 客户端**（`shared/python/datacode_common/identity/client.py`）：供 reconciler / schedule 触发等非 HTTP 入口做 actor 授权判断

### 任务

- [ ] **Global / Cell 库物理拆分**：`identity` schema 迁到独立 Global PG 实例（运维动作；代码侧前置已全部就绪：`db_topology` 启动断言、identity 独立库配置、cell_registry 注册。跨库 Saga 对账：P2 已删除 `workspace_members`（绑定归 identity），`workspaces` ↔ 绑定已无跨库外键，原补偿项随之消失——迁移工具的 verify 阶段承担等价的数据完整性对账）
- [ ] **第二个 Cell 部署**（`cell-1`）：完整栈（gateway/control/agent/runtime/orchestrator/PG/Redis）（运维动作；本地已用 identity + 双 control 实例模拟双 Cell 完成迁移演练，见下）
- [ ] **全局边缘路由**：Envoy/Ingress 按 `JWT.cell_id` 路由（部署配置；`gateway/internal/cellrouter` 的按 claim 路由库与测试已就绪）
- [x] identity-service 的 placement API 与放置策略（选最空 Cell）（✅ `/internal/tenants/{id}/placement` 读写端点 + `PickCell` 最空优先（满载/非 active 排除）+ `EnsurePlaced` 新租户落位 + `/internal/cells` 注册/列表，`internal/placement/` 全套含测试）
- [x] **Cell 迁移工具**（✅ 2026-08-29 `scripts/cell-migrate.py` plan / run / purge-source 三命令；数据面 = control-service `src/api/rest/cell_migration.py` 的 export / import / verify / purge 内部端点，`X-Internal-Token`（`CONTROL_SERVICE_CELL_MIGRATION_TOKEN`，fail closed）保护）：
  ```
  1. tenant_placement.migration_state = draining（停发新 token）✅
  2. 等旧 token 过期（access token TTL，如 15 min）✅ --token-ttl
  3. state = migrating：✅
     a. 该租户的 Cell 数据导出（workspaces/environments/environment_events/
        git 凭证/配额/knowledge 元数据/草稿/apply 历史）✅
     b. 导入目标 Cell（幂等：同过滤先删后插，单事务）✅
     c. 该租户的 orchestrator deployment 在新 Cell 重建 —— 由目标 Cell 的
        reconciler missing_deployment 修复路径自动完成（设计如此：复用自愈，
        不在迁移工具里复制编排）✅
     d. 工作副本无需迁移（从 Git 在新 Cell 重新物化）✅（设计不变）
  4. state = verifying：新 Cell 健康检查 + 计数对账（源 export ↔ 目标 verify，
        含源保留副本完好性检查）✅
  5. cell_id = target_cell_id，state = stable ✅
  6. 旧 Cell 数据保留 7 天后清理 ✅ purge-source（防呆：租户仍在本 Cell 时拒绝）
  ```
  范围决策（记录在案）：`lease_history`/`task_records` 审计留在源 Cell（append-only 的"事发地"记录）；agent 会话库（独立 service="agent" 库）不迁移——会话是易失上下文，不阻塞工作区可用性，保留期内源 Cell 仍可读。
  **本地双 Cell 演练已通过（2026-08-29）**：identity（真实服务）+ 源/目标两个 control 实例（独立库），plan → run（draining→wait→migrating→verifying→stable 全状态机）→ purge-source 全闭环；迁移后新登录 token 的 `cell_id` claim 已切到目标 Cell；源库清空、目标库数据完整。演练抓到并修复 2 个真实缺陷：源 Cell 上 runtime 表未建时 export 500（现在按零行处理）、import 的 lazy 建表漏 knowledge_documents 所在 metadata。仍待真实集群/生产环境：多 Cell 完整栈部署与 Envoy 边缘路由联调
- [ ] 逐 Cell 滚动部署流水线（§05.4.2）（运维）
- [ ] Cell 容量监控与告警（`assigned_tenants / capacity_tenants > 0.8`）（随 DEFERRED 可观测性栈）

### 验收门

- 两个 Cell 独立运行，租户按 placement 正确路由（本地演练已验证 placement 切换与新 token 的 cell_id 路由；真实双栈部署待运维）
- **Cell 迁移演练**：迁移一个测试租户 A→B，验证数据完整、工作区可用、schedule 恢复（✅ 2026-08-29 本地双 Cell 演练：数据完整（计数对账）+ 新 token 正确路由；工作区从 Git 重物化与 reconciler 重建 deployment 依赖目标 Cell 的 runtime/orchestrator，待真实双栈环境验证）
- **爆炸半径验证**：停掉 cell-1 的 PG，cell-0 的租户完全不受影响（待真实双栈）
- identity-service 挂掉时：已登录用户仍可用（授权缓存），新登录返回 503（网关授权缓存行为，P5.5 已实现；完整演练待真实双栈）

---

## P8 · 可观测性与容量治理（2 周）

> **范围调整（2026-08-28 决策）**：本阶段拆为**两半独立推进**。
>
> - **实施部分**：配额执行 + 容量编排 + P4/P5 欠账清偿（下方标注 ⬅ **实施**）
> - **延后部分**：OTel 全链路、Loki 标签、指标导出、Grafana 仪表盘、告警规则 —— 整体标记 **DEFERRED**，作为未来 TODO。
>
> 延后的前提条件已保留：结构化日志字段（含出网审计四元组 tenant/dest/bytes/deny_reason）与 `task_records` / `lease_history` 审计表均可被后续管道**无损消费**，接入时无需改动业务代码。
> 因此本阶段的容量编排**不得依赖指标栈**：`LeaseController` 直接读租约心跳（Redis `rt:{runtime_id}` 的 leases/capacity/load 字段），而非 `lease_utilization` 指标。
> 建议触发时机：多 Cell 上线（M-E）后，或出现需要跨服务定位的线上问题时。

### 任务

- [ ] **DEFERRED · OpenTelemetry**：`opentelemetry-instrumentation-{fastapi,httpx,sqlalchemy}` + Go otel；OTLP → Tempo/Jaeger；`trace_id` 与 structlog 字段统一
- [ ] **DEFERRED · Loki 标签**：`cell_id` / `tenant_id` / `workspace_id` / `service` / `level`
- [ ] **DEFERRED · 新增关键指标**（下表整体延后；`lease_*` 系列的数据源已在租约心跳中就绪，接入时无需改动 runtime）：

| 指标 | 类型 | 用途 |
|------|------|------|
| `lease_count{cell,runtime}` | Gauge | 租约容量 |
| `lease_utilization{cell}` | Gauge | 扩缩容驱动 |
| `lease_acquire_duration_seconds` | Histogram | 首次物化延迟 |
| `lease_context_hit_ratio{cell}` | Gauge | Context 命中率（核心 SLI） |
| `workcopy_disk_usage_percent{runtime}` | Gauge | LRU 回收触发 |
| `task_duration_seconds{kind,isolation}` | Histogram | 沙箱性能 |
| `task_failures_total{kind,reason}` | Counter | 沙箱可靠性 |
| `sandbox_oom_total{tenant}` | Counter | 噪声邻居检测 |
| `egress_bytes_total{tenant,dest}` | Counter | 出网审计与配额 |
| `egress_denied_total{tenant,reason}` | Counter | **安全告警** |
| `tenant_quota_usage{tenant,resource}` | Gauge | 配额治理 |
| `codeloc_warm_count{cell}` | Gauge | code location 容量 |
| `cell_assigned_tenants{cell}` | Gauge | Cell 容量 |

- [ ] **DEFERRED · Grafana 仪表盘**：Cell 概览 / 租约健康 / 沙箱执行 / 租户配额 / 安全事件
- [ ] **DEFERRED · 告警规则**（下表整体延后）：

| 告警 | 条件 | 级别 |
|------|------|------|
| 租约利用率过高 | `lease_utilization > 0.85` 持续 10 min | Page |
| Context 命中率低 | `lease_context_hit_ratio < 0.8` 持续 15 min | Page（说明路由失效） |
| 出网被拒激增 | `rate(egress_denied_total) > 10/min` per tenant | **Security Page** |
| 沙箱 OOM 率高 | `sandbox_oom_total / task_total > 0.2` per tenant | Warn |
| Redis 协调降级 | 任何 `*_degraded` warning 持续 > 5 min | Page |
| Cell 容量 | `cell_assigned_tenants / capacity > 0.8` | Warn |
| daemon 心跳丢失 | Dagster daemon heartbeat > 120s | Page |
| 租约脑裂 | `lease_split_brain` 漂移 > 0 | **Page** |

- [x] ⬅ **实施 · 配额执行**（✅ 2026-08-29）：工作区/环境配额在 control-service 的创建路径拦截（`src/quota/service.py`，live COUNT 对照限额，超限 429 + `Retry-After` + 结构化负载；缺行用平台默认值、行内 0 是显式禁用、计数读失败 fail-open）；租约配额在 runtime 的 acquire 路径拦截（`src/lease/quotas.py`，从心跳流聚合租户占用：并发租约超限直接 429，副本占用超限通过 placement 排除集导向既有占用面、无可用即 429）。限额由 control 的发布器（`src/quota/publisher.py`）写入 Redis（`quota:defaults` + `quota:{tenant}`，带 TTL，控制面死亡一个 TTL 后优雅降级为 enforcement-off）；run 并发/会话数/出网流量/LLM token 四类配额未纳入本轮——表结构与拦截模式已就绪，按运营需要追加
- [x] ⬅ **实施 · 欠账清偿**（✅ 2026-08-29，P4/P5 记账项）：
  - `LeaseController` 扩缩容编排（control-service `src/capacity/lease_controller.py`）：直接读租约心跳水位（Redis `rt:{runtime_id}`，与指标栈零依赖），迟滞阈值驱动副本数；扩容立即生效，缩容 **drain-first**——先经 runtime 新增的 `POST /internal/leases/drain` 内部端点请求排空（draining 标志即刻上心跳），等心跳消失或超时（SIGTERM drain 兜底）再缩；副本执行器可插拔（`log` 默认 / `kubernetes` 走 StatefulSet scale 子资源，需 `pip install -e ".[k8s]"`）。**已在 kind 集群实测通过**（`scripts/verify-p8-k8s.py` 3/3：A=scale 子资源真实读写；B=缩容移除最高 ordinal 的受害者语义确认——SIGTERM drain 兜底是必需而非优化；C=完整 drain 时序闭环：drain_requested→心跳消失→scale_down，生产 controller + 生产 httpx drain 调用 + 真实 Redis/K8s，仅 runtime 为同线契约的桩）。仍待生产集群验证的：真实 workspace-runtime 镜像在集群内 drain（pod 间 HTTP、最终快照、SIGTERM 行为）
  - `lease_history` 写入接通：runtime `src/lease/audit.py`（Core 表镜像 + 审计失败绝不影响租约；acquire/release 走 fire-and-forget 任务，drain 内联等待——关机路径零丢失）；挂点：serve/adopt 的 acquire、三处补偿 release、drain
  - `/api/v1/knowledge/documents/list` 元数据路由从 runtime 拆回 control-service（`src/api/rest/knowledge.py`，Core 表镜像 runtime 拥有的 `knowledge_documents`，响应形状与旧端点逐字节兼容）；网关 `KnowledgeMetadataValidatedRoutes` 走 `controlIdentity`，create/upload/read/delete 留在 runtime（文件在工作副本上）；runtime 旧端点与死代码已删
- [x] ⬅ **实施 · 运营就绪度补全**（✅ 2026-08-29，review 清偿轮）：
  - **五方对账闭环**：reconciler 补齐租约与沙箱两方（设计 §05.5.1 五种新漂移全部落地）——`stale_lease`（持有者心跳消失，Lua CAS 按 token 删）/ `orphan_lease`（环境已归档，租约无人释放）/ `lease_split_brain`（双声明者，**critical** 级只报不修且抑制同键的 stale 判定，防删活写者的锁）/ `orphan_codeloc_pod`（经 runtime 新增 `/internal/env-control/sandbox/inventory` 与 delete 端点，删除前二次校验 Pod 标签）/ `zombie_task`（grace 窗口后落 `failed`，窗口默认 900s 覆盖镜像拉取）。新增周期性 sweep（`src/environments/sweep.py`，小时级；**默认只检测**，`reconcile.sweep_repair` 开启修复；跨副本用 `env_lock:{ws}:__sweep__` 伪环境锁互斥——该名不是合法环境名故不与真实环境锁碰撞）。修复了 `reconcile_workspace` 此前**无生产调用方**的问题
  - **顺带修复真实 bug**：lease holder 的 RELEASE/RENEW Lua 脚本把 JSON payload 整体与裸 token 比较——对真实 Redis 恒为 false，持有者每 40s 自驱逐、release 从不删除记录。修复=脚本解析 JSON 比对 `token` 字段（`datacode_common.lease.CAS_DELETE/RENEW_SCRIPT` 共享），配套 `test_lease_cas_scripts.py` 直接解释 Lua 文本钉住语义（含对旧脚本的 3 条可执行反证）
  - **配额可见性**：`GET /api/v1/quotas`（契约 08 §2 冻结项）——control `src/api/rest/quotas.py` + 网关 `controlIdentity` 路由 + routemap 仅放行 GET；租户取自身份头不可指定（防枚举）；用量与执法路径同源（live COUNT），读不到返回 `null` 而非 0（与 fail-open 执法对称）；`limits_for` 补 fail-open（原 DB 故障会 500 整个端点）。前端 Settings→Organization 配额面板（`QuotaPanel.vue` + `stores/quotas.ts` + i18n）与 429 结构化文案
  - **审计表保留**：control `src/records/retention.py`（task_records 90 天锚定最后状态转移 / lease_history 30 天，分批删除防长事务）；agent `src/context/retention.py`（timeline_events 30 天 / memory_events 90 天带 summaries 显式级联——不依赖 FK enforcement 以兼容 SQLite；messages/sessions 刻意不动，其生命周期归用户显式删除）
  - **CI 补齐**：identity-service / task-runner / egress-proxy / shared-go 全部进 lint+test（Go 1.25 对齐 go.mod）；顺带清零全部存量 lint（control 33、runtime 130、agent 122），其中抓到两个 **F821 真 bug**：runtime `external_models_service._import_sync` 引用未定义的 `effective_gateway`（外部模型导入必 NameError）、agent `shell.py` 超时路径引用未定义的 `timeout_ms`（本地 shell 超时必 NameError）
  - **WORKSPACE_RELOCATING**（设计 §03.4.3 / 契约 08）：前端 axios 拦截器识别 503+`WORKSPACE_RELOCATING`，按 `Retry-After`（上限 10s）透明重试至多 4 次，期间全局横幅提示"工作副本迁移中"（`WorkspaceRelocatingBanner.vue` + i18n 中英）；重试耗尽才把错误交给调用方
  - **租约公平性**（设计 §03.6 `tenant_lease_share`）：placement 排序键前置租户压力项（该租户在候选实例上的占用/权重），大租户突发批量 acquire 时自我铺开、小租户请求优先落在其未触碰的实例；权重经 quota snapshot 下发（`TenantQuotaSnapshot.tenant_lease_share`，缺省=纯铺开即旧行为，只重排不拒绝）；share=0/缺键/Redis 故障均优雅回退
  - **清理**：删除 `conf/sqlmesh-service.yaml` / `sqlmesh-service.docker.yaml`（服务目录 P5 已删，conf 漏删）及指向它们的过期注释
- [ ] **DEFERRED · 容量规划报表**：按 Cell 输出租约/内存/磁盘/DB 增长趋势

### 验收门

- **DEFERRED** 一次跨 gateway → control → runtime → task-runner 的请求在 Tempo 里是**一条完整 trace**
- 所有 §04.6 的审计事件可查（本阶段以**结构化日志 + 审计表**为准，不要求 Loki）
- 配额超限返回正确的 429/409 + `Retry-After`
- 模拟租户超配额，验证不影响其他租户
- `LeaseController` 在利用率越过阈值时正确扩容、drain 时不丢 WIP（复用 P4a 的 WIP 验收手法）

---

## 服务演进时间线

```
       P1    P2      P3          P4      P5     P6        P7    P8
       │     │       │           │       │      │         │     │
gateway ─────┼──瘦身──┼───────────┼租约路由┼─路由─┼─────────┼─────┼──▶ edge-gateway
             │       │           │       │      │         │     │
             └拆出───────────────────────────────────────────────▶ identity-service
                     │           │       │      │         │     │
sqlmesh ─────────────┼─拆分────┐ │       │      │         │     │
ontology ────────────┼─并入──┐ │ │       │      │         │     │
dagster ─────────────┼─并入┐ │ │ │       │      │         │     │
                     │     ▼ ▼ ▼ │       │      │         │     │
                     │  workspace-runtime┼──────┼─沙箱化──┼─────┼──▶ workspace-runtime
                     │           │       │      │         │     │
                     │           └───────┴─提取─┴─────────┴─────┴──▶ control-service
                     │                          │         │     │
                     │                          └─新建────┴─────┴──▶ task-runner
                     │                                    │     │
dagster-control-plane ────────────降级为 sidecar───────────┴─────┴──▶ orchestrator
                                                                 │
agent ───────────────┼───────剥离本地执行───────────────────────────▶ agent-service
```

---

## 回滚策略

| 阶段 | 回滚方式 | 数据风险 |
|------|---------|---------|
| P1 | 镜像回滚 | 无（纯字段增加） |
| P2 | gateway 镜像回滚 + identity schema 保留 | 低（表未删，只是 SET SCHEMA） |
| P3 | 镜像回滚（旧服务镜像保留 2 个版本）；3e 的隔离可单独用 `executor.mode` 回退到进程内（**但会重新暴露 T2，仅限紧急**） | **无**（工作副本可从 Git 重建，Context 可重载） |
| P4 | **4a**：镜像回滚（网关关闭 `LeaseRouter` 回到普通负载均衡，工作副本仍在共享 PV）；**4b**：改 `materialize.mount_kind` + volume 类型即回滚，**代码无分支** | 低：4b 前 WIP 保护已上线并验证；工作副本可从 Git 重建 |
| P5 | 镜像回滚 + alembic downgrade | 中（表结构变更；用 expand-contract 保证可回滚） |
| P6 | `isolation-profiles.yaml` 把 `executor.mode` 与 `sandbox.enabled` 回退到 **L3-proc**（P3 的形态，仍保有 T2 防护） | 无（配置开关） |
| P7 | 把租户 placement 改回原 Cell | 中（需要反向迁移；保留旧 Cell 数据 7 天正是为此） |
| P8 | 镜像回滚 | 无 |

> **P6 的回滚终点是 L3-proc 而非"进程内无隔离"** —— 这是 Q1 决策把隔离前置到 P3 带来的一个额外好处：沙箱平面出问题时，回退后仍保有 T2 防护，而不是一路退回到用户代码可读 `DB_PASSWORD` 的状态。

**P4 因此不再是"难回滚阶段"**（Q7 决策），但仍要求：
- 4a 与 4b **分两次上线**，中间至少间隔一个观察周期；**WIP 保护（4a）必须先于介质切换（4b）上线并验证通过**——否则一旦切换出问题，兜底机制自身还未被验证
- 每次上线前在 staging 跑满 1 周
- 按环境分批全量：dev → staging → prod（**不做流量比例灰度**，见 P4 §"灰度取消后的等价验证手段"）
- `ephemeral` 与 `instance_local` 两种介质都保持在 CI 中被测试，避免回滚路径腐烂

---

## 里程碑与能力对照

| 里程碑 | 完成阶段 | 获得能力 | 安全等级（可对外声明） |
|-------|---------|---------|-------------------|
| **M-A** | P1+P2 | 身份受管、Cell 概念就绪、共享库统一 | 无变化 |
| **M-B** | P3 | 服务整合完成、HTTP 环路为 0、**Python model/macro 保留且不再能读平台凭证** | **用户代码无法读取平台凭证（T2）+ 资源受限（rlimit）** |
| **M-C** | P4+P5 | **可扩展到单 Cell 满载（200 租户 / 4,000 工作副本）** + 数据持久性契约生效 | 同 M-B |
| **M-D** | P6 | 沙箱执行平面（L3-base） | **租户间隔离达标（数据/网络/资源），多租户共享集群可用** |
| **M-E** | P7 | **可扩展到 1,000 租户 × 1,000 用户** | 同 M-D + 单 Cell 故障爆炸半径 20% |
| **M-F** | P8 | 可运营（可观测、可计费、可治理） | 同 M-D |
| **M-G**（待增强） | §04.8.1 | gVisor/Kata 启用 | + 抗容器逃逸（T8） |

**最小可用目标**：M-C（P1-P5，约 14.5 周）已能支撑单 Cell 200 租户 / 200,000 用户，且解决了内存放大、git 性能、缓存一致性、服务耦合四个核心问题。

**两条安全底线（因 P0 决策而明确）**：
1. **M-B（P3）之前不应让不可信用户使用平台** —— 此前用户代码可读 `DB_PASSWORD`，一次泄露即全平台失陷。
2. **M-D（P6）之前不应对外声明"租户间强隔离"** —— 此前同 Pod 文件系统与网络共享。若有合同义务，为该客户提供**独占 Cell**（ADR-006 的 `dedicated_cell`），这比内核沙箱隔离更强且立即可用。
