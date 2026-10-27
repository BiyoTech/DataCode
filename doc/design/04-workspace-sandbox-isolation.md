# 04 · 工作区沙箱隔离

> 现状问题（详见 §01.4）：平台**允许用户在平台服务进程内执行任意代码**，防线只有正则黑名单。`conf/shell-command-policy.yaml` 自述 *"the sandbox isolation is the primary guard"* —— 而那个 guard 目前不存在。

---

## 1. 威胁模型

在 1,000 租户共享集群的前提下，明确要防什么。

### 1.1 攻击面

用户可以让代码在平台内执行的三条路径：

| # | 入口 | 现状执行位置 | 能力 |
|---|------|------------|------|
| **A1** | Agent `shell` 工具 | `agent-service/src/tools/shell.py:94` `create_subprocess_exec` | **任意 shell 命令**，仅经正则过滤 + L1-L6 审批 |
| **A2** | Dagster `asset-dag/definitions.py` | dagster-service 的 code location 子进程 `import` 用户模块 | **任意 Python 代码**，无任何过滤 |
| **A3** | SQLMesh 模型中的 Python macro / Python model | SQLMesh Context 在 sqlmesh-service 进程内 `exec` | **任意 Python 代码**，无过滤 |

A2 与 A3 是**完全无防护**的——它们是产品的正常功能（用户当然要写 Dagster asset 和 SQLMesh Python model），因此不能用"禁止"解决，只能用**隔离**解决。

### 1.2 威胁清单

| ID | 威胁 | 当前是否可行 | 影响 |
|----|------|------------|------|
| T1 | 读取其他租户的工作区文件 | ⚠️ 部分（同 PV 挂载，路径校验拦住 API 但 shell 内 `cat /workspaces/other-tenant/...` 可行） | **跨租户数据泄露** |
| T2 | 读取平台服务的环境变量（DB 密码、LLM key、加密密钥） | ✅ 可行（`os.environ` / `env` 命令，用户代码与服务同进程空间） | **平台完全失陷** |
| T3 | 直连集群内 Postgres / Redis | ✅ 可行（无 NetworkPolicy，且从 T2 拿到凭证） | **所有租户数据泄露** |
| T4 | 直连其他 runtime 实例的内部 API | ✅ 可行 | 跨租户操作 |
| T5 | 资源耗尽（死循环 / OOM / 磁盘写满） | ✅ 可行（无 cgroup 限制） | **同 Pod 内其他用户会话被杀（噪声邻居）** |
| T6 | 访问云元数据服务（169.254.169.254 → IAM 凭证） | ✅ 可行 | **云账号失陷** |
| T7 | 通过 SSRF 访问内网服务 | ✅ 可行 | 内网横向移动 |
| T8 | 容器逃逸 | 取决于运行时；标准容器 + root 有已知逃逸路径 | **节点失陷** |
| T9 | 加密货币挖矿 / 出网滥用 | ✅ 可行 | 成本与合规 |
| T10 | 篡改平台代码/配置（若挂载了代码目录） | 需检查挂载 | 持久化后门 |

**T2 是最关键的**：只要用户代码与平台服务共享进程空间/容器，`os.environ` 就能拿到 `DB_PASSWORD`、`OPENAI_API_KEY`、`SQLMESH_SERVICE_TOKEN_ENCRYPTION_SECRET`。**所有其他防护在这之后都无意义。**

---

## 2. 隔离原则

### 2.1 三条原则

| # | 原则 | 落实 |
|---|------|------|
| **P1** | **用户代码永不与平台服务共享进程空间。**（= §02 的 R3） | 所有执行移到 task-runner 容器 |
| **P2** | **执行容器不持有任何平台凭证。** | 凭证经短时效、最小权限的 scoped token 注入；DB/Redis/LLM 凭证绝不进入执行容器 |
| **P3** | **执行容器默认拒绝一切出网。** | NetworkPolicy default-deny + 显式白名单（仅客户仓库 + Git server，经出口代理） |

### 2.2 隔离分级

不是所有执行都需要最强隔离。按「代码来源」分级：

| 级别 | 代码来源 | 隔离机制 | 适用 |
|------|---------|---------|------|
| **L0** | 平台代码，无用户输入 | 无（平台服务进程内） | control-service 的 DB 操作、identity 的鉴权 |
| **L1** | 用户**数据**（YAML/JSON 解析、路径参数） | 平台进程内 + 严格校验 + `yaml.safe_load` | Ontology YAML 加载、文件路径校验 |
| **L2** | 用户 **SQL**（无宿主代码执行能力） | **独立容器**（标准 runc）+ 无凭证 + 网络白名单 + cgroup | SQLMesh plan/apply（纯 SQL 模型）、query |
| **L3** | 用户**任意代码**（Python / shell） | 见 §2.3 的三个实现档位 | Agent shell、Dagster asset、SQLMesh Python model/macro |

**判定规则**：如果执行内容可能包含**图灵完备的宿主语言代码**，就是 L3。SQLMesh 的 Python model 与 macro 使 plan/apply 在有 Python 模型时也升级为 L3 —— 因此实现上需要在 plan 前扫描工作副本是否含 Python 模型/macro，有则用 L3 profile（安全默认：**不确定时用 L3**）。

### 2.3 L3 的三个实现档位（演进路径）

**决策背景**（P0 已定）：
- **Q1 决策：不接受禁用 SQLMesh Python model / 自定义 macro。** 因此不能用"校验期拒绝"来规避 A3 攻击面，必须真做隔离，且要尽早。
- **Q2 决策：暂不引入 gVisor / Kata。** 记录为待增强点（§8）。

两个决策的交集决定了 L3 必须**分档落地**：先用不依赖容器运行时的手段消除最致命的威胁，再逐档加固。

