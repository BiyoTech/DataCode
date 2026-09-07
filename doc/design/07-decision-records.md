# 07 · 关键架构决策记录（ADR）

> 每条：背景 → 决策 → 理由 → 被否决的替代方案 → 代价与残余风险。
> 状态：`proposed`（待 P0 评审）/ `accepted` / `superseded`

---

## ADR-001 · 服务边界按「状态性质」切分，而非按「外部引擎」切分

**状态**：proposed

### 背景
现有边界按外部引擎切：SQLMesh / Dagster / Ontology 各一个服务，三者读写同一个工作副本目录的不同子目录。

### 决策
按 **状态性质 × 伸缩维度 × 隔离边界** 切分，三者必须重合。分类：无状态（可自由 HPA）/ 工作区亲和（租约绑定）/ 单例控制面 / 一次性执行体。

### 理由
按引擎切导致三个可独立伸缩的服务共享同一个可变目录树，产生三个连锁后果：

1. **必须共享可变 PV** → git 操作在 NFS 上性能塌陷（`git status` 0.5-4.5 s，§01.3.3）
2. **无法沙箱化** → 目录被三个服务挂载，任一服务的沙箱化都不完整
3. **产生 HTTP 环路** → `sqlmesh ⇄ dagster` 双向、`ontology → sqlmesh`

而这三个服务操作的是**同一份不可分割的状态**：一个工作副本的文件树及其派生内存索引。`environment` 本身就同时是 Git 分支 + SQLMesh 虚拟环境 + Dagster deployment（这正是 `naming.py` 命名代数存在的原因）。把一个原子概念的三个面切到三个进程里，必然要用分布式协调把它们重新粘起来。

### 被否决的替代方案

| 方案 | 否决理由 |
|------|---------|
| 保持现状 + 加强分布式锁与缓存失效广播 | 治标不治本：共享 PV 的性能问题无法用锁解决；沙箱化仍不可能；每加一个跨服务不变式就要加一个 Redis 原语（当前已有 7 个域） |
| 反向拆得更细（plan-service / lineage-service / git-service…） | 全部访问同一工作副本，把共享可变 PV 问题放大 10 倍 |
| 单体（全部合成一个服务） | 违反 R2：LLM 并发、DB QPS、工作区内存三个维度互相绑架扩容节奏 |

### 代价
- `workspace-runtime` 达 ~20k 行。判据不是行数而是**状态域数量**：它只有一个状态域，因此内部可用进程内锁与函数调用。
- 一次性重构成本高（P3，4 周）。

---

## ADR-002 · 工作副本采用「租约」显式绑定实例，而非随机负载均衡

**状态**：proposed

### 背景
现有文档声明「ContextPool 分片键 = `workspace_id`，由网关路由保证同 `workspace_id` 落同一副本」。**实测：网关未实现该路由**（`router.go` 全部用单一 upstream + K8s Service 随机 LB）。

### 决策
显式租约：`lease_key = {tenant}/{workspace}/{user}/{env}` 通过 Redis `SET NX EX` 排他绑定到一个 `workspace-runtime` 实例，网关按租约路由。TTL 是 crash 恢复界（watchdog 续期）。

### 理由
工作副本是**有状态且不可共享**的（文件树 + 数百 MB 内存索引 + 子进程）。随机路由下：

| 后果 | 量化 |
|------|------|
| 内存放大 N 倍 | 2,000 并发编辑需 400 GB 且无法命中；租约模型只需 84 GB 且 100% 命中 |
| `dirty` 标志失效 | 副本 A 写文件，副本 B 的 Context 仍旧 → **读到过期模型**（正确性问题） |
| LRU 疯狂驱逐 | `max_size=20` × 8 副本 = 160 槽位 vs 2,000 需求 |
| 需要分布式锁 | `env_lock` 的存在本身就是随机路由的补偿 |

额外收益：**消除一整类缓存一致性问题**，`env_lock` 退化为进程内锁。

### 被否决的替代方案

| 方案 | 否决理由 |
|------|---------|
| K8s `sessionAffinity: ClientIP` | 粒度错误：亲和键应是工作副本而非客户端 IP。一个用户开两个工作区应落不同实例；NAT 后多用户同 IP 会挤到一个实例 |
| 一致性哈希（无租约） | 副本数变化时大量重映射（Context 全部冷启动）；无法表达"该实例正在 drain" |
| Sticky cookie / header 亲和 | 只覆盖浏览器请求；agent-service、reconciler、Dagster run 的服务间调用无 cookie |
| 每个工作副本一个 Pod | 20,000 Pod per Cell，调度器与 kubelet 撑不住；Pod 启动开销 >> 租约获取开销 |
| Context 存共享内存（Ray/Redis） | SQLMesh Context 不可序列化（SQLGlot AST、引擎连接、Python 对象） |

### 代价与残余风险
- 网关新增租约路由中间件，Redis 进入热路径（缓解：本地缓存 10s，命中率 >95%）
- **Redis 长时间不可用 + 副本数变化 → 可能短暂双挂载**。三道防线：Redis SET NX / 文件 `flock` / Git worktree 自身约束。最坏后果是一个用户的未提交改动冲突（Git 报错而非静默损坏），**不是跨租户泄露**。显式接受此风险。
- runtime 缩容需控制器编排 drain（不能用 HPA 直接杀 Pod）

### Q3 修订：每实例租约数是**可配置的内存预算函数**，不是魔数

原设计写死 `max_per_instance = 16`，其依据是"假设 Context ≈ 200 MB"。**该假设未经实测，不能作为固定容量。** 决策：改为三层机制。

**① 配置化（P0 冻结的契约）**

```yaml
lease:
  # Hard cap. 0 means "derive from the memory budget below only".
  max_per_instance: 16
  # The real driver: the runtime admits a new lease only while the projected
  # footprint stays under this budget. Sizing the pods differently (4 / 8 /
  # 16 GiB) therefore changes capacity without a code change.
  memory_budget_bytes: 6979321856        # 6.5 GiB = limit 8 GiB - 1.5 GiB baseline
  # Per-lease cost estimate, refined at runtime by actual measurements.
  estimated_lease_bytes: 220200960       # 210 MiB (SQLMesh metadata + ontology)
  # Refuse new leases once real RSS crosses this fraction of the pod limit,
  # even if max_per_instance has not been reached — the estimate can be wrong.
  admission_rss_high_watermark: 0.80
```

**② 启动期自校验**：runtime 启动时读取自身 cgroup 内存 limit，校验 `max_per_instance × estimated_lease_bytes + baseline ≤ limit`。不成立则**按预算下调有效上限并告警**（`lease_capacity_downgraded`），而不是接受一个会 OOM 的配置。

