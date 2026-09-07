# DataCode 2.0 服务架构重构设计

> 目标：把当前 **6 个后端代码库 / 5 个 HTTP 服务** 的架构，重构为按「**状态性质 × 扩展维度 × 隔离边界**」切分的 **4 服务 + 2 类运行时**，并使其能在 **1000 租户 × 1000 子用户** 的规模下横向扩展、且每个用户工作区强隔离。
>
> 约定：**不考虑历史兼容**，允许破坏式变更。

---

## 文档索引

| # | 文档 | 内容 |
|---|------|------|
| 01 | [current-state-assessment.md](01-current-state-assessment.md) | 现状盘点、耦合分析、1000×1000 规模下的量化瓶颈 |
| 02 | [target-architecture.md](02-target-architecture.md) | 目标架构：服务整合方案、边界准则、职责矩阵 |
| 03 | [multi-tenancy-and-scaling.md](03-multi-tenancy-and-scaling.md) | Cell 单元化、工作区租约、路由与容量模型 |
| 04 | [workspace-sandbox-isolation.md](04-workspace-sandbox-isolation.md) | 工作区沙箱：分级隔离模型、执行平面外移 |
| 05 | [data-and-state-model.md](05-data-and-state-model.md) | 数据分区、状态归属、存储选型 |
| 06 | [migration-roadmap.md](06-migration-roadmap.md) | 分阶段迁移路线、验收门与回滚 |
| 07 | [decision-records.md](07-decision-records.md) | 关键架构决策（ADR）与被否决的替代方案 |
| 08 | [service-contracts.md](08-service-contracts.md) | 服务接口契约速查、路由映射、错误码、已冻结配置键 |
| — | [datacode-2.0-architecture.drawio](datacode-2.0-architecture.drawio) | 架构图（4 个 page：总览 / 租约机制 / 沙箱隔离 / 请求链路） |

**专题设计**（与 2.0 架构正交，随各服务演化）：

| 文档 | 内容 |
|------|------|
| [domain-model.md](domain-model.md) | 领域模型与业务术语（Ubiquitous Language），按 2.0 服务归属更新 |
| [rbac-permission-design.md](rbac-permission-design.md) | 控制面 RBAC、工作区归属、发布审批（identity-service 的规范） |
| [data-agent-design.md](data-agent-design.md) | Agent 架构：Triage→专业 Agent 编排、工具门控（agent-service 的规范） |
| [sqlmesh-docs-tool-design.md](sqlmesh-docs-tool-design.md) | SQLMesh 文档检索工具设计 |

> 1.x 的整体架构设计（backend-architecture / dataops-platform-design /
> datacode-production-architecture / environment-branch-deployment-design 及其
> 配图）已删除：它们描述的四个单服务边界被 2.0 重构取代，环境收敛等仍有效的
> 概念已并入上方 01–08 与各专题文档。

---

## P0 决策（已冻结 2026-08-26；Q7 补充 2026-08-27）

| # | 问题 | 决策 | 主要后果 |
|---|------|------|---------|
| Q1 | 是否禁用 SQLMesh Python model / macro？ | **不禁用** | 必须真做隔离且尽早 → **P3 增加 L3-proc**（进程级隔离），**T2 消除从 P6 提前到 P3** |
| Q2 | 是否用 gVisor / Kata？ | **暂不用**，记为待增强 | L3 上限为 **L3-base**（标准 runc 加固）；T8 容器逃逸未覆盖，缓解为用户代码专属节点池 + 内核补丁基线（§04.8.1） |
| Q3 | `max_per_instance = 16` 依据？ | **设为可配置** | 改为内存预算驱动 + 启动期自校验 + 运行期 EWMA 自适应（Context 内存随用户仓库规模差一个数量级） |
| Q4 | 需要跨 Cell 协作？ | **不需要** | Cell 化模型无需调整；租户迁移按整租户粒度 |
| Q5 | 接受 env 注入仓库凭证？ | **接受** | 派生强制约束：**可写仓库账号只允许 L2 档位**（写操作必须从 L3 拆出）；warehouse-proxy 转待增强 |
| Q6 | 接受非优雅崩溃丢 10 分钟改动？ | **接受** | 升级为**对外数据持久性契约**：需前端"最后保护时间"提示 + 恢复弹窗 + 快照失败告警 |
| Q7 | P4 需要流量灰度？ | **不需要** | 灰度会迫使网关/runtime 各维护两套并行路径，风险大于收益 → 改为**物化逻辑唯一路径 + 存储介质可配置**（`materialize.mount_kind`，`shared` 启动期拒绝）+ P4 拆 **4a**（租约+WIP）/ **4b**（介质切换）两次上线；P4 不再是难回滚阶段 |

**净影响**：总工期 21 → **22 周**（P3 +1、P4 +0.5、P6 −0.5；Q7 工期中性），但**安全达标时间大幅前移**：

| 威胁 | 原计划 | 现在 |
|------|-------|------|
| **T2**（用户代码读平台凭证 —— 使其余防护全部失效的那一条） | P6 | **P3** |
| T5 部分（资源耗尽）、T10（篡改平台文件） | P6 | **P3** |
| T1 / T3 / T6 / T7（跨租户、网络） | P6 | P6 |
| T8（容器逃逸） | P6 | **待增强**（Q2） |

**两条安全底线**：① M-B（P3）前不应让不可信用户使用平台；② M-D（P6）前不应对外声明"租户间强隔离"，有合同义务时用**独占 Cell** 替代。