| 档位 | 机制 | 消除的威胁 | 残余威胁 | 落地阶段 | 依赖 |
|------|------|-----------|---------|---------|------|
| **L3-proc** | 独立 OS 进程 + 独立非特权 UID + **清空环境变量** + `setrlimit`（CPU/AS/NPROC/FSIZE）+ 独立 cwd + 禁用 git hooks | **T2（平台凭证）** ✅ 最致命的一条<br>T5 部分（rlimit）<br>T10（文件权限） | T1 部分（同 Pod 文件系统可见）<br>T3 / T6 / T7（网络）<br>T8（无内核隔离） | **P3**（服务合并期） | 无（纯进程级，不依赖 K8s/租约/容器） |
| **L3-base** | 独立容器（**标准 runc**）+ nonroot + drop ALL caps + seccomp RuntimeDefault + readOnlyRootFs + cgroup + **只挂载 1 个工作副本** + NetworkPolicy default-deny + egress-proxy | **T1 / T3 / T5 / T6 / T7** ✅<br>（T2 继续保持） | **T8（容器逃逸）**<br>—— 标准 runc 与宿主共享内核 | **P6**（沙箱平面） | 租约（P4）+ 容器基础设施 |
| **L3-strong** | L3-base + `runtimeClass: gvisor`（或 Kata） | **T8** ✅ | — | **待增强**（§8） | 集群支持 gVisor/Kata |

**为什么 L3-proc 能在 P3 落地而容器形态不能**：

```
per-lease warm sandbox（容器形态）的依赖链：
  "per-lease"          → 需要租约机制                      → P4
  scoped task token    → 需要 control-service 的任务表      → P5/P6
  NetworkPolicy        → 需要 K8s 部署形态与节点池划分       → P6
  egress-proxy         → 需要独立组件                       → P6
  ⇒ 最早只能在 P6

L3-proc 的依赖链：
  独立进程 + UID + 清空 env + rlimit → 仅需 Python 标准库（os.fork / setuid / setrlimit）
  ⇒ P3 即可，与服务合并同期完成
```

**L3-proc 的核心价值**：`os.environ` 在子进程里是**空的**（只保留白名单：`PATH` / `HOME` / `LANG` / 本工作副本的仓库连接参数）。这一条就消除了 T2 —— 而 §1.2 已论证 *"T2 是最关键的，所有其他防护在这之后都无意义"*。因此 L3-proc 虽不完整，但它拿掉的是**使其余防护失效的那块基石**。

**安全等级的诚实表述**：

| 阶段 | 可对外声明的隔离等级 | 不可声明的 |
|------|-------------------|-----------|
| P3 后（L3-proc） | 用户代码无法读取平台凭证；资源受 rlimit 约束 | 不可声明"跨租户隔离"（同 Pod 文件系统仍可见）、不可声明"网络隔离" |
| P6 后（L3-base） | **跨租户隔离 + 网络隔离 + 资源隔离** 达标；可支撑多租户共享集群 | 不可声明"抗容器逃逸"（共享内核） |
| 待增强后（L3-strong） | 抗容器逃逸 | — |

⚠️ **对合规/客户承诺的约束**：在 L3-base 落地前（即 P6 前），**不应向外承诺"租户间强隔离"**。若有此类合同义务，需把 P6 提前或为该客户提供独占 Cell（ADR-006 已支持 `dedicated_cell`）。

---

## 3. 执行平面外移

### 3.1 现状 vs 2.0

```
【现状】用户代码在平台进程内
┌──────────────────────────────────────────┐
│  agent-service Pod                       │
│  ┌────────────────────────────────────┐  │
│  │ FastAPI 进程                        │  │
│  │  env: OPENAI_API_KEY, DB_PASSWORD  │  │  ← T2: 用户可读
│  │  └─ subprocess: 用户的 shell 命令   │  │  ← 同 UID / 同 netns / 同 fs
│  └────────────────────────────────────┘  │
│  mount: /workspaces (共享 NFS，全租户)    │  ← T1: 可读其他租户
└──────────────────────────────────────────┘
        网络：无限制 → PG / Redis / 元数据服务   ← T3/T6

【2.0】用户代码在独立沙箱
┌────────────────────────────┐      ┌──────────────────────────────────┐
│ workspace-runtime Pod      │      │ task-runner Pod（每任务一个）      │
│  平台代码 · 持凭证           │      │  runtimeClass: gvisor            │
│  mount: 本实例租约的工作副本  │─────▶│  user: 65534 (nonroot)           │
│  （仅本 runtime 的 ≤16 个）  │ 提交 │  readOnlyRootFilesystem: true    │
│                            │ 任务 │  capabilities: drop ALL          │
│  ┌──────────────────────┐  │      │  seccomp: RuntimeDefault         │
│  │ TaskSubmitter        │  │      │  env: 仅 scoped task token        │  ← P2
│  │ 挂载: 单个工作副本      │  │      │  mount: 仅 1 个工作副本（RW）      │  ← T1 解决
│  └──────────────────────┘  │      │  cgroup: cpu 2, mem 4Gi, pids 512│  ← T5 解决
└────────────────────────────┘      │  NetworkPolicy: default-deny     │  ← T3/T6/T7 解决
                                    │    + egress: 出口代理 only         │
                                    └──────────────────────────────────┘
```

### 3.2 task-runner 规格