**③ 运行期自适应**：每次租约释放时记录该租约的实际内存占用（RSS 增量），用 EWMA 更新 `estimated_lease_bytes` 的运行时估计值。若实测值持续高于配置值 20% 以上，降低有效 `max_per_instance` 并上报指标 `lease_estimate_drift`。**这使容量模型能自我纠正**，而不依赖 P0 那一次实测的准确性。

**为什么必须自适应而不只是"实测一次填进配置"**：Context 内存与**用户仓库规模强相关**（模型数、SQL 复杂度）。一个 50 模型的仓库与 2000 模型的仓库差一个数量级。固定值在租户间必然错配 —— 大仓库租户会 OOM，小仓库租户浪费资源。

**P0 仍需实测的内容**（用于设定初始值，不再是唯一依据）：对 3 个代表性规模的仓库（小 ~50 模型 / 中 ~500 / 大 ~2000）分别测 `Context()` 构造后的 RSS 增量，据此设定 `estimated_lease_bytes` 与告警阈值。

---

## ADR-003 · 工作副本存实例私有目录，Git 是唯一真源，介质可插拔

**状态**：proposed

### 背景
现有设计要求共享 PV 以支持多 Pod 挂载。10^7 文件规模下 `git status` 需 0.5-4.5 s。

### 决策
工作副本（`.repo` + `envs/{env}` worktree）存**实例私有目录**（`materialize.root`）。该目录的介质由部署声明（`emptyDir` / per-replica RWO PVC / `hostPath`），**物化逻辑只有一条代码路径且不感知介质**。共享存储只作为可选的 `.repo` 冷层快照。**Git Server 是唯一真源。**

### 理由
1. **代码已承认这一点**：`workspace/paths.py` 文档写明 *"Local directories are disposable, so they are never persisted in the database — they are always derived from (base_path, tenant_id, workspace_id, user_id, environment) at runtime"*。derived state 不需要**共享**持久卷（per-replica PVC 只是缓存，丢了只是变慢）。
2. **性能差一到两个数量级**：EFS 元数据 1-3 ms/op vs 本地 0.05 ms/op。`git status` 从秒级降到 30 ms。
3. **ADR-002 的租约已保证单挂载**，共享挂载能力不再被需要。
4. **成本**：10^7 文件在 EFS 上元数据成本显著；实例私有盘是节点自带或 per-replica 块存储。

### 被否决的替代方案

| 方案 | 否决理由 |
|------|---------|
| 共享 PV（现状） | 性能塌陷（10-45× 差距）；并发写风险只能靠约定 |
| 每用户一个 PVC | 10^6 个 PVC，etcd 对象数与 attach/detach 延迟不可行 |
| 一个 RWX PVC 挂所有副本 | "换了名字的共享 NFS"：抵消 git 性能收益，并绕过租约的单挂载不变式（Q7 修订：`mount_kind: shared` 启动期硬拒绝） |
| tmpfs 全内存 | 大仓库放不下；节点重启全丢且无 WIP 保护窗口 |
| 对象存储 FUSE（s3fs/goofys） | git 需 POSIX 语义（rename 原子性、文件锁），FUSE 层不满足 |
| **流量灰度 + 共享 PV 双跑**（原 P4 回滚方案） | Q7 修订否决：要求网关与 runtime 同时维护两套路径/两套存储语义，**并行代码路径本身就是比它所降低的更大的风险**。改为单代码路径 + 介质可配置 + 4a/4b 分阶段上线 |

### 代价与残余风险
- **未提交改动在实例目录消失后丢失**（本决策引入的新风险）。四层缓解：前端草稿存 DB / drain 时 push `refs/datacode-wip/*` / 每 10 分钟周期快照 / 物化时恢复提示。等级：优雅下线零丢失，非优雅崩溃最多丢 10 分钟。生产默认 `mount_kind: instance_local` 后，部署与 OOMKill 不再触碰该窗口（见 Q7 修订）。
- 首次物化耗时。缓解：shallow clone + `.repo` 冷层快照 + 同实例 worktree 缓存复用。
- 需要磁盘水位管理（LRU 回收无租约 worktree）。
- **配置误用风险**：`mount_kind` 若被配成共享介质会造成静默数据损坏。缓解：启动期硬失败，而非文档提醒。

### Q6 修订：10 分钟窗口成为正式的**对外数据持久性契约**

**Q6 决策：接受"非优雅崩溃最多丢 10 分钟改动"。** 这从"设计假设"升级为**产品契约**，因此必须被明确定义、实现、测试与告知：

**契约表述（需进入产品文档与前端提示）**：

| 场景 | 保证 |
|------|------|
| 用户主动保存（编辑器 Ctrl+S / 调用 write API） | **零丢失**（写入工作副本 + 草稿落 DB） |
| 编辑器内未保存的输入 | 最多丢 **5 秒**（前端自动保存草稿到 control-service） |
| 已写入工作副本但未 commit 的改动 + 优雅下线（部署/缩容/drain） | **零丢失**（drain 时 push `refs/datacode-wip/{user}/{env}`） |
| 已写入工作副本但未 commit 的改动 + **非优雅崩溃**（节点宕机/OOMKill/kill -9） | 最多丢 **10 分钟**（`wip_snapshot_interval_seconds = 600`） |
| 已 commit 未 push | **零丢失**（commit 在 `.repo`，随 WIP 快照一并保护） |
| 已 push | 零丢失（Git Server 是真源） |

**配置化**（不同租户可有不同要求）：

```yaml
materialize:
  # Bounds how much uncommitted work a NON-GRACEFUL crash can lose. This is a
  # user-facing durability contract (design 07 ADR-003 / Q6), not a tuning knob:
  # changing it changes what the product promises.
  #
  # Cost of shortening it: each snapshot is a `git stash create` plus a push to
  # a hidden ref, so the write amplification on the Git server scales inversely.
  wip_snapshot_interval_seconds: 600     # 10 min — the accepted default
  # Skip the snapshot when nothing changed, so idle leases cost nothing.
  wip_snapshot_skip_if_clean: true
```

**必须实现的配套项**（否则契约不成立）：

| 项 | 说明 |
|----|------|
| **前端提示** | 工作区界面显示"最后保护时间"（last WIP snapshot at ...），让用户对风险窗口可见 |
| **恢复流程** | 物化时检测 `refs/datacode-wip/{user}/{env}` → 弹窗"发现未提交改动（快照于 X 分钟前），是否恢复"→ 用户可选择恢复或丢弃 |
| **快照失败告警** | WIP push 失败（Git server 不可达）必须告警且**在前端降级提示**"当前改动未受保护"，否则用户以为有保护而实际没有 |
| **验收测试** | P4 验收门必须包含：写入改动 → 等待快照 → `kill -9` runtime → 新实例物化 → 验证改动可恢复 |