---

## 一页纸结论

### 服务整合：5 服务 → 4 服务

```
【当前】                              【2.0】
gateway (Go)              ─┐
                           ├────────▶  edge-gateway (Go)        无状态 · 纯边缘
                          ─┘            + identity-service (Go)  IAM/RBAC 独立成服务

sqlmesh-service (22.4k)   ─┐
  ├ 环境注册中心 / 工作区元数据 ├────▶  control-service (Py)      无状态 · DB 驱动 · 全局可扩
  ├ Git 操作 (2164 行)      ─┤
  ├ SQLMesh Context 池     ─┤
  └ 文件/知识库             ─┘

ontology-service (2.7k)   ─┐
dagster-service (4.7k)    ─┼────────▶  workspace-runtime (Py)   有状态 · 工作区亲和 · 按租约扩
dagster-control-plane     ─┘            （SQLMesh + Ontology + Dagster code location
                                          + Git worktree + 文件 IO 全在一个进程）

agent-service (13k)       ──────────▶  agent-service (Py)       无状态 · 保留独立（LLM 扩展维度独立）

                                     ＋ orchestrator (Dagster OSS 控制面, 保持官方形态)
                                     ＋ task-runner (按 run 拉起的一次性容器)
```

### 为什么这样切

当前边界按「**外部引擎**」切（SQLMesh 一个服务、Dagster 一个服务、Ontology 一个服务）。后果是 3 个服务读同一个 `workspace_path` 的 3 个子目录：

```
{base}/{tenant}/{ws}/{user}/envs/{env}/
    ├── sql-warehouse/   ← sqlmesh-service 读写
    ├── ontology/        ← ontology-service 读
    ├── asset-dag/       ← dagster-service 读（还要 spawn gRPC 子进程）
    └── logs/            ← sqlmesh-service 写
```

一个可变目录被 3 个独立伸缩的服务共享 ⇒ 必须共享可变 PV ⇒ **无法做进程/容器级沙箱**，并且被迫产生 3 条服务间 HTTP 环路（`sqlmesh ⇄ dagster`、`ontology → sqlmesh`）。

2.0 改按 **状态性质** 切：

| 分类 | 判据 | 服务 |
|------|------|------|
| **无状态 · 全局可扩** | 只碰 DB / Redis / LLM，任意副本可服务任意请求 | `edge-gateway`、`identity-service`、`control-service`、`agent-service` |
| **有状态 · 工作区亲和** | 必须持有某个工作区的**本地文件系统 + 内存态** | `workspace-runtime`（唯一的工作区所有者） |
| **单例控制面** | 官方硬约束 | `orchestrator`（Dagster daemon 单副本 + webserver 多副本） |
| **一次性执行体** | 生命周期 = 一次任务 | `task-runner` |

**核心不变式**：一个工作区工作副本（working copy）在任一时刻**只被一个 workspace-runtime 实例挂载和写入**。由此获得三件当前拿不到的东西：① 工作区目录可以是实例私有目录（emptyDir 或 per-replica PVC）而非共享 NFS；② 可以把用户代码执行体整体隔离（P3 进程级 → P6 容器级 → 待增强内核级）；③ 工作区内的写并发不再需要分布式锁（退化为进程内锁）。

> ⚠️ 第 ③ 条依赖第 ① 条**不被运维配置破坏**：共享可写介质会绕过租约放进第二个写者，此时进程内锁不足。故 `materialize.mount_kind: shared` 在启动期被硬拒绝（ADR-003 Q7）。

### 1000×1000 规模的三个数量级问题

| 问题 | 当前 | 2.0 |
|------|------|-----|
| **共享可变 PV** | 全部工作区在一个 `workspace-base-path`，NFS/EFS 上 10^6 用户 × N 环境目录；git 操作在 NFS 上性能塌陷 | 工作区**按需从 Git 物化到实例私有目录**（`materialize.mount_kind`），共享存储只存冷备/缓存（§03.4） |
| **SQLMesh Context 内存** | `ContextPool(max_size=20)` 每 Context 数十 MB～GB，靠 `workspace_id` 网关亲和分片，但网关**没有实现该亲和路由**（router.go 无一致性哈希） | 工作区租约（lease）显式路由，Context 数 = 该 runtime 持有的租约数，容量可算（§03.5） |
| **单一控制面/单一 DB** | 一套 Postgres + 一套 Dagster 控制面服务全部租户；daemon 单副本是全局单点 | **Cell 单元化**：每 Cell 一套完整数据面+控制面+DB，租户按 shard 映射到 Cell（§03.2） |

### 服务数量为什么不能更少

不把 `agent-service` 也并进 `control-service`，因为它的扩展维度是 **LLM 并发 + 长连接（SSE）**，与 DB QPS 完全不相关：一次对话可占用一个 worker 数十分钟，而 control 的请求是毫秒级。合并会让 DB 型服务被长连接拖住扩容节奏。同理 `identity-service` 从 gateway 中**拆出**（而非合并）——身份是全平台唯一真源，把 `CREATE TABLE users/tenants/roles` 埋在网关进程里（`gateway/internal/auth/store.go:159`）意味着任何非 Go 服务都无法权威地回答"这个 user 属于哪个 tenant"。

**净结果：服务实体数从 6 降到 4（+2 类运行时），但每个服务的伸缩语义变成单一且可计算的。**