```yaml
# task-runner Pod 模板（L3-base profile，P6 交付）
#
# NOTE (Q2 decision): gVisor/Kata is NOT adopted for now — recorded as a
# deferred hardening item (see §8). runtimeClassName is therefore left unset,
# so the standard runtime (runc) is used and the sandbox SHARES THE HOST
# KERNEL. Everything else below still applies, which covers T1/T3/T5/T6/T7;
# only T8 (container escape) stays open. Turning gVisor on later is a one-line
# change here plus a node pool that supports it.
apiVersion: v1
kind: Pod
metadata:
  labels:
    app: task-runner
    datacode/tenant-id: "{tenant_id}"        # 用于配额与审计
    datacode/task-kind: "agent.shell"
    datacode/isolation: "L3-base"
spec:
  # runtimeClassName: gvisor                  # DEFERRED — see §8
  automountServiceAccountToken: false         # 不注入 K8s SA token
  serviceAccountName: task-runner-noperm      # 零 RBAC 权限的 SA
  enableServiceLinks: false                   # 不注入集群服务的 env 变量 ⚠️ 关键
  hostNetwork: false
  hostPID: false
  hostIPC: false
  dnsPolicy: None                             # 不用集群 DNS
  dnsConfig:
    nameservers: ["10.x.x.x"]                 # 受控 DNS（只解析白名单域名）

  securityContext:
    runAsNonRoot: true
    runAsUser: 65534
    runAsGroup: 65534
    fsGroup: 65534
    seccompProfile: { type: RuntimeDefault }

  # ── 节点隔离：用户负载专属节点池，与平台服务物理分离 ──────────
  nodeSelector:
    datacode.io/workload: user-code
  tolerations:
    - key: datacode.io/user-code
      operator: Exists
      effect: NoSchedule

  containers:
    - name: runner
      image: <registry>/datacode/task-runner:<version>
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: { drop: ["ALL"] }
      resources:
        requests: { cpu: "500m", memory: "1Gi", ephemeral-storage: "2Gi" }
        limits:   { cpu: "2",    memory: "4Gi", ephemeral-storage: "8Gi" }
      env:
        # 唯一注入的凭证：单任务、单工作副本、短时效的 scoped token
        - name: DATACODE_TASK_TOKEN
          valueFrom: { secretKeyRef: { name: "task-{task_id}", key: token } }
        - name: DATACODE_RUNTIME_CALLBACK_URL
          value: "http://{runtime_pod_ip}:8010/rt/v1/tasks/{task_id}"
        # 明确不注入：DB_PASSWORD / REDIS_PASSWORD / OPENAI_API_KEY /
        #             TOKEN_ENCRYPTION_SECRET / 任何 *_INTERNAL_TOKEN
      volumeMounts:
        - name: workcopy
          mountPath: /workspace                # 唯一可写的业务目录
        - name: tmp
          mountPath: /tmp
      # 用户代码不需要知道自己在 K8s 里
  volumes:
    # 只挂载「这一个」工作副本，不是整个 base_path ⚠️ T1 的根本解决
    - name: workcopy
      csi:
        driver: workcopy.datacode.io            # 自研 ephemeral CSI，或
        # 起步方案：hostPath subPath（runtime 与 runner 同节点，Pod 反亲和保证）
    - name: tmp
      emptyDir: { sizeLimit: 2Gi, medium: Memory }

  activeDeadlineSeconds: 1800                   # 硬超时，防死循环占用
  restartPolicy: Never
```

### 3.3 工作副本如何交给 task-runner

三个方案，按落地成本递增：

| 方案 | 机制 | 优点 | 缺点 | 采用 |
|------|------|------|------|------|
| **S1 · 同节点 hostPath subPath** | task-runner Pod 与持租约的 runtime 调度到同节点（`podAffinity`），挂载 `hostPath: {node_workdir}/{lease_key}` | 零拷贝、简单 | hostPath 需要节点级权限；调度约束强 | **起步** |
| **S2 · 自研 ephemeral CSI** | CSI driver 按 `lease_key` 从 runtime 借出目录（bind mount） | 无 hostPath、可跨节点（若配 NFS 局部） | 要写 CSI driver | M2 |
| **S3 · 文件视图 API** | runner 不挂载文件，所有 IO 经 runtime 的 gRPC 文件 API | 最强隔离（runner 完全无文件系统访问） | 用户代码需要真实文件（`import` / `git`），不可行 | ❌ 否决 |

**采用 S1 起步**，并明确其安全边界：`hostPath` 只挂载 `{node_workdir}/{lease_key}` 这一个子目录（用 `subPath` 或 CSI 的 `subPathExpr`），且节点上 `{node_workdir}` 的权限为 runtime 的 fsGroup。

⚠️ **S1 与 `materialize.mount_kind` 的一致性约束**（ADR-003 Q7）：S1 的 hostPath 形态只在 `mount_kind: ephemeral` 或 `hostPath` 下成立。生产默认的 `instance_local`（per-replica RWO PVC）下**没有节点级路径可挂**，task-runner 必须与持租约的 runtime **同节点**并挂载同一 PVC（RWO 允许同节点多 Pod 挂载），或走 S2。**这不引入新的存储模式** —— 挂载策略跟随已声明的介质，仍是一条物化路径。

⚠️ **Q2 决策下的一个诚实说明**：原设计依赖"gVisor 保证即使 runner 内提权也无法访问 hostPath 之外"。**暂不使用内核沙箱后，这层保证不存在** —— hostPath 挂载在标准 runc 下，若发生容器逃逸则节点上其他工作副本可达。因此 S1 的实际安全性依赖于：① `{node_workdir}` 目录权限（非 root 用户只能访问自己的子目录）；② 用户代码专属节点池（逃逸影响限于用户负载节点）；③ 节点内核补丁基线。**若启用 L3-strong，S1 才回到设计预期的强度**（§8.1）。若此风险不可接受，应优先做 S2（自研 ephemeral CSI，不依赖 hostPath）。

**关键约束：一个 task-runner 只挂载一个工作副本。** 这是 T1（跨租户读取）的根本解决——不是靠路径校验，而是靠**挂载点不存在**。

### 3.4 出网控制

用户代码的合法出网需求：
1. **客户数据仓库**（Snowflake / BigQuery / Databricks / 客户 PG）——SQLMesh 必须连
2. **Git server**——`git push/fetch`
3. **PyPI**（可选）——用户可能需要装依赖
4. **LLM**（❌ 不需要）——LLM 调用在 agent-service 侧，runner 不该有 LLM key

设计：**default-deny + 显式出口代理**

```yaml
# NetworkPolicy: task-runner 默认拒绝一切
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: task-runner-default-deny
spec:
  podSelector: { matchLabels: { app: task-runner } }
  policyTypes: [Ingress, Egress]
  # Ingress: 完全拒绝（runner 不接受入站连接）
  ingress: []
  egress:
    # 1. 受控 DNS（只解析白名单域名）
    - to: [{ podSelector: { matchLabels: { app: egress-dns } } }]
      ports: [{ protocol: UDP, port: 53 }]
    # 2. 出口代理（唯一出网通道）
    - to: [{ podSelector: { matchLabels: { app: egress-proxy } } }]
      ports: [{ protocol: TCP, port: 3128 }]
    # 3. 回调 runtime（上报进度与结果）
    - to: [{ podSelector: { matchLabels: { app: workspace-runtime } } }]
      ports: [{ protocol: TCP, port: 8010 }]
  # 明确未列出 ⇒ 拒绝：Postgres / Redis / identity / control /
  #   169.254.169.254（云元数据）/ 10.0.0.0/8 内网 / 其他 runner
```