⚠️ **一个诚实的补充**：10 分钟契约仅覆盖**已写入工作副本文件系统**的改动。若用户在编辑器里输入但未触发保存，则由前端 5 秒草稿机制覆盖 —— 两个机制的**接缝**（已保存到前端草稿但未写入工作副本）需在实现时确认无空洞。

### Q7 修订：不做流量灰度，改为介质可配置与分阶段上线

**Q7 决策：P4 不做流量灰度；物化目录的存储介质做成可配置（`materialize.mount_kind`），P4 拆成 4a/4b 两次上线。**

#### 为什么砍掉灰度是**降低**风险而不是提高

原方案要求「先让 10% 工作区走租约路由 + 保留共享 PV 双跑」。这个做法自相矛盾：

| 灰度带来的 | 后果 |
|-----------|------|
| 网关同时维护「租约路由」与「随机路由」两条转发路径 | 两套路径的行为差异本身成为 bug 来源，且只有 10% 流量覆盖新路径 |
| runtime 同时支持「共享 PV 直接用」与「本地物化」两套存储语义 | `dirty` 标志、`env_lock`、LRU 回收在两套语义下含义不同，需双份测试 |
| 两套语义在同一版本共存 | **并行代码路径是 P4 的最大风险源**——而灰度的目的正是降低风险 |

**用来降低风险的手段引入了比它所降低的更大的风险。** 所以正确解法不是"更小心地灰度"，而是**消除可灰度的对象**。

#### 消除方式：物化逻辑与存储介质正交

| 维度 | 内容 | 是否可配置 |
|------|------|----------|
| **物化逻辑** | `git clone --bare` → `.repo` → `git worktree add` | ❌ 唯一路径，永不分支 |
| **存储介质** | `emptyDir` / per-replica PVC / `hostPath` | ✅ `mount_kind` + K8s volume 类型 |

物化逻辑不关心底下是 NVMe 还是网络块存储，它只需要一个可写目录。于是**不存在"新旧两条路径"**，只存在"同一条路径跑在不同介质上"。介质切换的回滚 = 改一个配置值 + 换 volume 类型，**代码零分支**。

#### 配置契约

```yaml
materialize:
  # The ONE writable directory working copies are materialized into. The medium
  # behind it is a deployment concern; this service only ever sees a path and
  # always derives content from Git.
  root: "/var/lib/datacode/workcopies"
  # Declares what the mount actually IS, so the runtime can REFUSE a
  # configuration that breaks the single-mount invariant instead of silently
  # corrupting working copies.
  #   ephemeral      -> emptyDir. Nothing survives a pod restart.
  #   instance_local -> PVC bound to THIS replica only (volumeClaimTemplates,
  #                     RWO). Working copies and .repo survive restarts.
  #   shared         -> REJECTED AT STARTUP (see invariants below).
  mount_kind: "instance_local"   # production default
```

**两个不可让步的约束**（结构性防住，不靠文档提醒）：

1. **`mount_kind: shared` 启动期硬失败。** 单挂载不变式由租约保证，与介质无关。若介质被配成共享 RWX 卷而租约被关，就回到并发写损坏风险——**且比 1.x 更糟**，因为此时 `env_lock` 的 Redis 后端已按设计退化为进程内锁。这是「一个看起来无害的运维配置造成静默数据损坏」的典型路径，必须在启动期拦截。
2. **`instance_local` 必须是 per-replica RWO PVC**（StatefulSet `volumeClaimTemplates`），不是挂给所有副本的 RWX PVC。前者是"持久化的本地盘"，后者是"换了名字的共享 NFS"，会把本 ADR 的全部收益（git 性能、无分布式锁、租约不变式）一次性抵消。
   另外 `mount_kind == "instance_local"` 时启动期额外断言租约子系统已启用。

#### 一个正面副作用：介质可配置是永久能力，不是临时开关

这个定位差别很重要——**临时开关会腐烂（没人测），永久能力会被持续验证**。`instance_local` 实质缩小了 Q6 的 10 分钟契约要兜底的场景：

| 事件 | `ephemeral` | `instance_local` |
|------|-----------|-----------------|
| 滚动更新 / Pod 重启 | 工作副本丢失，靠 WIP ref 恢复 | **原地保留，零丢失、零 re-clone** |
| OOMKill | 同上 | **同上** |
| 节点故障 / PVC 不可用 | 丢失 | 丢失（**此时才需要 WIP 快照**） |
| 大仓库首次物化 | 每次重启都要重 clone | **`.repo` 缓存存活** |

10 分钟契约仍然是对外承诺（覆盖节点级故障），但**日常运维动作不再消耗这个窗口**。故生产默认 `instance_local`，`ephemeral` 留给无 PV 或对启动速度敏感的环境；两者都必须留在 CI 中被测试，否则回滚路径会腐烂。

#### 灰度取消后必须补上的等价验证

灰度真正的价值不是降低介质切换风险（已由单代码路径消除），而是**用真实流量验证租约路由本身的正确性**。这部分换成等价手段：

| 灰度原本要验证的 | 替代手段 |
|-----------------|---------|
| 同 lease_key 100% 落同实例 | staging 压测 + trace 断言（4a 验收门，已有） |
| 租约 miss/acquire 竞态 | **并发 acquire 压测**：对同一 lease_key 并发 100 次 acquire，断言只产生一个租约（4a 验收门，**新增**） |
| Redis 降级路径 | 混沌测试：断开 Redis，验证稳定哈希路由仍单写者（已有） |
| drain 不丢数据 | 手工 drain + WIP 恢复验证（已有） |
| 真实用户行为长尾 | **按环境分批全量**：dev → staging（1 周）→ prod。代码单路径，风险由部署顺序控制而非流量比例 |

#### 派生的强制顺序约束

**WIP 保护（4a）必须先于介质切换（4b）上线并验证通过。** 原路线图把两者都放在 P4 同期是隐患：一旦介质切换出问题，兜底机制自身还未被真实验证。故 P4 拆分为：

- **4a**（2 周）：租约服务端/客户端 + 网关租约路由 + WIP 契约 + 容量自适应 + StatefulSet 化。工作副本**仍在原共享 PV 上**。
- **4b**（1 周）：仅落 `mount_kind` 与 volume 类型切换 + 启动期校验 + LRU 回收 + `env_lock` 退化为进程内锁。