**egress-proxy** 职责：
- 按 `datacode/tenant-id` label 识别来源，只允许该租户**已配置的**仓库地址 + Git server
- 拒绝所有内网段（`10.0.0.0/8` `172.16.0.0/12` `192.168.0.0/16` `169.254.0.0/16` `127.0.0.0/8`）与用户规则里提到的 `9.* 11.* 21.* 30.*`
- 全量审计日志（谁、何时、连了哪里、传了多少字节）
- 出网流量配额（防挖矿/数据外传）

**PyPI 依赖**：不给 runner 直接访问 PyPI。改为 **预置镜像 + 受管依赖清单**：用户在工作区声明 `requirements.txt`，由 control-service 审核（白名单包 + 版本）后构建**租户专属 runner 镜像**（离线构建，不在 runner 内 pip install）。这同时解决供应链风险。

### 3.5 凭证注入（P2 落实）

用户代码需要**客户仓库凭证**（否则 SQLMesh 连不上）。这是最难的部分：不能把凭证给 runner，但 SQLMesh 要用它。

**方案：连接代理 + scoped token**

```
task-runner（无仓库凭证）
    │  连接串指向本地代理：postgresql://localhost:15432/db
    ▼
sidecar: warehouse-proxy（同 Pod，但独立容器）
    │  ① 校验 DATACODE_TASK_TOKEN（单任务、单工作副本、TTL=任务时长）
    │  ② 向 runtime 换取本次任务的仓库连接（runtime 持凭证，runner 与 sidecar 都不持久持有）
    │  ③ 透明代理 SQL 流量，凭证只在 sidecar 内存中
    ▼
客户数据仓库
```

**分级采用**：

| 阶段 | 方案 | 安全性 |
|------|------|-------|
| **起步（P6，✅ Q5 已接受）** | 凭证注入 runner 的 env，但 **① 只注入本工作副本对应的那一个 gateway 凭证；② 只读/最小权限的仓库账号（写权限的 apply 走 L2 而非 L3）；③ 任务结束即销毁 Secret** | 中：用户可读到自己工作区的仓库凭证（**这本来就是他有权访问的**），但读不到其他租户的、也读不到平台自身的 |
| **强化（待增强，§8）** | warehouse-proxy sidecar | 高：runner 完全不接触凭证 |

**关键判断**：让用户读到「他自己工作区配置的仓库凭证」是**可接受的**（他本来就配置了它）。绝不可接受的是读到「平台的 DB_PASSWORD / 加密密钥 / 其他租户的凭证」。因此起步方案的分界线画在这里是合理的，不必为完美而阻塞落地。

**Q5 决策已接受此方案**，随之产生两条强制约束（否则"只能读到自己的凭证"这句话不成立）：

| 约束 | 理由 |
|------|------|
| **写权限的 apply 必须走 L2，不得走 L3** | L3 是"用户任意代码"档位。若把可写仓库账号注入 L3 容器，用户代码可绕过 SQLMesh 直接对仓库做任意 DDL/DML。含 Python model 的 apply 因此需要拆分：**元数据解析/渲染在 L3（只读凭证），最终 SQL 执行在 L2（可写凭证）** |
| **Secret 必须随任务销毁，且不得写入镜像层或日志** | 任务级 Secret + `emptyDir: medium: Memory`；凭证经 `safe_url()` 脱敏后才可进日志（继承现有 M1 实现） |

### 3.6 各执行入口的落地映射

| 入口 | 现状 | P3 后（L3-proc） | P6 后（L3-base） |
|------|------|-----------------|-----------------|
| **Agent shell**（A1） | `agent-service` 进程内 subprocess | agent-service → runtime `/rt/v1/tools/shell` → **L3-proc 子进程**（独立 UID + env 清空 + rlimit）；审批 L1-L6 仍在 agent 侧 | → **task-runner L3-base 容器** |
| **Agent 文件读写** | agent-service 进程内 `Path.read_text` | agent-service → runtime `/rt/v1/tools/{read,write,patch,list,search}`；**runtime 内直接执行**（L1：纯文件 IO，无代码执行，路径经 `parse_workspace_path` 校验） | 不变（L1） |
| **Dagster asset run**（A2） | code location 子进程 in dagster-service | 仍为子进程，但应用 L3-proc 加固（独立 UID + env 清空） | Dagster RunLauncher → **task-runner L3-base**（Pod per run，官方 `K8sRunLauncher` 天然如此） |
| **Dagster code location（warm）** | dagster-service 子进程 | runtime 的 L3-proc 子进程 | **独立 Pod**（§03.6），L3-base |
| **SQLMesh plan/apply（纯 SQL）** | sqlmesh-service 进程内 Context | **L3-proc 子进程**（`SqlMeshExecutor`） | **task-runner L2**（可写凭证仅在此档位，见 §3.5） |
| **SQLMesh plan/apply（含 Python model/macro）** | 同上 | **L3-proc 子进程**（Q1 决策：不禁用该能力，故必须真隔离） | **元数据/渲染在 L3-base（只读凭证）+ 最终 SQL 执行在 L2（可写凭证）** |
| **SQLMesh 只读**（models 列表 / lineage） | 同上 | **runtime 内**（读 `MetadataContext`，纯数据，毫秒级） | 不变 |
| **SQLMesh render（含 Python macro）** | 同上 | **L3-proc 子进程**（需 exec 用户 macro） | **task-runner L3-base** |
| **Ontology YAML 加载** | ontology-service 进程内 | **runtime 内**（L1，`yaml.safe_load`） | 不变（L1） |
| **Git 操作** | sqlmesh-service 进程内 git CLI | **runtime 内**（L2 语义：git 是可信二进制；但**必须** `core.hooksPath=/dev/null` 禁用 hooks） | 不变 |

⚠️ **`git hooks` 是一个当前被忽略的攻击面**：用户 push 的仓库里的 `.git/hooks/post-checkout` 会在 runtime 内执行。**必须在所有 git 调用上强制 `-c core.hooksPath=/dev/null`**，且 worktree 物化时不复制 hooks。

### 3.7 SQLMesh Context 的隔离难题

**这是 2.0 最棘手的技术点。**

矛盾：
- SQLMesh Context 是**内存态**，加载成本高（秒～十秒级），必须缓存在 runtime 里才有可用的交互延迟
- 但 SQLMesh 的 Python model / macro 在 `Context()` 构造时就会 `exec` 用户代码 → 违反 P1

**解法：Context 分裂为两级**

```
┌─ workspace-runtime（平台进程，可信） ─────────────────────────┐
│  MetadataContext（只读元数据视图）                            │
│   • 从 task-runner 序列化回传的模型元数据构建                   │
│   • 内容：模型名/列/依赖/血缘/渲染后 SQL（纯数据，无代码）        │
│   • 服务：/models /lineage /schemas /columns（毫秒级，热缓存）  │
└──────────────────────────────────────────────────────────────┘
                    ▲ 序列化元数据（JSON）
                    │
┌─ task-runner（沙箱，不可信） ──────────────────────────────────┐
│  真正的 sqlmesh.Context                                       │
│   • 构造时 exec 用户 Python model / macro（在沙箱内，安全）      │
│   • 执行：plan / apply / render / evaluate / test              │
│   • 输出：结构化元数据 + 结果，回传给 runtime                     │
└──────────────────────────────────────────────────────────────┘
```

**性能问题**：每次 plan 都要冷启动一个 task-runner + 重建 Context = 10-30 秒。用户体验不可接受。

**缓解：per-lease 的常驻执行体**

```
租约建立时，runtime 拉起一个 长驻但受限 的执行体：
  • 一个租约（= 一个工作副本 = 一个用户的一个环境）对应一个执行体
  • 生命周期 = 租约生命周期（idle_ttl 后一起回收）
  • 执行体内常驻真正的 SQLMesh Context（热的）
  • runtime 通过本地 UDS 与它交互（毫秒级，协议在 P0 冻结）
  • 崩溃/OOM 只影响这一个用户的这一个环境
  • 形态随阶段演进（协议不变）：
      P3 → L3-proc  持久子进程（独立 UID、清空 env、rlimit）
      P6 → L3-base  warm sandbox 容器（+ 单工作副本挂载、default-deny 网络）
```

**这实际上是把「用户代码执行」做成了租约的一部分**：

```
一个租约 = { worktree(实例私有目录), MetadataContext(runtime 内存),
             warm sandbox(独立容器: SQLMesh Context + code location) }
```

**容量重算**：

| 项 | 值 |
|----|---|
| 每 sandbox 内存 | 1.5 GB（SQLMesh Context 200MB + Python 运行时 + 余量）|
| 单 Cell 峰值租约 | 400 |
| sandbox 总内存 | 400 × 1.5 GB = **600 GB per Cell** |
| 部署形态 | 独立节点池（`workload: user-code`），可用 Spot + 超卖（多数 sandbox 空闲） |
| 超卖比 | requests 0.5 GB / limits 1.5 GB → 实际预留 200 GB，峰值可 burst |

**这是隔离的真实成本**：约 200-600 GB 内存 per Cell 用于用户代码沙箱。相对于"跨租户数据泄露"的风险，这个成本是必须付的。

**分阶段落地**（依 Q1 决策：**不禁用 Python model/macro**，因此每一阶段都必须真做隔离）：

| 阶段 | Context 位置 | L3 档位 | 隔离强度 | plan 交互延迟 |
|------|------------|--------|---------|-------------|
| **M0**（现状） | runtime 进程内 | — | ❌ 无：用户代码可读 `DB_PASSWORD` | 2-10 s |
| **M1**（P3 后） | **持久 exec 子进程**（同 Pod，独立 UID，env 清空） | **L3-proc** | **中**：T2 消除（凭证不可读）、rlimit 约束资源 | 2-10 s（子进程常驻，Context 热） |
| **M2**（P6 后） | **per-lease warm sandbox 容器** | **L3-base** | **高**：跨租户 + 网络 + 资源隔离达标 | 3-12 s（本地 UDS） |
| **M3**（待增强） | 同 M2 | **L3-strong** | 抗容器逃逸 | 同 M2 |

**M1 的实现形态（P3 交付，关键）**：

不是"每次 plan fork 一个进程"（那样 Context 每次冷建，10-30 s 不可接受），而是**每个工作副本一个常驻 exec 子进程**：

```
workspace-runtime 进程（平台，持凭证）
    │
    │  为每个活跃工作副本 fork 一个 SqlMeshExecutor 子进程：
    │    • os.setgid/setuid → 独立非特权 UID（如 60000 + slot）
    │    • 环境变量清空，仅保留白名单（PATH/HOME/LANG + 本工作副本仓库连接参数）
    │    • setrlimit: RLIMIT_AS(4GB) / RLIMIT_CPU / RLIMIT_NPROC / RLIMIT_FSIZE
    │    • cwd 限定为该工作副本目录
    │    • git hooks 禁用（core.hooksPath=/dev/null）
    │    • 通过 UNIX domain socket 双向通信（长度前缀 + JSON）
    ▼
SqlMeshExecutor 子进程（不可信，常驻）
    • 常驻真正的 sqlmesh.Context（含用户 Python model/macro，已 exec）
    • 执行 plan / apply / render / evaluate / test
    • 回传结构化元数据供 runtime 构建 MetadataContext
    • 崩溃/OOM 只影响这一个工作副本；runtime 重新 fork
```

**这个形态是 M2 的直接前身**：`SqlMeshExecutor` 的**通信协议与元数据序列化契约在 M1 就已冻结**（UDS + JSON），M2 只是把"子进程"换成"容器"，协议不变。因此 M1 不是临时方案，而是 M2 的第一步 —— 这也是为什么它能在 P3 做而不浪费。

**M1 的诚实边界**：子进程与 runtime 同 Pod、同网络命名空间、同文件系统视图。它消除的是 T2（凭证）与 T5 部分（rlimit），**不消除** T1（该 Pod 内其他租约的工作副本目录仍可见）与 T3/T6/T7（网络）。因此 M1 适用于**单租户或可信租户**场景；多租户共享集群必须到 M2。

---

## 4. 冷启动与性能

沙箱化的最大代价是冷启动。

| 操作 | 无沙箱（现状） | L3-proc（P3） | 朴素容器 | L3-base 优化后（P6） |
|------|-------------|--------------|---------|-------------------|
| Agent shell 单条命令 | 50 ms | **80 ms**（fork 常驻子进程，无镜像/网络开销） | 1-3 s（runc Pod 冷启动） | **200 ms**（warm pool） |
| SQLMesh plan | 2-10 s（Context 已热） | **2-10 s**（Context 在常驻子进程内，仍热） | 10-25 s（Pod + Context 冷建） | **3-12 s**（warm sandbox） |
| Dagster run | 5 s（子进程已在） | 5 s | 8-15 s | 8-15 s（可接受，run 本身分钟级） |