⚠️ **`env_lock` 的退化必须在 4b 而非 4a**：4a 期间介质仍是共享 PV，此时 Redis 不可用 + 副本数变化会让两个副本写**同一个目录**（介质切换后它们写各自的目录，最坏后果从"目录损坏"降为"缓存不一致"）。租约保证的是正常路径，降级路径的后果取决于介质——所以锁的移除必须跟随介质切换，不能提前。

回滚：4a 关闭 `LeaseRouter` 回普通负载均衡；4b 改 `mount_kind` + volume 类型。**P4 因此不再是"唯一难回滚的阶段"。**

---

## ADR-004 · 用户代码在独立沙箱容器执行，永不进入平台进程

**状态**：proposed

### 背景
现状三条路径让用户代码在平台进程内执行：Agent shell（`shell.py:94` subprocess）、Dagster asset（code location 子进程 import 用户模块）、SQLMesh Python model/macro（Context 内 exec）。防线只有正则黑名单，其策略文件自述 *"the sandbox isolation is the primary guard"* —— **而那个 guard 不存在**。

### 决策
分级隔离（L1 平台内数据处理 / L2 标准容器跑用户 SQL / L3 跑用户任意代码）。所有 L2/L3 执行移出平台进程：无平台凭证、default-deny 网络、非 root、只读 rootfs、cgroup 限制、只挂载单个工作副本。

**L3 因 Q1/Q2 决策拆为三档演进**（详见 §04.2.3）：

| 档位 | 形态 | 阶段 | 覆盖威胁 |
|------|------|------|---------|
| **L3-proc** | 平台 Pod 内的独立 OS 进程（独立 UID + **清空 env** + rlimit） | **P3** | **T2**（最致命）、T5 部分、T10 |
| **L3-base** | 独立容器（标准 runc + 全套 securityContext + 网络隔离） | **P6** | + T1 / T3 / T5 / T6 / T7 |
| **L3-strong** | L3-base + gVisor/Kata | **待增强**（§04.8.1） | + T8 |

### 理由
**T2（用户代码读平台凭证）使所有其他防护无意义**。只要共享进程空间，`os.environ` 就能拿到 `DB_PASSWORD` / `OPENAI_API_KEY` / `TOKEN_ENCRYPTION_SECRET`。在 1,000 租户共享集群下这不可接受。

而 Dagster asset 与 SQLMesh Python model 是**产品的正常功能**（用户当然要写），不能用"禁止"解决，只能用隔离解决。

分级而非一律最强：内核沙箱有 syscall 开销，纯 SQL 执行（无宿主代码执行能力）用标准 runc 足够。**安全默认：无法判定级别时用当前最强已启用档位**（`default_profile: strongest_enabled`）。

### 被否决的替代方案

| 方案 | 否决理由 |
|------|---------|
| 加强正则黑名单 | 策略文件自述无法防 base64/eval/变量展开；且对 Dagster/SQLMesh 的 Python 代码完全无效 |
| Python 沙箱（RestrictedPython / seccomp-in-process） | 同进程内的语言级沙箱历史上反复被绕过；且用户代码需要真实 import 第三方库 |
| 一律最强隔离（全部内核沙箱） | 纯 SQL 场景付不必要的 syscall 开销；plan 延迟不可接受。且 Q2 已决定暂不引入内核沙箱 |
| 每租户独立命名空间/集群（硬多租户） | 1,000 个命名空间的运维成本；且不解决"同租户内的用户互相隔离" |
| 只做网络隔离不做进程隔离 | 不解决 T2（凭证读取）与 T5（噪声邻居） |

### 代价与残余风险
- **冷启动**：Agent shell 从 50 ms 升到 **80 ms**（L3-proc，P3）/ **200 ms**（L3-base + warm pool，P6）；plan 在 L3-proc 下**保持 2-10 s**（子进程常驻，Context 仍热），L3-base 下 3-12 s。
- **内存成本**：per-lease warm sandbox 约 200-600 GB per Cell（P6 起）。L3-proc 阶段无额外容器开销，仅子进程本身。
- **残余风险 1**：用户仍能读到「他自己工作区配置的仓库凭证」（**Q5 已接受**）。派生强制约束：可写账号只允许 L2 档位。warehouse-proxy 转为待增强项（§04.8.2）。
- **残余风险 2（Q2 决策引入）**：**T8 容器逃逸未覆盖** —— L3-base 用标准 runc 与宿主共享内核。缓解为用户代码专属节点池 + 节点内核补丁基线 + 全套 securityContext。记为待增强项（§04.8.1），`L3_strong` profile 已预留。
- **残余风险 3**：P3-P6 之间（L3-proc 阶段）T1/T3/T6/T7 未覆盖，**不可对外声明"租户间强隔离"**。高敏感客户在此期间应给独占 Cell。

---

## ADR-005 · SQLMesh Context 分裂为「平台内只读元数据视图」+「沙箱内可执行 Context」

**状态**：proposed（**Q1 决策已冻结：保留 Python model/macro 能力**）

### 背景
矛盾：Context 加载成本高（秒～十秒级），必须缓存在平台进程内才有可用的交互延迟；但 Context 构造时会 `exec` 用户 Python model/macro，违反 ADR-004。

**Q1 决策：不接受禁用 Python model/macro。** 因此无法用"校验期拒绝"规避，必须真做隔离，且用户要求尽早（提前到 P3）。

### 决策
两级模型：
- `MetadataContext`（runtime 内存）：纯数据的模型元数据视图（模型名/列/依赖/血缘/渲染后 SQL），服务 `/models` `/lineage` `/schemas` 等读请求，毫秒级
- 真正的 `sqlmesh.Context`：在**隔离的执行体**内常驻，负责 plan/apply/render/evaluate/test，通过 **UDS + 长度前缀 JSON** 与 runtime 交互（毫秒级）

**执行体形态分两步（因 P3 无法承载容器形态）**：

| 阶段 | 执行体形态 | 档位 | 依赖 |
|------|-----------|------|------|
| **P3** | `SqlMeshExecutor` —— **持久子进程**（同 Pod，独立非特权 UID，env 清空，rlimit） | L3-proc | 无（仅 Python 标准库） |
| **P6** | per-lease **warm sandbox 容器** | L3-base | 租约（P4）+ 容器基础设施（P6） |

**关键设计约束：通信协议与元数据序列化契约在 P3 就冻结**（UDS + 长度前缀 JSON）。P6 只是把"子进程"换成"容器"，`SqlMeshExecutor` 的协议与调用方代码不变。**因此 P3 的工作不是临时方案，而是 P6 的第一步。**