> **Q2 决策的一个正面副作用**：不用 gVisor 后，容器冷启动从 3-8 s 降到 1-3 s（无 `runsc` 初始化开销），L3-base 的性能压力显著减轻。代价是 T8（容器逃逸）未覆盖 —— 见 §8。
>
> **L3-proc 几乎无性能代价**：它是常驻子进程，Context 保持热态，只多一次 UDS 往返（< 1 ms）。这使 P3 就上隔离在性能上完全可行。

**优化手段**：

| 手段 | 说明 |
|------|------|
| **L3-proc 常驻子进程（P3）** | §3.7 M1：每工作副本一个常驻 `SqlMeshExecutor`，Context 不重建 |
| **warm sandbox（per-lease，P6）** | §3.7 M2：租约建立时预拉起容器，Context 常驻 |
| **warm pool（per-tenant，P6）** | 为 shell 类短任务维护每租户 1-2 个预热的空 runner，任务到达时 bind-mount 工作副本后立即执行；用完销毁并补充 |
| **镜像预拉取** | DaemonSet 在用户代码节点池预拉 task-runner 镜像；镜像分层最小化 |
| **批量 shell** | Agent 一次提交多条命令而非逐条（现有 `shell.py` 已支持批量，保留） |

**分级优化**：L3 短任务（shell）用 warm pool；L3 长任务（Dagster run）接受冷启动；L2（纯 SQL apply）用标准 runc。

---

## 5. 隔离配置矩阵

```yaml
# conf/isolation-profiles.yaml（新增，平台级）
#
# Isolation profiles for task execution, referenced by task kind.
#
# Security default: when the isolation level of a task cannot be determined,
# the platform falls back to the STRONGEST profile currently enabled
# (see `default_profile` at the bottom).
#
# Decisions frozen at P0:
#   Q1 — SQLMesh Python models/macros stay SUPPORTED, so arbitrary user code
#        must be really isolated rather than rejected at validation time.
#        L3_proc therefore ships in P3, together with the service merge.
#   Q2 — gVisor/Kata is NOT adopted for now. L3_strong is kept here as a
#        DISABLED profile so turning it on later is a config change only.

profiles:
  L1_inprocess:
    # Runs inside workspace-runtime. Data-only inputs, no code execution.
    isolation: in_process
    allowed_task_kinds: ["file.read", "file.write", "file.list", "file.search",
                         "ontology.load", "git.status", "git.diff", "log.read"]
    guards:
      path_validation: strict        # parse_workspace_path, 5-segment layout
      yaml_loader: safe_load
      git_hooks: disabled            # -c core.hooksPath=/dev/null

  L3_proc:
    # SHIPS IN P3. A separate OS process inside the workspace-runtime Pod.
    # Not a container: it needs neither the lease mechanism nor Kubernetes, so
    # it can land during the service merge and remove T2 (the platform
    # credentials read) months before the container plane exists.
    isolation: process
    # A distinct unprivileged uid per executor slot. The working copy is owned
    # by the runtime's fsGroup, so the executor can write its own copy only.
    run_as_user_base: 60000
    # THE point of this profile: the child starts with an EMPTY environment and
    # only the entries below are re-injected. os.environ can no longer surface
    # DB_PASSWORD / LLM keys / TOKEN_ENCRYPTION_SECRET / internal tokens.
    env_policy: clear_except
    env_allowlist: ["PATH", "HOME", "SQLMESH_HOME", "LANG", "LC_ALL", "TZ",
                    "DATACODE_WORKSPACE_PATH", "DATACODE_ENVIRONMENT",
                    "DATACODE_WAREHOUSE_*"]   # this working copy's gateway only
    rlimits:
      # RLIMIT_AS caps address space; RLIMIT_CPU is wall-clock-independent CPU
      # seconds; NPROC stops fork bombs; FSIZE stops filling the node disk.
      address_space_bytes: 4294967296        # 4 GiB
      cpu_seconds: 1800
      max_processes: 256
      max_file_size_bytes: 2147483648        # 2 GiB
      max_open_files: 1024
    cwd: "{workspace_path}"
    guards:
      git_hooks: disabled
    # Persistent, one per working copy: the SQLMesh Context stays hot, so this
    # profile costs ~1 ms (one UDS round trip) rather than a Context rebuild.
    persistent: true
    idle_stop_seconds: 900
    allowed_task_kinds: ["agent.shell", "sqlmesh.plan", "sqlmesh.apply",
                         "sqlmesh.plan_with_python", "sqlmesh.render_macro",
                         "sqlmesh.evaluate", "sqlmesh.test",
                         "dagster.codelocation"]
    # Honest limits: same Pod, same netns, same filesystem view as the runtime.
    # Does NOT cover T1 (other leases' working copies are visible) nor
    # T3/T6/T7 (network). Single-tenant / trusted-tenant deployments only.
    covers_threats: ["T2", "T5-partial", "T10"]

  L2_container:
    # SHIPS IN P6. Standard container. User SQL, no host-language code path.
    isolation: container
    runtime_class: runc
    run_as_user: 65534
    read_only_root_fs: true
    drop_capabilities: ["ALL"]
    seccomp: RuntimeDefault
    resources:
      cpu_limit: "2"
      memory_limit: "4Gi"
      pids_limit: 256
      ephemeral_storage_limit: "8Gi"
    network:
      default: deny
      egress_via_proxy: true
      allowed_destinations: ["warehouse", "git"]
    credentials:
      # The ONLY profile allowed a writable warehouse account: no host-language
      # code runs here, so the account cannot be repurposed for arbitrary DDL.
      warehouse: scoped_readwrite    # single gateway of this working copy
      platform: none                 # never
    timeout_seconds: 3600
    allowed_task_kinds: ["sqlmesh.plan", "sqlmesh.apply", "sqlmesh.query",
                         "git.clone", "git.push"]

  L3_base:
    # SHIPS IN P6. Gated user code: arbitrary Python / shell, in a container.
    # Standard runtime (Q2): hardened but SHARING THE HOST KERNEL.
    isolation: container
    runtime_class: runc              # gvisor deferred — see section 8
    run_as_user: 65534
    read_only_root_fs: true
    drop_capabilities: ["ALL"]
    seccomp: RuntimeDefault
    automount_service_account_token: false
    enable_service_links: false      # no cluster service env vars
    dns_policy: None                 # controlled resolver only
    host_network: false
    host_pid: false
    host_ipc: false
    resources:
      cpu_limit: "2"
      memory_limit: "4Gi"
      pids_limit: 512
      ephemeral_storage_limit: "8Gi"
    network:
      default: deny
      egress_via_proxy: true
      allowed_destinations: ["warehouse", "git"]
      denied_cidrs: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                     "169.254.0.0/16", "127.0.0.0/8",
                     "9.0.0.0/8", "11.0.0.0/8", "21.0.0.0/8", "30.0.0.0/8"]
    credentials:
      # Read-only on purpose: write operations are split out to L2_container
      # (design 04 section 3.5), because user code runs in this profile.
      warehouse: scoped_readonly
      platform: none
    node_pool: user-code             # physically separate from platform nodes
    timeout_seconds: 1800
    allowed_task_kinds: ["agent.shell", "dagster.run", "dagster.codelocation",
                         "sqlmesh.plan_with_python", "sqlmesh.render_macro"]
    covers_threats: ["T1", "T2", "T3", "T5", "T6", "T7", "T10"]
    # T8 (container escape) is NOT covered: see L3_strong.

  L3_deps_install:
    # 安装工作副本在 asset-dag/pyproject.toml 中声明的第三方 Python 包。
    #
    # 单独一档的理由：它需要的组合在其它档位都不存在 —— 可写工作副本 + 只通包索引
    # + 零凭证。L2 持有可写数仓账号（Q5 禁止与任何执行包内代码的场景共存，wheel 的
    # 元数据钩子就是代码）；L3_base 凭证等级正确但白名单是 warehouse/git，而放宽
    # L3_base 等于把包索引发给所有跑用户代码的 task。
    isolation: container
    runtime_class: runc
    run_as_user: 65534
    read_only_root_fs: false           # pip 需要可写的 target 与 cache
    drop_capabilities: ["ALL"]
    seccomp: RuntimeDefault
    resources:
      ephemeral_storage_limit: "12Gi"  # 科学计算栈解包体积
    network:
      default: deny
      egress_via_proxy: true
      # index host 只来自平台配置（deps.index_url），绝不从工作副本推导：
      # 能自选索引的声明文件等于从被评审的代码里放宽沙箱出网。
      allowed_destinations: ["package_index"]
      denied_cidrs: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                     "169.254.0.0/16", "127.0.0.0/8",
                     "9.0.0.0/8", "11.0.0.0/8", "21.0.0.0/8", "30.0.0.0/8"]
    credentials:
      warehouse: none                  # 安装只需要索引与一个目录
      platform: none
    node_pool: user-code
    timeout_seconds: 900               # 首次安装是分钟级下载
    allowed_task_kinds: ["deps.sync"]
    covers_threats: ["T1", "T2", "T3", "T5", "T6", "T7", "T10"]

  L3_strong:
    # DISABLED (Q2). Identical to L3_base plus a sandboxed kernel. Kept here so
    # enabling it is a config change plus a capable node pool, not a redesign.
    enabled: false
    inherits: L3_base
    runtime_class: gvisor            # or kata-containers
    node_pool: user-code-sandboxed
    covers_threats: ["T1", "T2", "T3", "T5", "T6", "T7", "T8", "T10"]

# Fail-safe: an unlisted task kind runs under the strongest ENABLED profile.
# Resolution order, first enabled wins: L3_strong -> L3_base -> L3_proc.
# It is never L1/L2, because those assume the input cannot execute host code.
default_profile: strongest_enabled
```

**profile 与阶段的对应**：

| 阶段 | 启用的 profile | `default_profile` 实际解析为 |
|------|--------------|--------------------------|
| P3 后 | L1、**L3_proc** | `L3_proc` |
| P6 后 | L1、L2、**L3_base**、L3_proc（保留给 dev/单机） | `L3_base` |
| 依赖安装后 | 上述 + **L3_deps_install**（只声明 `deps.sync`，不参与 fail-safe 链） | 不变 |
| 待增强 | 上述 + **L3_strong** | `L3_strong` |

`deps.sync` 同时被 L3_proc 声明（dev/单机唯一能跑 pip 的载具），容器可用时
「最强者胜」自动选中 `L3_deps_install`；产物是与路径无关的 `--target`
site-packages 树（`.datacode/deps/site-packages`），因此同一份树在 runtime 的真实
路径与容器的 `/workspace` 挂载点下都能被 `PYTHONPATH` 前置消费。venv 不可用：
其 `pyvenv.cfg` 与 shebang 钉死绝对路径，跨挂载点即废。

---

## 6. 审计与检测

隔离是预防，审计是检测。

| 事件 | 记录内容 | 存储 | 保留 |
|------|---------|------|------|
| 任务提交 | task_id, kind, isolation_level, tenant, user, workspace, lease_key, 命令摘要（脱敏） | Cell PG `task_audit` | 90 天 |
| 沙箱出网 | tenant, task_id, 目标 host:port, 字节数, 是否放行 | egress-proxy → Loki | 90 天 |
| **策略拦截** | 被 deny 的命令 / 被拒的出网 / 被拒的路径 | Cell PG + 告警 | 1 年 |
| 资源触限 | OOMKill / CPU throttle / pids 触限 / 超时 kill | 指标 + 事件 | 30 天 |
| 凭证使用 | 哪个 task 用了哪个 gateway 凭证 | Cell PG | 1 年 |
| RBAC 决策 | 继承现有 `rbac_audit_log` | Global PG | 1 年 |

**异常检测规则**（触发人工审查）：

| 规则 | 阈值 |
|------|------|
| 单租户出网字节数突增 | > 10× 7 日均值 |
| 出网到非白名单目标的尝试 | > 10 次/小时 |
| 沙箱 OOM 率 | > 20% 该租户任务 |
| L3 任务时长贴近超时上限 | > 50% 任务达 `timeout_seconds` |
| 命令策略 deny 命中 | > 5 次/小时/用户 |
| 同一租户任务并发数异常 | > 配额 80% 持续 10 分钟 |

---

## 7. 与现有机制的关系