### 理由
不能既要"Context 在平台进程内以获得低延迟"又要"用户代码不在平台进程内"。唯一出路是把 Context 一分为二：**数据留在平台，代码执行进隔离体**。

**为什么 warm sandbox 容器无法真的提前到 P3**（这是对用户诉求的技术回应）：

```
"per-lease warm sandbox" 的字面含义就包含了它的依赖：
  per-lease            → 租约机制在 P4 才交付
  scoped task token    → control-service 的任务表在 P5/P6
  NetworkPolicy / 节点池 → K8s 部署形态在 P6
  egress-proxy         → 独立组件，P6
⇒ 把它塞进 P3 等于把 P4+P5+P6 的前置条件全部提前，实际是整体重排而非局部提前

而 Q1 的真实诉求是「不禁用 Python model 且尽早消除攻击面」。
L3-proc 用 fork + setuid + 清空 env + rlimit 满足了后半句：
  它在 P3 就消除 T2（平台凭证读取）—— 而 §01/§04 已论证 T2 是
  「使其余所有防护失效」的那一条。
```

**持久子进程而非每次 fork**：每次 plan fork 会让 Context 重建（10-30 s），不可接受。每工作副本一个常驻 executor 使 Context 保持热态，隔离成本降至一次 UDS 往返（< 1 ms）。

### 被否决的替代方案

| 方案 | 否决理由 |
|------|---------|
| 每次 plan 冷启动 task-runner | 15-40 s 延迟，用户体验不可接受 |
| Context 完全留在平台进程（现状） | 违反 ADR-004，T2 未解决 |
| 永久禁用 Python model/macro | **Q1 已明确否决**：丧失 SQLMesh 重要能力（动态模型、可复用 macro），产品竞争力受损 |
| 在 P3 直接做 warm sandbox 容器 | 依赖租约（P4）与容器基础设施（P6），实际等于整体重排路线；且 P3 尚无 K8s 部署形态可依托 |
| P3 完全不做隔离，等 P6 | Q1 拒绝禁用 Python model ⇒ P3-P6 期间攻击面完全敞开（用户代码可读 `DB_PASSWORD`），不可接受 |
| Context 序列化到 Redis 共享 | 不可序列化（见 ADR-002） |

### 代价与残余风险
- **P3 的 L3-proc 是不完整隔离**：同 Pod、同 netns、同文件系统视图。它消除 T2/T5部分/T10，但 T1/T3/T6/T7 要到 P6。因此 **P3-P6 期间不可对外声明"租户间强隔离"**（§04.2.3 的等级表）。
- **元数据序列化契约需精心设计**（哪些字段必须回传、如何表达血缘）。它在 P0 冻结、P3 实现、P6 复用，**一旦冻结不应变更** —— 变更意味着 runtime 与 executor 两侧同时改。
- **进程/容器边界带来的行为差异**：Context 从"同进程对象"变为"跨 UDS 的远端对象"，需审计返回值中是否有不可序列化对象（SQLGlot AST、连接对象）泄漏到 API 层。P3 验收门已含此项。
- warm sandbox 的资源成本（P6 起，约 200-600 GB/Cell，见 §04.3.7）。
- **产品能力不受限**（Q1 决策的直接结果）：SQLMesh Python model 与自定义 macro 全程可用，无功能取舍。

---

## ADR-006 · Cell 单元化：租户按显式映射表分片到独立 Cell

**状态**：proposed

### 背景
三个东西在架构上**无法水平扩展**：Dagster daemon（官方硬约束单副本，且要串行遍历所有 code location）、Dagster storage（20,000 环境的 event log 单库扫不动）、Reconciler 全局对账。

### 决策
Cell = 完整自足的平台实例（gateway + control + agent + runtime + orchestrator + PG + Redis + Event Store），服务约 200 租户。租户通过 Global DB 的 `tenant_placement` 表**显式映射**到 Cell。全局层只有 `identity-service` + Global DB + 全局边缘路由（按 `JWT.cell_id`）。

### 理由
1. **Cell 化不是可选优化，是那三项的唯一解**。加副本解决不了 daemon 单副本约束。
2. **爆炸半径**：单 Cell 故障影响 20% 租户（5 Cell），当前架构下任何单点影响 100%。
3. **升级安全**：逐 Cell 滚动，失败即止，限制爆炸半径为 1 个 Cell。
4. **代码代价为零**：只要从一开始把 `cell_id` 作为一等概念（JWT claim / 日志字段 / 指标 label），从 1 Cell 扩到 5 Cell 是纯运维动作。**因此 P1 就引入 `cell_id`，即使只有一个 Cell。**

### 被否决的替代方案

| 方案 | 否决理由 |
|------|---------|
| 一致性哈希分片 | 租户规模差异巨大（一个大租户 = 100 个小租户），哈希无法均衡；无法表达"大租户独占 Cell"；迁移不可控 |
| 只分片数据库不分片控制面 | 不解决 daemon 单点（最核心的问题） |
| 每租户一个 Cell | 1,000 套 Dagster 控制面 + 1,000 个 PG，运维成本与资源浪费不可接受 |
| 不分片，把 Dagster 换成自研调度器 | Dagster 的 asset/schedule/sensor/backfill 模型是核心价值，自研成本远超收益 |

### 代价与残余风险
- **跨库约束**：`workspaces`（Cell）与 `workspace_members`（Global）无法 FK。用 Saga + 孤儿对账补偿。
- **identity-service 是设计上唯一的全局单点**。降级路径必须是"已登录可用"（JWT 本地验签 + 授权缓存），只有新登录受影响。
- Cell 迁移需要工具与演练（P7 交付）。
- 跨 Cell 协作（不同 Cell 的租户共享工作区）**不支持**。**Q4 决策已确认不需要**，因此本 ADR 的核心假设成立。

### Q4 确认后的产品约束（需明确写入产品文档）

| 约束 | 说明 |
|------|------|
| 工作区共享的边界 | 工作区成员必须与工作区**同租户**（现有 `workspace_members` 模型已如此），而同租户必然同 Cell（`tenant_placement` 是租户粒度）⇒ **共享天然不跨 Cell，无需额外限制** |
| 不支持的场景 | ① 跨组织协作（供应商与客户共享一个工作区）；② 一个用户同时是两个不同 Cell 租户的成员并需要在**同一界面**跨租户操作（可切换租户，但不能同屏聚合） |
| 若未来需要 | 需重新设计 Cell 模型（≥6 周），选项有：全局工作区注册表 + 跨 Cell 读代理 / 把协作方所在租户迁到同 Cell / 引入"联邦工作区"概念。已记为待增强项（§04.8.3） |
| **放置策略的隐含要求** | 由于共享不跨 Cell，租户迁移 Cell 时必须**整租户一起迁**（不能只迁部分工作区）。P7 的迁移工具已按此设计 |

---

## ADR-007 · identity-service 从 gateway 拆出为独立服务

**状态**：proposed

### 背景
`gateway/internal/auth/store.go:147-247` 裸 `CREATE TABLE IF NOT EXISTS` 建 9 张身份/RBAC 表；权限点在 Go 代码硬编码。Python 侧服务只能从 HTTP header 读 `X-User-ID`/`X-Tenant-ID`，无法自己做授权判断。

### 决策
拆出 `identity-service`（Go），拥有全部身份/RBAC 表（改为 golang-migrate 受管迁移）+ `tenant_placement`/`cells`。提供 `/internal/authz/resolve` 供任意服务查询授权。gateway 保留决策逻辑与缓存，数据来源改为调用。

### 理由
1. **身份是全局的，gateway 是每 Cell 的**。身份表不能跟着 Cell 走。
2. **非 HTTP 入口完全绕过 RBAC**：Dagster schedule 触发、reconciler 后台任务、CLI shim 都没有网关中间件。它们需要一个可调用的授权 API。
3. **`users` 表加一列在当前架构下没有安全做法**（裸 `CREATE TABLE IF NOT EXISTS` 无版本、无回滚）。
4. **同一聚合根的两半分属两个语言两个代码库**：`workspaces` 由 Python ORM 建，`workspace_members` 由 Go 裸 SQL 建。
5. gateway 应可被替换成任意 Ingress/Envoy 而不丢身份能力。

### 被否决的替代方案

| 方案 | 否决理由 |
|------|---------|
| 留在 gateway，Python 侧信任 header | 非 HTTP 入口无解；且身份表随 Cell 部署会导致跨 Cell 用户不一致 |
| 并入 control-service（Python） | 需重写全部 Go 鉴权代码（JWT、bcrypt、RBAC 缓存），无收益；且 control 是每 Cell 的 |
| 用外部 IdP（Keycloak / Auth0）完全替代 | 平台的 RBAC 模型（tenant/workspace 双层 scope + 自定义权限点）在通用 IdP 中难表达；且 `tenant_placement` 是平台特有概念。**但 OIDC 联邦登录应作为 identity-service 的一个能力接入** |

### 代价
- gateway 热路径新增一次网络调用（缓解：本地缓存 TTL 60s + `perms_version` 失效，命中率极高）
- identity-service 不可用时权限变更生效延迟（降级：用缓存继续服务）
- 一次性迁移工作（P2，2 周）

---

## ADR-008 · agent-service 保持独立，不并入 control-service

**状态**：proposed

### 背景
整合的直觉是"服务越少越好"。agent-service（13k 行）看起来可以并入 control-service。

### 决策
保持独立。

### 理由
**伸缩维度完全不同**：

| | agent-service | control-service |
|---|---|---|
| 单请求时长 | 数十秒～10 分钟（goal loop `timeout_seconds: 600`） | 毫秒级 |
| 瓶颈资源 | LLM 配额 + 长连接数 + 内存 | DB 连接池 + CPU |
| 扩容指标 | `active_conversations`（CPU 利用率低但连接占满） | CPU / DB pool utilization |
| 故障模式 | LLM provider 抖动 | DB 慢查询 |

合并后：① control 的扩容被长连接绑架（缩容会杀掉进行中的对话）；② LLM 抖动会影响元数据 API 的 SLA；③ 用 CPU 扩容会导致 agent pod 过载而不扩（IO 等待型负载 CPU 利用率低）。

### 被否决的替代方案
| 方案 | 否决理由 |
|------|---------|
| 合并为一个服务 + 内部线程池隔离 | 进程级资源（内存、连接数、GIL）无法用线程池隔离；OOM 会带走整个进程 |
| 合并但用不同的 Deployment 跑同一镜像（按 env 开关角色） | 等价于两个服务但共享代码库耦合；且镜像体积与依赖树混合（LLM SDK vs DB 驱动） |

### 代价
服务数不能降到 3。**这是正确的代价：服务数不是优化目标，伸缩语义的单一性才是。**

---

## ADR-009 · Dagster code location 从「常驻子进程」改为「按需独立 Pod」

**状态**：proposed

### 背景
现状每个活跃 deployment 常驻一个 gRPC 子进程 in dagster-service。规模下：20,000 需求 vs 3,200 端口上限（`50 × 64`）；内存 3-6 TB。

### 决策
三态生命周期（cold / warm / hot），warm 时作为**独立 K8s Pod**（不是 runtime 的子进程），由 control-service 的 deployment 控制器管理，workspace-sync 从 K8s Endpoints 发现。

驱动规则：① 有启用 schedule/sensor 的 deployment 必须 warm（非 prod 默认 `SCHEDULES_ENABLED=false`，故绝大多数分支环境是 cold）；② 用户打开 Asset DAG 页面按需 warm，5 分钟无访问回 cold；③ run 由 task-runner 自己加载用户代码。

### 理由
1. **常驻模型在这个规模下不成立**（6.25× 端口缺口、TB 级内存）。
2. **按需后容量重算可行**：单 Cell 只需 ~1,100 个 warm location（1,000 prod + 100 按需分支），在 3,200 端口上限内。
3. **独立 Pod 而非 runtime 子进程**：1,100 × 200 MB = 220 GB per Cell，若塞进 runtime 会击穿其内存预算（§03.6 的修正）。独立 Pod 还可用低优先级节点池 + Spot。
4. **它 import 用户代码，必须是 L3 隔离**（ADR-004）。独立 Pod 是唯一能给它施加容器级 securityContext / NetworkPolicy 的形态，也是未来启用内核沙箱（§04.8.1）的唯一入口。

### 被否决的替代方案

| 方案 | 否决理由 |
|------|---------|
| 调大端口段（`segment_size` / `max_segments`） | 解决端口不解决内存（3-6 TB） |
| 全部常驻但用更小的进程 | code location 必须 import dagster + 用户代码，内存下限约 150 MB，无压缩空间 |
| 一个 code location 服务多个 deployment | Dagster 的 location = 一份 Definitions，无法在一个进程内隔离多份用户代码（且违反 L3 隔离） |
| 放弃 code location，改用 Dagster 的 `python_module` | 已被现有设计否决（`Selector` schema 拒绝自定义字段，且控制面会自己 spawn 子进程加载它没有的代码） |

### 代价
- 首次打开 Asset DAG 页面有 warm 延迟（5-15 s）。缓解：前端展示 loading + 预测性预热（用户打开工作区时预热其 prod 环境）。
- deployment 控制器是新组件（K8s Deployment 的 CRUD 编排）。