| 现有机制 | 2.0 处置 |
|---------|---------|
| `conf/shell-command-policy.yaml` 正则策略 | **保留**，但降级为「用户体验层」：拦截明显误操作（`rm -rf /`），给出友好提示。**不再是安全边界** |
| `command_approval.py` L1-L6 审批 | **保留**在 agent-service，是对话语义（"你确定要删这个表吗"），与隔离级别正交 |
| `parse_workspace_path` 5 段校验 | **保留**，L1 的核心 guard |
| `rbac.OwnsWorkingCopy` 网关校验 | **保留**，纵深防御 |
| `internal_token` fail-closed | **保留**，扩展到所有 `/internal/*` |
| 凭证 AES-256-GCM 加密 | **保留**，并要求密钥按 Cell 分离 |
| Dagster `tag_concurrency_limits` | **保留**，是租户级公平性机制 |

**关键认知转变**：现有的正则策略 + 审批分级不是安全机制的失败，而是**放错了位置**——它们是很好的 UX 与合规机制，但被误当成了隔离边界。2.0 把真正的隔离边界建立在容器/内核层，让这些机制回到它们本来的位置。

---

## 8. 待增强点（deferred hardening）

> 这些项**不在** P0-P8 路线范围内，但已在设计中预留接口，启用时不需要重新设计。每项都记录了触发条件（什么时候必须做）与预估工作量。

### 8.1 内核级沙箱（gVisor / Kata） — Q2 决策延后

| 项 | 内容 |
|----|------|
| **未覆盖的威胁** | **T8 容器逃逸**。L3_base 用标准 runc，与宿主共享内核；已知内核 CVE（如 `CVE-2022-0847` Dirty Pipe 类）可能导致节点失陷，进而横向到同节点其他租户的 task-runner |
| **当前缓解（部分，非等价）** | ① 用户代码专属节点池（`workload: user-code`），与平台服务**物理分离** —— 逃逸后拿到的是用户负载节点，而非平台节点；② `drop ALL` capabilities + `seccomp RuntimeDefault` + nonroot + `readOnlyRootFilesystem`，显著抬高逃逸门槛；③ 节点内核保持及时补丁（纳入运维基线）；④ 节点无平台凭证（IRSA/Workload Identity 最小权限） |
| **残余风险等级** | **中**。逃逸需要一个未修补的内核提权漏洞。影响范围限于用户代码节点池内同节点的其他 task-runner（可能跨租户） |
| **必须启用的触发条件** | ① 有合同/合规要求"租户间内核级隔离"（如金融、医疗客户）；② 出现针对本平台的定向攻击；③ 用户代码节点池承载超过 N 个租户且无法用节点亲和按租户分组；④ 引入"允许用户自带任意镜像"能力 |
| **启用成本** | 集群安装 gVisor（DaemonSet + RuntimeClass）+ 独立节点池 + `isolation-profiles.yaml` 里 `L3_strong.enabled: true`。**代码零改动**。约 1 周（含压测） |
| **性能代价** | 容器冷启动 1-3 s → 3-8 s；syscall 密集型负载（大量文件 IO）吞吐下降 10-30%。用 `runsc --platform=systrap` 可部分抵消 |
| **过渡期补偿建议** | 对高敏感租户直接给 **独占 Cell**（ADR-006 的 `dedicated_cell`）—— 这比内核沙箱的隔离更强（整套栈物理独立），且**现在就可用** |

### 8.2 warehouse-proxy sidecar（凭证零接触） — Q5 决策延后

| 项 | 内容 |
|----|------|
| **当前方案（Q5 已接受）** | 仓库凭证以 env 注入 task-runner：只注入本工作副本的那一个 gateway、只读账号（写操作走 L2）、任务结束销毁 Secret |
| **残余风险** | 用户代码可读到**他自己工作区配置的**仓库凭证。**这是可接受的**（他本来就配置了它），但意味着：① 凭证可能被用户代码外传（受 egress-proxy 审计但非阻断）；② 若同一凭证被多个用户共享（团队共用账号），一个成员可拿到该账号 |
| **必须启用的触发条件** | ① 客户要求"平台不得让终端用户接触仓库凭证"；② 工作区成员间需要**凭证级**隔离（而非仅工作副本级）；③ 出现凭证外传事件 |
| **启用成本** | 新增 sidecar 容器（SQL 协议透明代理，需支持 PG wire / Snowflake / BigQuery 等目标方言）+ 凭证换取端点。约 2-3 周，**且方言支持是长尾工作** |
| **中间过渡方案（成本更低，建议优先）** | 平台为每个 (workspace, environment) 自动创建**短时效仓库账号**（若仓库支持，如 Snowflake 的 `CREATE USER` + 到期回收 / BigQuery 的 short-lived service account token）。凭证仍在 runner 内可读，但**有效期 = 任务时长**，外传的价值趋零。约 1 周/每种仓库 |

### 8.3 其他待增强项

| 项 | 未覆盖内容 | 触发条件 | 预估 |
|----|-----------|---------|------|
| **用户自定义依赖** | 当前用"离线构建的受管镜像 + 白名单依赖清单"，用户不能自由装包 | 用户强需求自定义依赖 | 租户专属镜像构建流水线 + 供应链扫描（Trivy/Grype），2 周 |
| **跨 Cell 协作** | Q4 决策：**不支持**。不同 Cell 的租户无法共享工作区 | 出现跨组织协作产品需求 | 需重新设计（ADR-006 的核心假设变更），≥ 6 周 |
| **出网 DLP** | egress-proxy 只做目标白名单 + 流量配额审计，不做**内容**检查 | 合规要求防数据外泄 | TLS 中间人 + 内容规则引擎，3 周（且有隐私争议） |
| **沙箱内 syscall 审计** | 无 syscall 级行为基线与异常检测 | 需要入侵检测能力 | Falco / Tetragon 接入 + 规则调优，2 周 |
| **L3_proc 的 T1 收窄** | P3 阶段同 Pod 内其他租约的工作副本目录可见 | 若 P6 被推迟且需在 P3-P6 间承载多租户 | 用 mount namespace + `pivot_root` 把子进程限制在单个工作副本（约 1 周，可作为 L3_proc 的加固版 `L3_proc_ns`） |
| **Secret 静态加密密钥轮转** | 密钥按 Cell 分离，但无自动轮转 | 合规要求密钥轮转周期 | KMS 接入 + 双密钥过渡期解密，1.5 周 |