---

## ADR-010 · task-runner 不做常驻池化，用 warm pool 缓解冷启动

**状态**：proposed

### 背景
每任务一容器带来冷启动开销（标准 runc Pod 1-3 s；若启用内核沙箱则 3-8 s）。直觉是做常驻 worker 池。

### 决策
task-runner 保持"每任务一容器，跑完销毁"。冷启动用 **per-tenant warm pool**（1-2 个预热的空 runner，任务到达时 bind-mount 工作副本后立即执行，用完销毁并补充）缓解。

### 理由
常驻池化会丧失两个核心属性：
1. **每任务一故障域**：常驻 worker 跑过任务 A 后跑任务 B，A 留下的状态（临时文件、修改的环境、后台进程、内存中的凭证残留）会影响 B。跨租户复用尤其危险。
2. **每任务一沙箱**：sandbox 的价值在于任务结束即整体销毁。复用等于把容器隔离降级为"进程隔离"。

warm pool 保留了两者：pool 里的 runner 是**未使用过的干净容器**，只是提前启动好了。**per-tenant** 而非全局池，避免跨租户复用。

### 被否决的替代方案

| 方案 | 否决理由 |
|------|---------|
| 全局常驻 worker 池 | 跨租户状态残留，安全不可接受 |
| per-tenant 常驻 worker | 同租户内跨用户残留；且 1,000 租户 × 常驻 worker = 资源浪费（多数租户空闲） |
| 不优化冷启动，接受 3-8 s | Agent shell 是交互式操作，3-8 s 不可接受 |

### 代价
warm pool 的资源占用（每租户 1-2 个空容器 × 活跃租户数）。缓解：只为**近 5 分钟有活动**的租户维持 warm pool，空闲租户不占用。

---

## ADR-011 · `dagster-control-plane` 降级为 orchestrator 的 sidecar，不再是独立代码库

**状态**：proposed

### 背景
`dagster-control-plane/` 是 689 行的独立 Python 代码库，只做一件事：轮询数据面的 `/internal/code-locations`，聚合后原子写控制面 `workspace.yaml` 并触发 reload。

### 决策
它的 `workspace_sync.py` 成为 orchestrator 部署单元里的 sidecar 组件（与 webserver/daemon 同 Pod，共享 `DAGSTER_HOME` 卷），不再是独立仓库/独立发布单元。每 Cell 一个。

### 理由
1. **689 行 + 无 HTTP 服务 + 生命周期完全绑定控制面** → 它是部署组件而非服务。独立代码库带来的是 CI/发布/依赖管理的额外开销，没有对应收益。
2. **P6 后其发现机制改变**：code location 变成独立 Pod（ADR-009），sync 从"轮询 runtime 实例"改为"读 K8s Endpoints"。这使它更贴近 K8s 控制器而非独立服务。
3. **单一写者 PULL 模式是对的**（现有设计已论证：N 副本并发写同一文件需要分布式锁 + 共享卷 + 写冲突处理，而这只是派生状态），此决策不改变该模式，只改变代码归属。

### 被否决的替代方案
| 方案 | 否决理由 |
|------|---------|
| 保持独立代码库 | 无收益的运维开销 |
| 让 runtime 各自直写控制面 `workspace.yaml` | 现有设计已论证否决：写竞争 + 每轮全量 reload 导致控制面永久重复 import |
| 用 Dagster 官方的动态 workspace 加载 | 官方无此机制；`grpc_server` 渲染 + 外部同步是唯一可行路径 |

### 代价
无实质代价（纯代码归属调整）。

---

## ADR-012 · 现有正则命令策略降级为「用户体验层」，不再作为安全边界

**状态**：proposed

### 背景
`conf/shell-command-policy.yaml` 用正则黑名单拦截危险命令，配合 `command_approval.py` 的 L1-L6 审批。策略文件自述无法防 base64/eval/变量展开。

### 决策
两者**全部保留**，但重新定位：
- 正则策略 → **用户体验层**：拦截明显误操作（`rm -rf /`），给出友好提示。**不再是安全边界。**
- L1-L6 审批 → **对话语义层**：留在 agent-service，回答"你确定要删这个表吗"。与隔离级别正交。
- 真正的安全边界 → 容器/内核层（ADR-004）。

### 理由
现有机制不是失败，而是**放错了位置**。它们是很好的 UX 与合规机制（审批留痕、误操作拦截），但被误当成隔离边界。把安全边界下移到内核后，它们回到本来的位置，且**双层防御职责不重叠**：审批管"该不该做"，沙箱管"做了也伤不到别人"。

### 被否决的替代方案
| 方案 | 否决理由 |
|------|---------|
| 删除正则策略（既然不安全） | 它对误操作拦截有真实价值；且审批分级需要风险评分输入 |
| 把审批也下移到 runtime/sandbox | 审批需要与用户对话交互（SSE 推送 + 等待确认），是 agent-service 的会话语义 |

### 代价
无。纯定位调整 + 文档更新（避免团队继续误以为它是安全边界）。

---

## ADR-013 · 高频 append 表用 PG 原生分区起步，不引入 ClickHouse

**状态**：proposed

### 背景
`timeline_events` 单 Cell 2×10^6/天（全平台 10^7/天），一年 3×10^9 行。放在 OLTP 库不合适。

### 决策
用 **PG 按天 RANGE 分区表**（`pg_partman` 管理）起步，热保留 30 天，detach 后导出 Parquet 到对象存储。若后续 timeline 查询成为瓶颈再引入 ClickHouse。

### 理由
1. **访问模式简单**：按 `session_id` 顺序读取，无聚合分析需求。分区表 + `(session_id, created_at DESC)` 索引足够。
2. **零新组件**：不增加运维面。ClickHouse 需要独立集群、独立监控、独立备份。
3. **Cell 化已经把量除以 5**：单 Cell 2×10^6/天 × 30 天 = 6×10^7 行，PG 分区表可轻松承受。
4. **可无痛演进**：schema 独立（`agent` schema），未来迁库只改连接串。

### 被否决的替代方案
| 方案 | 否决理由 |
|------|---------|
| 直接上 ClickHouse | 过早引入组件；当前无分析型查询需求 |
| 不分区，靠定期 DELETE | 大表 DELETE 产生大量 dead tuple + autovacuum 压力；分区 detach 是 O(1) |
| 全部写对象存储（无 PG） | 会话回放需要低延迟点查 |

### 代价
未来若出现 timeline 分析需求（如"统计所有用户的工具调用分布"），需要迁移。届时 Parquet 冷层可直接被 DuckDB/Trino 查询，迁移成本可控。

---

## 决策依赖关系

```
ADR-001 (按状态性质切分)
    ├── ADR-002 (租约) ──── ADR-003 (实例私有目录 · 介质可插拔)
    │        │                   │
    │        └───────────────────┴──▶ 消除分布式锁与缓存一致性问题
    │
    ├── ADR-004 (沙箱) ──── ADR-005 (Context 分裂)
    │        │         └─── ADR-009 (code location 独立 Pod)
    │        │         └─── ADR-010 (不常驻池化)
    │        └───────────── ADR-012 (正则降级为 UX 层)
    │
    ├── ADR-006 (Cell 化) ── ADR-007 (identity 拆出)
    │        │           └── ADR-013 (PG 分区起步)
    │        │
    ├── ADR-008 (agent 独立)
    └── ADR-011 (control-plane 降级为 sidecar)
```

**关键依赖**：ADR-002 是 ADR-003 的前提（没有租约就不能用实例私有目录；反向也成立——共享可写介质会让租约的单写者保证失效，故 `mount_kind: shared` 启动期拒绝）；ADR-004 是 ADR-005/009 的前提；ADR-006 是 ADR-007 的动因。

**可独立决策**：ADR-008、ADR-011、ADR-012、ADR-013。

---

## P0 决策结论（已冻结 2026-08-26；Q7 补充 2026-08-27）

七个开放问题的决策结果，及其对设计的影响：

| # | 问题 | **决策** | 影响 |
|---|------|---------|------|
| **Q1** | 是否接受暂时禁用 SQLMesh Python model / 自定义 macro？ | **不接受**——保留该能力 | 不能用"校验期拒绝"规避 A3 攻击面，必须真做隔离且尽早。用户要求 warm sandbox 提前到 P3；因容器形态依赖租约（P4）与容器基础设施（P6）而**物理上不可行**，故改为在 P3 交付 **L3-proc**（进程级隔离，无外部依赖），并冻结其通信协议作为 P6 容器形态的前身。见 ADR-005 修订、§04.2.3 |
| **Q2** | 集群是否支持 gVisor / Kata？ | **暂不支持**，记录为待增强点 | L3 最高档位为 **L3-base**（标准 runc + 全套 securityContext + 网络隔离）。T8（容器逃逸）未覆盖，缓解为用户代码专属节点池 + 节点内核补丁基线。`L3_strong` profile 以 `enabled: false` 保留在配置中，启用仅需配置改动。见 §04.8.1 |
| **Q3** | `lease.max_per_instance = 16` 的依据？ | **设为可配置**，P0 实测校准 | 从"固定容量"改为"内存预算驱动的可配置值 + 启动期自校验 + 运行期自适应降级"。见 ADR-002 修订、§05.7 |
| **Q4** | 是否需要跨 Cell 协作？ | **不需要** | ADR-006 的核心假设成立，Cell 化模型无需调整。工作区共享限定在**同 Cell 内**（同租户天然同 Cell）。记为待增强项（§04.8.3），变更需 ≥6 周重设计 |
| **Q5** | 起步阶段凭证注入方案（env 注入）是否可接受？ | **接受** | 保持 env 注入 + 只读账号 + 单 gateway + 任务级销毁。派生两条**强制约束**：① 可写仓库账号只允许 L2 档位（写操作必须从 L3 拆出）；② Secret 随任务销毁且不入日志/镜像层。warehouse-proxy 转为待增强项（§04.8.2） |
| **Q6** | 非优雅崩溃最多丢 10 分钟改动，可接受？ | **接受** | ADR-003 的本地盘方案成立，`wip_snapshot_interval_seconds = 600` 作为正式契约（可配置）。需在产品层明确告知用户此保证等级。见 ADR-003 修订、§05.5.3 |
| **Q7** | P4 是否需要流量灰度（10% 分流 + 共享 PV 双跑）？ | **不需要**——改为单代码路径 + 介质可配置 + 分阶段上线 | 灰度会迫使网关与 runtime 各维护两套并行路径，**风险大于其所降低的**。改为：物化逻辑唯一路径 + `materialize.mount_kind`（`ephemeral` / `instance_local` / `shared`=启动期拒绝）；P4 拆为 4a（租约+WIP，共享 PV 上验证）与 4b（介质切换）两次上线；灰度原本验证的租约竞态改为并发 acquire 压测；按环境分批全量替代流量比例灰度。P4 不再是难回滚阶段。见 ADR-003 Q7 修订、§03.3.4、§05.7 |

**因决策产生的文档修订**：ADR-002（容量可配置）、ADR-003（WIP 契约 + Q7 介质可插拔）、ADR-004（L3 分档）、ADR-005（L3-proc 前置到 P3）、ADR-006（跨 Cell 不支持已确认）；`03-multi-tenancy-and-scaling.md` §3.4 改为「物化唯一、介质可插拔」；`04-workspace-sandbox-isolation.md` 新增 §2.3 与 §8；`05-data-and-state-model.md` §5.3/§7 增加 `mount_kind`；`06-migration-roadmap.md` 的 P3/P4（拆 4a/4b）/P6 任务与回滚策略调整。

---

## 待 P0 明确的开放问题（已全部关闭）

| # | 问题 | 影响 | 决策方 | 状态 |
|---|------|------|--------|------|
| **Q1** | M1 是否接受暂时禁用 SQLMesh Python model / 自定义 macro？ | 若不接受，需在 P3 就上真隔离 | 产品 | ✅ **不接受** → L3-proc 前置到 P3 |
| **Q2** | 集群是否支持 gVisor 或 Kata？ | 若不支持，L3 上限为标准 runc 加固档 | 基础架构 | ✅ **暂不支持** → 记为待增强（§04.8.1） |
| **Q3** | `lease.max_per_instance = 16` 的依据？ | 直接决定 runtime 实例数与成本 | 工程实测 | ✅ **设为可配置** + 启动期自校验 |
| **Q4** | 是否需要支持跨 Cell 协作？ | 若需要，Cell 化模型要大改 | 产品 | ✅ **不需要** |
| **Q5** | 起步阶段凭证注入方案是否可接受？ | 影响 P6 工期 2 周 | 安全 | ✅ **接受**（附两条强制约束） |
| **Q6** | 非优雅崩溃最多丢 10 分钟改动是否可接受？ | 若不可接受需缩短快照周期或保留共享 PV | 产品 | ✅ **接受** |
| **Q7** | P4 是否需要流量灰度？ | 决定 P4 是否需维护两套并行路径 | 工程 | ✅ **不需要** → 介质可配置 + 4a/4b 分阶段上线 |
