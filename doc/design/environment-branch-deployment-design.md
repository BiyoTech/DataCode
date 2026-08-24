# Environment 统一与 Branch Deployment 设计方案

> 目标：把 **Git 分支**、**SQLMesh Environment**、**Dagster Deployment** 三个概念收敛为平台上唯一的一等公民 —— `Environment`。
> 一个 Environment = 一个 Git 分支 + 一个 SQLMesh 虚拟环境 + 一个 Dagster Branch Deployment。

- 状态：设计草案（Draft）
- 关联文档：[backend-architecture.md](backend-architecture.md)、[dataops-platform-design.md](dataops-platform-design.md)、[domain-model.md](domain-model.md)、[rbac-permission-design.md](rbac-permission-design.md)
- 兼容性约定：**不考虑历史兼容**，允许破坏式变更（数据目录、DB schema、API 契约均可重建）

---

## 1. 现状梳理与问题定义

### 1.1 三套概念各自独立

| 维度 | 当前实现 | 关键代码位置 |
|------|---------|-------------|
| **Git 分支** | workspace 级单一分支，记录在 `workspaces.git_branch`；用户可通过 Git 面板自由创建/切换/删除本地分支 | `sqlmesh-service/src/workspace/models.py:43`、`src/api/rest/git.py:250`（checkout）、`:320`（branch/delete）、`:344`（branches） |
| **SQLMesh Environment** | 只是 SQLMesh state 里的一条记录；平台侧无注册表；REST 仅 `GET /environments`、`DELETE /environments/{env}` | `src/api/rest/sqlmesh.py:365-392`、`src/core/environment_service.py` |
| **Dagster Deployment** | ✅ 已是一等公民：一环境一 deployment，名 `{workspace_id}__{env}` 由命名代数派生；schedule/sensor 受部署身份门禁；run 带环境 tag | `dagster-service/src/core/{naming,deployment_manager}.py`、`src/api/rest/deployments.py`、`src/templates/defs/deployment.py.tpl` |

### 1.2 六个具体问题

1. **环境创建是 hack**：前端"新建环境"实际是 `POST /sqlmesh/apply` + `plan_options.create_from`（`frontend/src/components/layout/Toolbar.vue:258-263`）。环境是 apply 的副产物，没有生命周期、没有归属、没有元数据。
2. **分支与环境正交**：切换 Git 分支不会切换 environment，切换 environment 不会切换代码。代码与数据视图可以任意错配，`prod` 环境完全可能是从某个未合并分支 apply 出来的。
3. **一个 working copy 只能停在一个分支**：布局为 `{base}/{tenant_id}/{workspace_id}/{user_id}`（`src/workspace/paths.py:9-11`，`_LAYOUT_DEPTH = 3`）。切换环境需要 `git checkout`，会阻塞未提交改动，且无法并行 plan 两个环境。
4. **State 隔离粒度错位**：`workspace_state_schema()` 取路径末段（`config_ops.py:30-33`），末段是 `user_id`，所以 state schema 实际是 `sqlmesh__{user_id}`。后果是 environment 列表是**用户私有**的，团队成员看不到同一份环境清单，环境无法作为共享资产存在。
5. **Dagster 与 SQLMesh 语义割裂**：~~`SQLMeshResource.plan/apply` 默认 `environment="prod"` 硬编码~~ ✅ Phase 4 修复：改读 `DATACODE_ENVIRONMENT`，缺失即报错；`GET /dagster/assets/graph` 的图现在天然按环境隔离（每环境一个 code location 加载各自分支）。
6. **Schedule 无环境护栏**：~~任意 code location 加载后其 schedule 均可被 daemon 触发~~ ✅ Phase 4 修复：非生产 deployment 的 schedule/sensor 以 STOPPED 加载（护栏边界见 §7.2）。

### 1.3 结论

必须引入**平台级 Environment 注册表**作为唯一真相源，由它同时驱动 Git 分支、SQLMesh 虚拟环境和 Dagster Deployment 三者的生命周期。

---

## 2. 设计目标与原则

### 2.1 目标

| 目标 | 描述 |
|------|------|
| **概念唯一** | 用户界面与 API 中只出现 `environment`，不再出现裸 `branch` 概念 |
| **1:1:1 绑定** | Environment ↔ Git Branch ↔ Dagster Deployment 严格一对一 |
| **代码与数据同源** | 某环境的数据视图，必然由该环境对应分支的代码 apply 产生 |
| **并行开发** | 同一用户可同时持有多个环境的 working copy，互不阻塞 |
| **动作语义统一** | `plan / apply / test / run / merge / diff / destroy` 在 SQLMesh 与 Dagster 两侧含义一致 |
| **生产护栏** | 非生产环境默认不调度、不写生产 schema、可自动回收 |
| **可对账** | 四方状态（注册表 / Git / SQLMesh state / Dagster location）可检测漂移并修复 |

### 2.2 设计原则

1. **注册表优先**：任何环境相关操作先写平台注册表，再由 reconciler 驱动下游（Git / SQLMesh / Dagster），保证幂等与可重放。
2. **Git 是唯一代码载体**：延续现状（`models.py:26-28`），环境不持久化本地路径，路径始终运行时派生。
3. **命名可逆**：`environment name` ⇄ `branch name` ⇄ `deployment name` ⇄ `code location name` 之间是纯函数，双向可推导，集中在单一模块。
4. **保守取交集**：命名规则取 SQLMesh 环境名、SQL schema 标识符、Git ref、Dagster location name、K8s 资源名的合法字符交集。
5. **默认安全**：新建环境默认 `sandbox` 类型、schedule 停用、有 TTL；晋升到生产需要独立权限。

---

## 3. 统一概念模型

### 3.1 概念映射表

| 平台概念 | Git | SQLMesh | Dagster | 说明 |
|---------|-----|---------|---------|------|
| `Environment` | Branch | Environment（虚拟环境 / state 记录） | Deployment（Code Location + Run 命名空间） | 一等公民 |
| `Environment.type = production` | `main` | `prod` | Full Deployment（schedule 启用） | 每 workspace 唯一 |
| `Environment.type = shared` | `env/{name}` | `{name}` | Branch Deployment（schedule 可选） | 团队共享，成员均可见 |
| `Environment.type = sandbox` | `env/{name}` | `{name}` | Branch Deployment（schedule 停用 + TTL） | 个人开发环境 |
| `plan` | `git diff base...HEAD` | `context.plan()` 预览 | Asset graph diff vs base deployment | 只读预览 |
| `apply` | 提交已在分支上 | `context.apply()`（backfill + virtual promote） | Reload code location + 重注册 deployment | 变更落地 |
| `test` | — | `context.test()` + audits | Asset checks | 质量校验 |
| `run` | — | `sqlmesh run`（cron 增量） | `launchPipelineExecution` / schedule tick | 数据刷新 |
| `merge` | `merge {src} → {dst}` + push | — （数据不动） | — （代码位置不动） | 代码晋升（操作一） |
| `promote` | — （合并已完成） | virtual update 到目标环境 | 目标 deployment `Pull & Reload` | 数据与调度落地（操作二） |
| `destroy` | 删除分支 | `invalidate_environment` + drop views | 停止 code location + 删 schedule | 环境销毁 |

### 3.2 命名规则（唯一真相源）

新增共享模块（Python 侧 `sqlmesh-service/src/environments/naming.py`，Dagster 侧镜像实现，Go Gateway 侧仅做正则校验）：

```python
# Environment name: intersection of SQLMesh env name, SQL identifier, Git ref
# and K8s resource name constraints. Lowercase snake_case only.
ENV_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

PRODUCTION_ENV = "prod"
PRODUCTION_BRANCH = "main"
BRANCH_NAMESPACE = "env/"


def branch_of(env: str) -> str:
    """Map an environment name to its Git branch name."""
    return PRODUCTION_BRANCH if env == PRODUCTION_ENV else f"{BRANCH_NAMESPACE}{env}"


def env_of(branch: str) -> str | None:
    """Inverse of branch_of; returns None for branches outside the namespace."""
    if branch == PRODUCTION_BRANCH:
        return PRODUCTION_ENV
    if branch.startswith(BRANCH_NAMESPACE):
        return branch[len(BRANCH_NAMESPACE):]
    return None


def deployment_name(workspace_id: str, env: str) -> str:
    """Dagster deployment / code location name. GraphQL-safe identifier."""
    return f"{_safe(workspace_id)}__{env}"
```

设计要点：
- 环境名不允许 `/` 与 `-`（SQLMesh 会把环境名拼进 schema 后缀 `{schema}__{env}`）。
- 分支使用 `env/` 命名空间，避免与仓库中既有的人工分支冲突；`env_of()` 返回 `None` 的分支被视为**平台外分支**，只读、不可 apply。
- Sandbox 环境名建议约定 `dev_{user_slug}`，由后端在创建时自动建议，但不强制。

### 3.3 目录布局：Git Worktree

**当前**（3 层，`paths.py:31-33`）：

```
{base}/{tenant_id}/{workspace_id}/{user_id}/
    ├── sql-warehouse/
    └── asset-dag/
```

**目标**（4 层 + 共享对象库）：

```
{base}/{tenant_id}/{workspace_id}/{user_id}/
    ├── .repo/                      # bare-ish main clone: shared object database
    └── envs/
        ├── prod/                   # git worktree → branch main        (read-only by default)
        │   ├── sql-warehouse/
        │   └── asset-dag/
        ├── dev_alice/              # git worktree → branch env/dev_alice
        │   ├── sql-warehouse/
        │   └── asset-dag/
        └── feature_orders/         # git worktree → branch env/feature_orders
```

- `WORKSPACE_PATH`（下游所有服务的入参）指向 `.../{user_id}/envs/{env}`，即某个 worktree 根。
- `paths.py` 的 `_LAYOUT_DEPTH` 由 3 → 5（`{tenant}/{workspace}/{user}/envs/{env}`），`parse_workspace_path()` 返回 `(tenant_id, workspace_id, user_id, environment)`。这是破坏式变更，符合"不考虑历史兼容"的约定。
- 选择 `git worktree` 而非多次 clone 的理由：共享 `.git` 对象库（磁盘占用 O(1) 而非 O(envs)）、分支与目录天然 1:1 且 Git 强制同一分支不能被两个 worktree 同时 checkout（这正是我们要的约束）、切换环境零成本（无需 checkout，不会被未提交改动阻塞）。

### 3.4 State 隔离粒度修正（关键）

| 层级 | 当前 | 目标 | 原因 |
|------|------|------|------|
| SQLMesh `state_schema` | `sqlmesh__{user_id}`（取路径 leaf，`config_ops.py:32`） | `sqlmesh__{workspace_id}` | 环境是 workspace 级共享资产，成员必须看到同一份环境清单；同时 environment 之间的 virtual promote / diff 要求共享 state |
| 数据视图 schema | `{schema}__{env}`（SQLMesh 原生） | 不变 | 环境间数据隔离由 SQLMesh 虚拟层保证 |
| Dagster run storage | 单实例共享 | 单实例共享 + `datacode/environment` run tag | 开源版 Dagster 无 deployment 隔离，用 tag + location 命名空间模拟 |

`ensure_state_isolation()` 需要显式接收 `workspace_id`，不能再从路径末段推断：

```python
def workspace_state_schema(workspace_id: str) -> str:
    """Return the workspace-scoped SQLMesh state schema.

    Scoped to the WORKSPACE (not the user, not the environment): environments
    are shared workspace assets and virtual promotion between them requires a
    single shared state store.
    """
    return f"sqlmesh__{_sanitize_identifier(workspace_id)}"
```

副作用：多个成员的多个 worktree 会并发访问同一 state schema。SQLMesh state sync 本身有乐观锁，平台侧再叠加环境级串行锁（见 §6.3）。

#### 3.4.1 state_schema 的归属模型：**生成即真值 + 执行时校验**（已定稿）

`config.yaml` 是 Git 跟踪、用户会直接阅读和编辑的文件，因此"文件里写 A、运行时生效 B"是最坏的失败模式——用户按 SQLMesh 官方文档配置了值却不生效，且无任何提示。为此平台只做两件事，不做第三件：

| 场景 | 行为 | 理由 |
|------|------|------|
| gateway **未声明** `state_schema` | 把托管值**写入文件**（附 YAML 注释说明），随首次 commit 进入 Git | 文件始终是唯一真值；用户后续新增 gateway 也自动补齐，不会因缺值被拦 |
| gateway 声明值 **等于**托管值 | 放行（常规路径，零开销） | 所见即所得 |
| gateway 声明值 **不等于**托管值 | **拒绝加载**，抛 `StateSchemaMismatchError`（409），错误信息给出期望值、实际值与恢复方式 | 分叉在第一次执行时显式暴露，而不是静默写入孤儿 state、数月后在 promote 时炸 |

被显式否决的两个替代方案：

- **运行时强制改写**：配置与生效值不一致，违反最小惊讶原则；且会把"多副本 `config.yaml` 分叉"这一真实故障掩盖成不可见状态。
- **纯文件为准、不设防**：分支/成员各持一份 `config.yaml` 副本，任何一次误改或合并冲突解决都会让该环境把快照写进另一个 schema，promote 时血缘断裂并触发全量重建，且过程无任何报错。

托管值由 `workspace_id` 唯一派生（不再用 `project` 名或路径末段），因此多环境、多成员的副本永远推导出同一个值；超过引擎标识符长度上限时按确定性哈希截断。落地位置：`config_ops.reconcile_state_schema()`，在 `_scaffold_sqlmesh()`（创建时生成）与 `ContextPool._create_entry()`（每次加载时校验）两处调用。

---

## 4. 数据模型

### 4.1 新增表 `environments`

```python
class EnvironmentModel(Base):
    """A platform environment: the single binding of a Git branch, a SQLMesh
    virtual environment and a Dagster deployment.

    The registry is the source of truth. Git branches, SQLMesh state entries and
    Dagster code locations are reconciled FROM this table, never the reverse.
    All timestamps are stored in UTC.
    """

    __tablename__ = "environments"

    id = Column(String(128), primary_key=True)          # {workspace_id}:{name}
    workspace_id = Column(String(128), nullable=False)
    tenant_id = Column(String(64), nullable=False)
    name = Column(String(40), nullable=False)            # ENV_NAME_RE
    branch = Column(String(128), nullable=False)         # derived: branch_of(name)

    # production | shared | sandbox
    env_type = Column(String(16), nullable=False, default="sandbox")
    # Base environment for diff / promote / create_from
    base_env = Column(String(40), nullable=False, default="prod")

    owner_user_id = Column(String(64), nullable=False)

    # creating | active | applying | drifted | archiving | archived
    # ("promoting" 保留但不再使用，见 §6.1)
    status = Column(String(16), nullable=False, default="creating")

    # Dagster deployment binding
    deployment_name = Column(String(160), nullable=False)
    schedules_enabled = Column(Integer, nullable=False, default=0)

    # Last successful apply / promote markers
    last_applied_commit = Column(String(64), nullable=True)
    last_applied_at = Column(DateTime(timezone=True), nullable=True)
    last_promoted_to = Column(String(40), nullable=True)

    # Lifecycle: sandbox environments expire; production never does
    ttl_hours = Column(Integer, nullable=False, default=168)   # 7 days
    expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=_utc_now, onupdate=_utc_now)

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_env_workspace_name"),
        UniqueConstraint("workspace_id", "branch", name="uq_env_workspace_branch"),
        Index("idx_env_workspace_status", "workspace_id", "status"),
        Index("idx_env_expiry", "status", "expires_at"),
    )
```

### 4.2 `workspaces` 表调整

| 字段 | 变更 | 说明 |
|------|------|------|
| `git_branch` | **删除** | 分支不再是 workspace 属性，而是 environment 属性 |
| `production_env` | **新增** `String(40) default "prod"` | 允许 workspace 自定义生产环境名 |
| `default_env` | **新增** `String(40) default "prod"` | 用户打开 workspace 时的默认环境 |

### 4.3 `environment_events` 表（审计）

复用 `plan_apply_history` 的思路，但覆盖全部环境动作，用于 UI 时间线与合规审计：

```python
class EnvironmentEventModel(Base):
    """Append-only audit trail of environment lifecycle actions."""

    __tablename__ = "environment_events"

    id = Column(String(64), primary_key=True)
    environment_id = Column(String(128), nullable=False)
    # create | plan | apply | test | run | promote | merge | destroy | reconcile
    action = Column(String(16), nullable=False)
    actor_user_id = Column(String(64), nullable=False, default="")
    trigger_type = Column(String(16), nullable=False, default="human")  # human | agent | schedule
    commit_hash = Column(String(64), nullable=True)
    succeeded = Column(Integer, nullable=False, default=0)
    detail_json = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("idx_env_event_env", "environment_id", "created_at"),
    )
```

`plan_apply_history` 保留但 `environment` 字段改为外键语义（`environment_id`），避免两套历史。

---

## 5. 服务职责划分

```
┌──────────────────────────────── Frontend (Vue) ────────────────────────────────┐
│  EnvironmentSelector (唯一切换入口)   EnvironmentManager (CRUD/diff)            │
│  GitPanel (只读分支 + commit/push/pull/log + merge/sync)                        │
│  PlanView (plan & apply + Pull & Reload)   AssetDagView (deployment 态)         │
└───────────────────────────────────┬────────────────────────────────────────────┘
                                    │ /api/v1/**  (JWT + RBAC)
┌───────────────────────────────────▼────────────────────────────────────────────┐
│                            Gateway (Go)                                         │
│  新增权限点校验：workspace:env:{read,create,delete,promote}                      │
│  路由：/environments/** → sqlmesh-service ；/dagster/** → dagster-service        │
└──────────────┬────────────────────────────────────────────┬────────────────────┘
               │                                            │
┌──────────────▼──────────────────────────────┐  ┌──────────▼────────────────────┐
│           sqlmesh-service                    │  │        dagster-service        │
│                                              │  │                               │
│  EnvironmentRegistry  ← 唯一真相源            │  │  DeploymentManager            │
│    ├─ CRUD + 状态机                          │  │    ├─ ensure_deployment(env)   │
│    ├─ GitBinder   : worktree / branch 生命周期│  │    ├─ reload_deployment(env)   │
│    ├─ SqlMeshBinder: plan/apply/promote/drop  │  │    ├─ destroy_deployment(env)  │
│    └─ Reconciler  : 四方对账                  │──▶│    └─ schedule gating         │
│                                              │  │                               │
│  git_merge : 晋升合并 / 同步基准（两原语）      │  │  CodeLocation per deployment  │
│  ContextPool (key = worktree path + gateway) │  │                               │
└──────────────────────────────────────────────┘  └───────────────────────────────┘
```

关键决策：**Environment 注册表放在 sqlmesh-service**，因为它已经持有 workspace 元数据、Git 操作能力和 SQLMesh state 访问能力（`src/workspace/*`）。dagster-service 作为下游被动接受 deployment 指令，保持无状态。

---

## 6. 生命周期设计

### 6.1 状态机

```
                 create
                   │
                   ▼
             ┌──────────┐   git branch + worktree ok
             │ creating │───────────────────────────┐
             └────┬─────┘                           │
                  │ failure                         ▼
                  ▼                            ┌────────┐
             ┌─────────┐                       │ active │◄──────────┐
             │ failed  │                       └───┬────┘           │
             └─────────┘             apply ────────┤                │
                                                   ▼                │
                                            ┌───────────┐           │
                                            │ applying  │───────────┘ success
                                            └───────────┘
   reconcile 检测到 Git/state/location 不一致 ──► drifted ──repair──► active
   TTL 到期 / 手动销毁 ──► archiving ──► archived（记录保留，资源释放）
```

`promoting` 状态**未被使用**：晋升拆成两个用户动作后（§6.4），代码合并只改 Git 远端、不进入环境状态机，数据落地复用 `applying`。该状态码保留在枚举里仅为兼容既有记录。

### 6.2 创建环境（端到端）

```
POST /api/v1/workspaces/{ws}/environments
     { name: "feature_orders", base_env: "prod", env_type: "shared", ttl_hours: 168 }
  │
  ├─ 1. 校验：ENV_NAME_RE、名称/分支唯一、配额（每 workspace 环境数上限）、RBAC
  ├─ 2. 注册表插入 status=creating（先写库，保证可重放）
  ├─ 3. GitBinder:
  │      git fetch origin
  │      git branch env/feature_orders origin/main       # from base_env's branch
  │      git worktree add {user}/envs/feature_orders env/feature_orders
  │      git push -u origin env/feature_orders           # 需 workspace:git:push
  ├─ 4. Scaffold 检查：sql-warehouse/config.yaml 的 state_schema 归一到 workspace 级
  ├─ 5. SqlMeshBinder: context.plan(environment="feature_orders",
  │                                 create_from="prod", skip_backfill=True)
  │      → 虚拟环境即时创建，零数据成本（SQLMesh virtual update）
  ├─ 6. DeploymentManager (dagster-service):
  │      POST /api/v1/dagster/deployments/ensure
  │      { workspace_path, environment, env_type, schedules_enabled: false }
  │      → 启动/复用 code location，命名 {workspace_id}__feature_orders
  ├─ 7. 注册表 status=active，写 environment_events(action=create)
  └─ 8. 返回 { environment, branch, worktree_path, deployment_name }
```

失败处理：任一步失败 → status=failed，返回已完成步骤清单；重试为幂等（每步先探测再执行）。

### 6.3 环境级串行锁

同一环境的 `apply / merge / destroy` 必须串行（SQLMesh state 写入 + Git 工作副本写入）。锁键为 `env:{workspace_id}:{name}`，由 worktree 路径派生，因此同一环境的**所有成员、所有 gateway** 竞争同一把锁（`ContextEntry.write_lock` 只覆盖单个 `(worktree, gateway)`，不足以覆盖共享 state）。

`POST /git/sync` 与 `POST /git/commit` 同样做锁前置校验：apply 进行中落地的合并会让"已记录的 applied commit"与"实际跑过的代码"脱钩。晋升合并（`POST /git/merge`）不加锁——它只写远端，不触碰任何工作副本与 state。

竞争时**不排队**：apply 可能跑数分钟，静默等待的 HTTP 请求与卡死无法区分，因此直接返回 409 `ENVIRONMENT_BUSY` 并带上持锁操作名。plan 预览与 test 为读操作，不加锁，靠 `state_fingerprint` 做乐观校验（`POST /plan` 已返回 fingerprint，`sqlmesh.py:61-76`）。

实现：`core/env_locks.py`，作用域为单进程（匹配当前单副本部署）；多副本需在同一接口后换成分布式锁。

### 6.4 晋升：拆成两个用户动作（已定稿，替代原"原子 promote"）

**结论**：晋升**不做原子编排**，拆成两个显式的用户操作。原因是冲突本质上不可原子化——它是一个必须由人在工作副本里离开的中间态。任何"原子 promote"最终都会退化成"尝试 → 报冲突 → 等人解决 → 重试"，只是把这条序列藏进黑盒，可观测性、可重试性、可授权性反而全部变差（§13 原本就把"promote 半途失败"列为风险）。

| 操作 | 位置 | 语义 | 权限 |
|------|------|------|------|
| **操作一：代码合并** | Git 面板 | `merge` 源环境分支 → 目标环境分支，并 push | `workspace:git:push`；目标为 `main` 时**追加** `workspace:env:promote` |
| **操作二：数据与调度落地** | Plan & Apply 页 | 目标环境 `plan & apply`（sqlwarehouse tab）+ `Pull & Reload`（asset-dag tab） | `workspace:plan:apply` / `workspace:pipeline:run` |

#### 6.4.1 代码合并的两个原语（关键设计）

合并**不是**一个原语而是两个，因为"在哪个 worktree 执行 merge"没有单一答案：目标环境的 worktree 属于特定成员且可能是只读态，而在源 worktree 里 `git merge` 又无法把改动合进另一个分支。

| 原语 | 端点 | 执行位置 | 冲突处理 |
|------|------|---------|---------|
| **晋升合并** | `POST /api/v1/git/merge` | 调用者自己的工作副本（只借用其共享对象库与 remote，**不修改任何工作副本**） | `git merge-tree --write-tree` 预检；有冲突直接 409 `GIT_MERGE_CONFLICT` 并内联冲突文件清单，**不写任何东西** |
| **同步基准** | `POST /api/v1/git/sync` | 调用者自己的工作副本内 | 冲突**故意留在工作区**，在编辑器 + Git 面板的 stage/commit 里解决 |

晋升合并的实现（`workspace/git_merge.py`）：

```
POST /api/v1/git/merge  { source_branch, target_branch, message? }
  ├─ 1. 两侧分支必须是平台管理分支（env_of != None），否则 409
  ├─ 2. 目标 = main 时校验 workspace:env:promote（服务侧二次校验，目标分支在 body 里）
  ├─ 3. fetch --prune（失败即中止：基于过期 ref 的合并结论比不合并更糟）
  ├─ 4. ref 解析优先 refs/remotes/origin/*（绝不晋升团队已推进的分支的过期本地副本）
  ├─ 5. 源分支有未推送提交 → 409（否则会静默少合并）
  ├─ 6. merge-tree 预检 → 有冲突 409 GIT_MERGE_CONFLICT（内联 conflicts[]）
  ├─ 7. commit-tree 造双亲 merge commit（永不 fast-forward，便于整体 revert）
  ├─ 8. push <sha>:refs/heads/{target}（非强推；被拒即目标已推进，需重算）
  └─ 9. environment_events 记 action=merge（成功/失败均记）
```

**为什么直推 commit 对象而不是先移动本地分支**：远端成为该合并唯一存在的地方，任何成员的工作副本都不会被他人的晋升在背后改写；目标环境用自己的 `Pull & Reload` 显式取回，这正好是操作二。

**因此平台永远不需要 Web 三方合并编辑器**——冲突解决路径固定为「在源环境 sync 目标分支 → 编辑器里改 → 提交 → 重新 merge」，全部复用既有 UI。

#### 6.4.3 pull 的集成策略由平台决定（不用 `git pull`）✅

`POST /api/v1/git/pull` **不执行 `git pull`**。Git ≥ 2.27 在分支分叉时会直接拒绝执行（`fatal: Need to specify how to reconcile divergent branches`），除非机器上配了 `pull.rebase`／`pull.ff`。这带来两个不可接受的后果：环境分支（本地与远端同时有提交是常态）根本拉不动；而结果又取决于服务器的环境配置，同一操作在不同机器上语义不同。

因此 `GitOperations.pull` 显式三步，且与任何 git 配置无关：

```
POST /api/v1/git/pull  { branch? }
  ├─ 1. 环境锁预检（拉取会改工作树，apply 进行中拒绝，同 commit / sync）
  ├─ 2. fetch origin +refs/heads/{branch}:refs/remotes/origin/{branch}
  │     （显式 refspec，绝不依赖 FETCH_HEAD，使随后的 status 与本次判定同源）
  ├─ 3. 远端 tip 已是 HEAD 祖先 → up_to_date（含"本地领先"，不失效 context）
  ├─ 4. 有未提交的**已跟踪**改动 → 409（什么都没写；未跟踪文件不阻塞）
  ├─ 5. HEAD 是远端 tip 祖先 → merge --ff-only（不产生 merge commit）
  └─ 6. 否则造真实双亲 merge commit；冲突留在工作区 → 200 + conflicts[]
```

| 判定 | `outcome` | HTTP | 是否失效 context |
|------|-----------|------|------------------|
| 已最新 / 本地领先 | `up_to_date` | 200 | 否 |
| 仅落后 | `fast_forward` | 200 | 是 |
| 分叉，合并成功 | `merged` | 200 | 是 |
| 分叉，有冲突 | `conflicted` | 200（内联 `conflicts[]`） | 是（冲突标记是真实文件内容） |
| 工作副本有未提交改动 | `blocked` | 409 | 否 |
| 远端拒绝 / 分支不存在 | — | 422（`git_error_reason`） | 否 |

**为什么是 merge 而不是 rebase**：环境分支是团队共享的，rebase 会重写已推送提交，在他人脚下改写历史不是一次 pull 该做的事。

**为什么冲突是 200**：与 `POST /sync` 同一契约——冲突文件已在工作区，等待人在编辑器里解决，这是解决流程的**成功开始**而非失败。`Pull & Reload` 因此在 `conflicted` 时**中止重载**：否则会把带冲突标记、根本无法解析的代码导入 Dagster。

#### 6.4.2 残余风险（显式接受）

合并已 push、但目标环境尚未 apply + reload 时，分支领先于数据。该窗口**不新增问题类别**：reconciler 的 `commit_drift`（§11.1）已能观测它，部署面板也会在 `behind > 0` 时提示"远端有未拉取的提交"。不为消除这个窗口重建编排器。

将来若要"一键晋升"，它只会是把这几个已验证原语串起来的薄封装——拆分为它保留了地基。

### 6.5 销毁与回收

```
DELETE /api/v1/environments/{env}          # production 拒绝
  ├─ 1. status=archiving
  ├─ 2. Dagster: destroy deployment（停 code location、删 schedule、取消在跑 run）
  ├─ 3. SQLMesh: state_sync.invalidate_environment(env) + delete_expired_environments()
  ├─ 4. Git: worktree remove + branch -D + push --delete origin env/{env}
  └─ 5. status=archived（保留注册表记录与事件历史，便于审计）
```

后台 reaper（定时任务）：扫描 `status=active AND env_type='sandbox' AND expires_at < now()`，先发提醒事件，宽限期后自动执行销毁流程。所有时间判断使用 UTC。

---

## 7. Dagster Branch Deployment 设计

### 7.1 Deployment 与 Code Location

| 项 | 规则 |
|----|------|
| Deployment name | `{workspace_id}__{env}`，来自 `deployment_name()` 纯函数 |
| Code location name | 同 deployment name（1:1） |
| 加载路径 | `{base}/{tenant}/{ws}/{user}/envs/{env}/asset-dag/definitions.py` |
| gRPC 端口 | dev：进程内递增分配 + 持久化到 `DAGSTER_HOME/code_location_ports.json`；生产：worker pool 共享（见 §7.5） |
| 注入环境变量 | `DATACODE_ENVIRONMENT`、`DATACODE_DEPLOYMENT`、`DATACODE_DEPLOYMENT_TYPE`、`DATACODE_WORKSPACE_ID`、`DATACODE_TENANT_ID`、`DATACODE_SCHEDULES_ENABLED`、`SQLMESH_GATEWAY`、`SQLMESH_SERVICE_URL`、`DAGSTER_WORKSPACE_PATH` |

`code_location_manager` 不再自己推导名字：deployment 名由 `DeploymentManager` 从平台命名代数派生后下发（路径推断在五层布局下会产出 `ws_envs_{env}` 这类既无 workspace 维度、又跨 workspace 冲突的名字）。

**身份来源（已定稿）**：`workspace_path` 是身份的唯一权威来源 —— 它编码了 `(tenant, workspace, user, environment)`，deployment 名是后两者的纯函数，因此 dagster-service 镜像一份命名代数（`src/core/naming.py`）自行派生，而不把注册表变成可用性依赖。请求体里的 `environment` 只能**确认**、不能**改写**（不一致直接 400）；注册表真正需要下发的是路径无法表达的**策略**（`env_type`、`schedules_enabled`），缺省时按"只有 prod 环境是 production 部署、只有 production 默认被调度"的安全默认取值。

端口持久化的理由：端口出现在 `workspace.yaml`、remote 模式的注册信息和运维记录里，重启后静默漂移会同时让这三者失效。

### 7.2 Schedule 护栏（核心安全设计）

gating 落在 scaffold 的一个**受管文件** `asset-dag/defs/deployment.py` 上（`definitions.py.tpl` 无法承载它：`definitions.py` 要 import `defs` 包，`defs` 下的模块再反向 import 就成了循环依赖）：

```python
# defs/deployment.py (excerpt) —— PLATFORM-MANAGED，内容漂移即被强制覆盖
import os

import dagster as dg

# Deployment identity injected by dagster-service when the code location starts.
DEPLOYMENT_TYPE = os.getenv("DATACODE_DEPLOYMENT_TYPE", "sandbox")
ENVIRONMENT = os.getenv("DATACODE_ENVIRONMENT", "")

# Only a deployment the platform allows to be scheduled may have running
# schedules. Branch deployments load the very same code, so without this gate a
# feature branch would silently drive production data on every tick.
SCHEDULES_ENABLED = os.getenv("DATACODE_SCHEDULES_ENABLED", "") == "1"

DEFAULT_SCHEDULE_STATUS = (
    dg.DefaultScheduleStatus.RUNNING
    if SCHEDULES_ENABLED
    else dg.DefaultScheduleStatus.STOPPED
)
DEFAULT_SENSOR_STATUS = ...  # 同理，sensor 也会自行拉起 run
```

- 用户在 `defs/schedules/`、`defs/sensors/` 中声明时统一 `default_status=DEFAULT_SCHEDULE_STATUS`（scaffold 的示例文件即以此写法示范）。
- `defs/deployment.py` 是受管文件（与 `defs/assets/sql_assets.py` 同列），内容漂移即被模板覆盖，所以门禁本身改不掉；示例 schedule/sensor 仍是**用户可编辑**文件 —— 覆盖它们会吃掉用户改的 cron。
- 允许显式为某个 shared 环境开启：注册表 `schedules_enabled=1` → 注入独立变量 `DATACODE_SCHEDULES_ENABLED=1`，**不复用** `DATACODE_DEPLOYMENT_TYPE=production`（那会污染语义）。API 层同时拒绝把非 prod 环境标成 `production` 类型，避免绕过该分工；开启需 `workspace:env:promote`（✅ 权限点已实现）。
- **护栏边界（诚实声明）**：`default_status` 只决定 schedule 在实例里的**初始状态**，开源版 Dagster 的 daemon 只看实例状态。因此这条链路防的是"分支环境加载后默认就被调度"，不能阻止用户在代码里硬写 `RUNNING`、也不能阻止有人在 Dagster UI 里手动启用。真正的强隔离在 §7.5 的生产形态（per-deployment worker + 命名空间）里解决。

### 7.3 Run 隔离与环境注入

| 隔离点 | 实现 |
|--------|------|
| Run 归属 | 每次 launch 注入 run tag `datacode/environment`、`datacode/workspace_id`、`datacode/deployment`（GraphQL `executionParams.executionMetadata.tags`） |
| SQLMesh 目标环境 | `SQLMeshResource` 不再默认 `"prod"`，改为读 `DATACODE_ENVIRONMENT`；两者都缺时**报错而非兜底**（唯一安全的默认就是本 deployment 自己的环境，读不到说明进程不知道自己在哪运行） |
| 数据写入 | SQLMesh 虚拟层保证：sandbox 环境写 `{schema}__{env}`，物理表复用 base 快照 |
| 资源配额 | 生产 K8sRunLauncher 按 deployment type 差异化 requests/limits；sandbox 并发上限低 |
| UI 过滤 | 前端 asset-dag / run history 按当前 environment 的 deployment name 过滤，不展示他人环境 |

### 7.4 Asset Graph Diff（对齐 Dagster+ 能力）

`GET /api/v1/environments/{env}/diff?target=prod` 返回三段并集：

```
{
  "code":  { "files_added": [...], "files_modified": [...], "commits_ahead": 3 },
  "sqlmesh": { "added": [...], "directly_modified": [...], "indirectly_modified": [...],
               "removed": [...], "backfill_required": [...] },
  "assets": { "added": ["asset_x"], "changed": ["asset_y"], "removed": [],
              "schedule_changes": [{ "name": "daily_refresh", "cron": "0 2 * * *" }] }
}
```

- `code` 段来自 Git（`GET /api/v1/git/branch-diff`，内部用 `git diff --name-status {target}...{source}` 的三点语义）。
- `sqlmesh` 段复用现有 plan 预览的 `format_plan_result`（`core/plan_service.py`）。
- `assets` 段由 dagster-service 的 `GET /api/v1/dagster/deployments/diff?deployment=&base=` 提供（✅ Phase 4）：比较两个 code location 的 asset / schedule 定义集合。asset 的"变更"按 `(group, 依赖集合)` 指纹判定；两个 deployment 都必须已加载 —— 定义集合只能从运行中的 code location 读出，把读不到当成空会把对方的全部 asset 报成"已删除"。
- **三段聚合端点已推迟（可选）**：晋升拆分后，三段各有天然归属且各自已就绪 —— `code` 在 Git 面板的合并对话框、`sqlmesh` 在 Plan 预览、`assets` 在部署面板。再造一个聚合端点只会多一个必须与三方保持同步的读模型。真正需要它的场景是"在一个视图里同时看三段"，属 Phase 6 的 `EnvironmentDiffPanel` 决定。

前端在 asset DAG 上按 diff 结果给节点着色（新增/变更/删除），这是 Branch Deployment 最直观的价值点。

### 7.5 生产形态（K8s）

| 组件 | 形态 |
|------|------|
| DagsterInstance | StatefulSet（daemon 单实例 + webserver 可扩）+ PostgreSQL 元数据 |
| dagster-service | 无状态 Deployment，`dagster_mode=remote`，只做注册与编排，不 spawn 子进程 |
| Code Location | Worker Pool：固定 N 个 worker，每个 worker 通过一致性哈希承载多个 deployment，LRU 淘汰空闲 slot |
| workspace.yaml | 由 `python_module` 动态 resolver 从注册表读取活跃 deployment 列表，替代文件写入，消除并发写竞争 |
| Run 执行 | `K8sRunLauncher`，每 run 一个 Job；按 deployment type 分配资源与命名空间 |

Deployment 数量控制：`活跃 deployment ≈ Σ(workspace × 活跃环境)`，而不是当前的 `Σ(workspace × 用户)`。配合 TTL 回收与 idle 卸载，规模可控。

---

## 8. API 设计

### 8.1 新增：Environment 一等公民 API（sqlmesh-service）

| Method | Path | 权限 | 说明 |
|--------|------|------|------|
| GET | `/api/v1/workspaces/{ws}/environments` | `workspace:env:read` | 列出注册表环境（含 status/type/drift/TTL/last_applied） |
| POST | `/api/v1/workspaces/{ws}/environments` | `workspace:env:create` | 创建环境（建分支 + worktree + 虚拟环境 + deployment） |
| GET | `/api/v1/environments/{id}` | `workspace:env:read` | 环境详情（含 worktree 路径、Git ahead/behind、deployment 状态） |
| DELETE | `/api/v1/environments/{id}` | `workspace:env:delete` | 销毁环境（production 拒绝） |
| POST | `/api/v1/environments/{id}/open` | `workspace:read` | 为当前调用者物化 worktree，返回 `workspace_path` |
| POST | `/api/v1/environments/{id}/plan` | `workspace:plan:preview` | 预览（含 fingerprint） |
| POST | `/api/v1/environments/{id}/apply` | `workspace:plan:apply` | 落地（同步/异步两态，SSE 进度） |
| POST | `/api/v1/environments/{id}/test` | `workspace:plan:preview` | 单测 + audits + asset checks |
| POST | `/api/v1/environments/{id}/run` | `workspace:pipeline:run` | 触发数据刷新（SQLMesh run 或 Dagster job） |
| GET | `/api/v1/environments/{id}/events` | `workspace:env:read` | 生命周期事件时间线 |
| POST | `/api/v1/environments/{id}/reconcile` | `workspace:env:create` | 手动触发四方对账修复 |

`POST /environments/{id}/promote` 与 `GET /environments/{id}/diff` **已从计划中移除**：晋升拆成两个用户动作（§6.4），代码合并走 `/api/v1/git/{merge,sync,branch-diff}`，数据落地走既有的 plan/apply + deployment reload；三段 diff 聚合推迟（§7.4）。

### 8.2 变更：现有 API

| API | 变更 | 原因 |
|-----|------|------|
| `POST /api/v1/plan`、`/apply`、`/apply/async` | ✅ 入参 `environment` 已改为**必填**且必须存在于注册表；`plan_options.create_from` 显式报错 | 环境创建收敛到注册表 |
| `DELETE /api/v1/environments/{environment}` | ✅ 已废弃（由 `DELETE /api/v1/environments/{id}` 取代，走完整销毁编排） | 直接删虚拟环境会留下孤儿分支与 deployment |
| `GET /api/v1/environments` | ✅ 已迁为 `GET /api/v1/sqlmesh/engine/environments`，仅供 reconciler 与排障使用 | 与注册表区分 |
| `POST /api/v1/workspaces/{id}/open` | ✅ body 的 `branch` 已改为 `environment`（前后端同步） | 概念统一 |
| `GET /api/v1/workspaces/{id}/remote-branches` | ✅ 保留但降级为 discover 视图（附 `environment` / `managed` 标注），供 Phase 5 的 adopt/reconcile 使用；用户改用 `GET /workspaces/{id}/environments` | 用户不再面对裸分支 |

### 8.3 Git API：分支操作的收敛与新增

已废弃（Phase 3 完成）：

| API | 处置 |
|-----|------|
| `POST /api/v1/git/checkout` | **已删除**（切换环境即切换 worktree，不再 checkout）；底层 `GitOperations.checkout` / `fetch_and_checkout` 一并移除，因已无调用方 |
| `POST /api/v1/git/branch/delete` | **已删除**（改由环境销毁编排）；`GitOperations.delete_branch` 保留，仅由 `GitBinder` 内部调用 |
| `GET /api/v1/git/branches` | 已降级为只读，每项返回 `environment`（`env_of` 结果，平台外分支为 `null`）与 `managed` 标记 |

新增（✅ 晋升拆分，见 §6.4）：

| Method | Path | 权限 | 说明 |
|--------|------|------|------|
| GET | `/api/v1/git/branch-diff` | `workspace:code:read` | 合并预览：提交列表、变更文件、`merge-tree` 冲突判定；带 `file_path` 时返回单文件 patch |
| POST | `/api/v1/git/merge` | `workspace:git:push`（目标 `main` 时追加 `workspace:env:promote`） | 晋升合并：预检 + 造双亲 merge commit + 直推远端，**不改任何工作副本** |
| POST | `/api/v1/git/sync` | `workspace:code:write` | 把基准分支合并进当前工作副本；冲突留在工作区供编辑器解决 |

保留的 Git API（工作副本内的日常操作）：`status / stage / unstage / commit / push / pull / fetch / reset / log / commit-files / commit-file-diff / init / publish / credentials/*`。其中 `POST /pull` 的语义已由"透传 `git pull`"改为"平台决定集成策略"（见 §6.4.3）：响应新增 `outcome / conflicted / conflicts[] / commit / integrated_commits`，冲突为 200、工作副本脏为 409。

### 8.4 新增：dagster-service Deployment API（✅ Phase 4）

| Method | Path | 权限 | 说明 |
|--------|------|------|------|
| POST | `/api/v1/dagster/deployments/ensure` | `workspace:pipeline:run` | 幂等确保 deployment 就绪（注册表 / UI 均可调用） |
| POST | `/api/v1/dagster/deployments/reload` | `workspace:pipeline:run` | 代码变更后热重载 |
| DELETE | `/api/v1/dagster/deployments/{name}` | `workspace:env:delete` | 停止并清理（请求只带 deployment 名，故 scope 可选） |
| GET | `/api/v1/dagster/deployments` | `workspace:pipeline:read` | 列出活跃 deployment（供 reconciler 对账） |
| GET | `/api/v1/dagster/deployments/diff` | `workspace:pipeline:read` | 两个 deployment 的 asset/schedule 定义差异 |

现有 `/dagster/pipelines/code-location/{ensure,reload,code-locations}` 已**删除**（不考虑历史兼容），由上述 API 取代；`/dagster/pipelines/run` 保留并自动注入 run tag。`/dagster/assets/graph`、`/runs/**`、`/schedules` 的 `workspace_path` 参数语义自动变为"某环境的 worktree"，无需改签名。

注意 deployment 注册表是**进程内状态**：deployment 是 `ensure` 随时可以从注册表/工作副本重建的派生态，落库只会多出一份需要对账的真相。dagster-service 重启后 `GET /deployments` 为空，直到各环境被重新 ensure —— 这正是 `missing_deployment` 对账修的东西（§11.1）：注册表侧一次 reconcile 即可按各环境的 `env_type` / `schedules_enabled` 策略把 deployment 连同调度门禁一起恢复，不必等谁打开对应环境的页面。

---

## 9. 前端设计

### 9.1 组件变更

| 组件 | 变更 |
|------|------|
| `layout/Toolbar.vue` | 环境选择器改为读注册表 API；移除"用 apply + create_from 建环境"的 hack（`:258-263`）；新增 drift/TTL 角标 |
| **新增** `environment/EnvironmentManager.vue` | 环境列表（type/status/owner/base/TTL/last_applied）、创建向导、销毁、diff 视图入口 |
| **新增** `environment/EnvironmentDiffPanel.vue` | 三段 diff 展示（code / sqlmesh / assets），支持在 DAG 上高亮 |
| `git/GitDetailPanel.vue` | ✅ 已移除创建/切换/删除分支入口；✅ 新增**晋升合并对话框**（分支对选择、提交与文件清单、`merge-tree` 冲突判定、单文件 diff 打开为编辑器标签）与**同步基准分支**入口；冲突文件在面板内列出并引导到编辑器解决 |
| `plan/AssetDagDeployPanel.vue` | ✅ 移除 promote 预留横幅；reload 升级为 **Pull & Reload**（拉取失败即中止，避免"重载了但代码还是旧的"）；`behind > 0` 时提示远端有未拉取的提交 |
| `layout/WorkspaceSelector.vue` | 打开工作区时的"分支选择弹窗"（`:319-333`）改为"环境选择" |
| `components/asset-dag/*` | 顶部显示当前 deployment 名与 schedule 启用状态；ScheduleManager 在非生产环境展示"已停用（分支部署）"提示并禁用手动启用（除非有 promote 权限） |
| `stores/environment.ts` | 从 31 行的纯状态扩展为完整 store：环境列表加载、创建/销毁、当前环境持久化、切换时联动重置 plan/models/dagster/git store |
| `stores/git.ts` | ✅ 已移除分支写操作 action；✅ 新增 `loadMergePreview` / `fetchBranchFileDiff` / `mergeBranches` / `syncWithBase` |
| `composables/useWorkspaceLifecycle.ts` | 切环境时不再固定 reset 为 `'prod'`，改为 workspace 的 `default_env`（`:41-42`） |

### 9.2 交互原则

1. **环境是全局上下文**：顶部环境选择器是唯一入口，切换后 Models / Asset DAG / Plan / Git / Chat 全部随之切换（Chat 已上报 environment，`createChatStore.ts:390-397`，保持不变）。
2. **生产环境只读默认态**：选中 production 环境时，文件编辑默认禁用，提示"请切换到开发环境修改后晋升"。
3. **危险动作二次确认**：合并到生产分支、destroy environment、启用分支环境 schedule，均需显式确认 + 展示影响面（合并对话框展示提交数、文件清单与冲突判定）。
4. **i18n**：所有新增文案进中英文资源文件；后端日志与存储一律英文。
5. **时区**：环境的 `created_at / expires_at / last_applied_at` 后端返回 UTC，前端按用户时区渲染。

---

## 10. RBAC 权限点

新增（`gateway/internal/rbac/catalog.go`，workspace scope）：

| 权限码 | 描述 | 默认授予角色 | 状态 |
|--------|------|-------------|------|
| `workspace:env:read` | 查看环境列表与详情 | owner / engineer / analyst / viewer | ✅ Phase 3 |
| `workspace:env:create` | 创建环境（含建分支与推送） | owner / engineer | ✅ Phase 3 |
| `workspace:env:delete` | 销毁非生产环境 | owner / engineer | ✅ Phase 3 |
| `workspace:env:promote` | 合并到生产分支 / 开启分支环境调度 | owner | ✅ 已实现 |

存量租户无需迁移：`Store.EnsureTenantRoles` 对"未被租户自定义过"的内置角色（其权限集是模板的子集）自动回填新增权限，网关重启即生效。

`POST /environments/{id}/open` 只要求 `workspace:read`：物化自己的工作副本不产生任何共享资源，与创建环境是不同量级的动作。

调整：
- `workspace:plan:apply` 对生产环境仍然适用：晋升拆分后，对生产 apply 是"操作二"的正常组成部分，代码进入生产的关口由**操作一的 merge** 用 `workspace:env:promote` 把守。
- `workspace:git:push` 是环境创建（要推分支）与晋升合并（要推目标分支）的前置条件。
- `POST /api/v1/git/merge` 的目标分支在请求体里，路由映射是静态的，因此网关只按 `workspace:git:push` 放行，服务侧再对 `target == main` 追加校验 `workspace:env:promote`（纵深防御，权限集由网关下发的 `X-Granted-Permissions` 提供）。

---

## 11. 一致性与漂移治理

### 11.1 四方对账

| 真相源 | 查询方式 |
|--------|---------|
| 注册表 | `environments` 表 |
| Git | `git ls-remote --heads` + `env_of(branch)` 过滤 |
| SQLMesh state | `GET /api/v1/environments`（引擎侧真实环境） |
| Dagster | `GET /api/v1/dagster/deployments` |

Reconciler（定时 + 手动触发）产出漂移分类与修复动作：

| 漂移类型 | 现象 | 修复动作 | 状态 |
|---------|------|---------|------|
| `orphan_branch` | Git 有 `env/x`，注册表无 | **仅上报**（adopt 是人工决定：自动接管会建出无人要求的 schema 与 deployment） | ✅ |
| `missing_branch` | 注册表有，Git 无 | 从 `base_env` 重建分支并推送；本地有、远端无时补推 | ✅ |
| `missing_virtual_env` | 注册表 active，引擎无该环境 | `ensure_virtual_environment` 重建（指针切换，零数据成本） | ✅ |
| `orphan_sqlmesh_env` | 引擎有虚拟环境，注册表无 | invalidate + drop views | ✅ |
| `stale_resources` | 注册表已 archived，但分支/工作副本/虚拟环境仍在 | `release_archived_resources`（补完 destroy 未完成的释放） | ✅ |
| `commit_drift` | `last_applied_commit` ≠ 分支 HEAD | **仅上报**：重新 apply 的 backfill 成本只能由用户授权 | ✅ |
| `applied_dirty` | 数据由未提交代码 apply 产生（`last_applied_dirty`） | **仅上报**：提交后重新 apply 即可消除 | ✅ |
| `missing_deployment` | 注册表 active，Dagster 无 location | `ensure_deployment` 重建（经任一成员 worktree，携带行上 `env_type`/`schedules_enabled` 策略） | ✅ |
| `stale_deployment` | Dagster 有，注册表已 archived | `release_archived_resources` 统一释放序列内 destroy deployment | ✅ |

（注册表侧经 `deployment_binder` 直连 `services.dagster.rest_url` 调用这三端点，create/destroy 联动与对账均已落地。两个实现要点：served 列表跨 workspace，stale/missing 判定前先按 `workspace_id` 过滤，避免误判他仓 deployment；`registry.reconcile` 的外层环境锁只对 live 环境持有——归档修复走的 `release_archived_resources` 内部自取同 key 锁，而锁不可重入。）

**"不可读"≠"不存在"**：仓库不可达时 SQLMesh 侧结论一律不下（`sqlmesh_reachable=false`）；dagster-service 不可达时同理（`dagster_reachable=false`），否则一次服务重启窗口就会触发全量误判。若把"读不到"当作"没有环境"，一次例行的仓库故障就会让 reconciler 重建或失效整个 workspace 的全部环境。

**修复的边界**：只做无歧义且零数据成本的动作（重建注册表自己拥有的分支、重建虚拟环境、重新 ensure 注册表自己拥有的 deployment、释放已归档环境的残留、把 `drifted` 收敛回 `active`）。任何需要计算数据或接管无人注册状态的事情一律只上报。

`commit_drift` 是最高频也最有价值的一项：它把"代码与数据同源"这条原则变成可观测状态，直接在环境选择器上以角标呈现。

### 11.2 平台外分支

`env_of(branch) is None` 的分支（例如从外部仓库带来的 `release/1.0`）：只读展示、不可 apply、不可作为环境 base。若用户要基于它开发，走"adopt 为环境"流程（新建环境 + `--from` 该分支）。

---

## 12. 实施规划

### Phase 0：设计确认与契约冻结（0.5 周）

- 交付：本文档评审通过；`naming.py` 命名函数与 `ENV_NAME_RE` 冻结；API 契约冻结（OpenAPI 草案）。
- 验收：Gateway / sqlmesh-service / dagster-service / frontend 四方对命名与 API 无异议。

### Phase 1：目录布局与注册表骨架（1.5 周）

- 影响文件
  - `sqlmesh-service/src/workspace/paths.py`（3 层 → 5 层，`parse_workspace_path` 返回四元组）
  - `sqlmesh-service/src/workspace/layout.py`（worktree 感知；`.gitignore` 增加 `/.repo/`、`/envs/`）
  - `sqlmesh-service/src/workspace/git_ops.py`（新增 `worktree_add / worktree_remove / worktree_list`）
  - **新增** `sqlmesh-service/src/environments/{naming.py,models.py,repository.py,registry.py}`
  - `sqlmesh-service/alembic/`（`environments`、`environment_events` 迁移；`workspaces.git_branch` 下线）
- 交付：注册表 CRUD + Git 分支/worktree 绑定可用；`POST /environments/{id}/open` 返回 worktree 路径。
- 验收：同一用户可同时持有 3 个环境 worktree，互不干扰；`git worktree list` 与注册表一致。

### Phase 2：State 归一与 SQLMesh 语义对齐（1 周）✅ 已实现

- 影响文件
  - `sqlmesh-service/src/workspace/config_ops.py`（重写为 `workspace_state_schema(workspace_id)` + `reconcile_state_schema()`：缺值生成进文件、异值拒绝加载，见 §3.4.1）
  - **新增** `sqlmesh-service/src/workspace/identity.py`（由 `workspace_path` 解析 `(tenant, workspace, user, environment)`，供下游统一使用）
  - `sqlmesh-service/src/core/context_pool.py`（加载前校验 state_schema；`ContextConfigError` 改为报告 workspace + environment 而非路径末段）
  - **新增** `sqlmesh-service/src/core/env_locks.py`（环境级串行锁，忙时 409 `ENVIRONMENT_BUSY`）
  - **新增** `sqlmesh-service/src/environments/guard.py`（plan/apply 目标必须是注册环境，且必须等于当前 worktree 的环境；apply 成功后回写 `last_applied_commit`）
  - **新增** `sqlmesh-service/src/environments/sqlmesh_binder.py`（虚拟环境创建/失效；`create_from` 仅此处可用）
  - `sqlmesh-service/src/environments/registry.py`（create 联动虚拟环境创建，失败置 `drifted`；destroy 先释放虚拟环境再释放 Git 资源）
  - `sqlmesh-service/src/core/plan_service.py`（守卫 + 环境锁；`_build_plan` 移除 `create_from`）
  - `sqlmesh-service/src/core/environment_service.py`（降级为引擎侧只读视图，删除 `delete_environment`）
  - `sqlmesh-service/src/api/models/requests.py`（`environment` 必填；`plan_options.create_from` 显式报错而非静默忽略）
  - `scripts/sqlmesh-cli/sqlmesh.sh`（移除 `--create-from`）
- 交付：环境列表对同一 workspace 的所有成员一致；plan/apply 只能作用于注册环境；新建环境即时获得零成本虚拟环境。
- 验收：两个成员分别 open 同一环境，看到同一份环境清单与同一份 state。
- 遗留（归入后续 Phase）：`environments/{id}/plan|apply` 端点收敛（Phase 5）；前端仍在用 `apply + create_from` 建环境的入口需在 Phase 6 替换为注册表 API。

### Phase 3：Git 概念收敛（0.5 周）✅ 已实现

原计划仅含前后端 Git 入口收敛，实施中发现**两处 Phase 1 遗留回归**必须同时修复，否则整条链路不可用（详见下方"回归修复"）。RBAC 权限点原定 Phase 5，因是本阶段验收的前置，故提前落地 `env:read/create/delete` 三项（`env:promote` 仍留 Phase 5）。

- 影响文件
  - **Gateway**
    - `gateway/internal/rbac/wspath.go`（重写：新增 `ParseWorkingCopy` 统一解析 `{tenant}/{ws}/{user}/envs/{env}` 五段布局，派生 `WorkspaceIDFromPath` / `OwnsWorkingCopy` / `WorkspaceIDFromEnvironmentID`）
    - `gateway/internal/rbac/catalog.go`（新增 `workspace:env:{read,create,delete}` 权限码与角色授予；`EnsureTenantRoles` 的子集回填机制使存量租户重启即生效，无需迁移）
    - `gateway/internal/rbac/routemap.go`（新增 `ScopeSourceEnvironmentSegment`：由 `{workspace_id}:{env}` 路径段解析 workspace；新增 5 条环境规则，GET 兜底规则确保未来的环境读端点不会退化为"仅认证"）
    - `gateway/internal/middleware/authorize.go`（接入新 scope source）
    - `gateway/internal/router/router.go`（新增 `/workspaces/{id}/environments` 与 `/environments/*` 转发）
    - `gateway/internal/handler/proxy_routes.go`（所有权校验改用 `OwnsWorkingCopy`；`GET /environments` 残留规则改为 `GET /engine/environments`）
  - **sqlmesh-service**
    - `src/api/rest/git.py`（删除 `POST /checkout`、`POST /branch/delete`；`GET /branches` 每项附 `environment` + `managed` 标注，并返回 `current_environment`）
    - `src/api/models/requests.py`（删除 `GitCheckoutRequest`、`GitDeleteBranchRequest`）
    - `src/workspace/git_ops.py`（删除随之失去调用方的 `GitOperations.checkout`、`fetch_and_checkout`、`fetch_and_checkout_async`、`is_valid_start_point`——环境切换永不 checkout）
    - `src/api/rest/workspace.py`（`remote-branches` 降级为 discover 视图并附环境标注，保留给 Phase 5 的 adopt/reconcile）
  - **frontend**
    - `src/stores/environment.ts`（31 行纯状态 → 完整注册表 store：列表/创建/切换/销毁；`currentEnvironment` **由工作副本路径派生**）
    - `src/stores/workspace.ts`（`open` 请求体 `branch` → `environment`；新增 `applyOpenedEnvironment`、`fetchOpenableEnvironments` 取代 `fetchRemoteBranches`）
    - `src/stores/git.ts`（删除 `checkout` / `checkoutRemoteBranch` / `deleteBranch`；`BranchInfo` 增加 `environment` / `managed`）
    - `src/components/layout/Toolbar.vue`（环境选择器改读注册表；创建走注册表编排并自动切入；销毁带二次确认与"当前环境不可销毁"拦截；展示类型与非 active 状态角标）
    - `src/components/layout/WorkspaceSelector.vue`（分支选择弹窗 → 环境选择弹窗）
    - `src/components/git/GitDetailPanel.vue`（移除创建/切换/删除分支入口；分支只读并显示所属环境标签）
    - `src/composables/useWorkspaceLifecycle.ts`、`src/i18n/locales/{en,zh-CN}.ts`
- 回归修复（Phase 1 五层布局改造遗漏 Gateway，本阶段一并修复）
  1. `WorkspaceIDFromPath` 仍按三层布局取 `segments[len-2]` 作为 workspace，在新布局下取到的是 `envs` → 所有 `ScopeSourceWorkspacePath` 规则解析失败：非 `ScopeOptional` 的写请求（plan/apply、files/write、git/push、knowledge、ontology）全部 403 `workspace_unresolved`。
  2. `assertWorkspacePathOwnership` 以路径末段等于 `user_id` 判断归属，新布局末段是环境名 → 所有带 `workspace_path` 的 validated 读端点（models、lineage、git/status、files/read…）全部 403 `workspace_path_foreign`。副作用是环境名恰好等于某 user_id 时会误判，新实现按第三段取 `user_id`，并有单测锁定。
- 交付：UI 中不再有裸分支创建/切换/删除入口；环境注册表 API 经网关端到端可用。
- 验收：仅通过环境操作即可完成"新建开发线 → 提交 → 推送"全流程。
- 遗留（后续 Phase）：`environments/{id}/{plan,apply,promote,diff,reconcile}` 端点与 `env:promote` 权限（Phase 5）；EnvironmentManager 完整 UI、DAG diff 着色、TTL reaper（Phase 6）。

### Phase 4：Dagster Branch Deployment（2 周）✅ 已实现

- 影响文件
  - **新增** `dagster-service/src/core/naming.py`（平台命名代数的 Dagster 侧镜像 + 工作副本路径解析；纯函数、无依赖，故不把注册表变成可用性依赖）
  - **新增** `dagster-service/src/core/deployment_manager.py`（deployment 身份 / 策略 / 生命周期 / run tag / diff，取代路径推断式 location 命名）
  - `dagster-service/src/core/code_location_manager.py`（降级为"机制层"：按 `CodeLocationSpec` 收外部下发的名字与 env；端口分配持久化到 `DAGSTER_HOME/code_location_ports.json`；注册 respawn 后的重挂钩子）
  - `dagster-service/src/core/process_manager.py`（后台 supervisor：定期巡检 + respawn + 滑动窗口重启预算，超限则 `abandoned` 上报；`on_restart` 钩子让 code location 自动重挂到 DagsterInstance）
  - `dagster-service/src/core/dagster_client.py`、`core/run_manager.py`（launch 支持 `executionMetadata.tags`）
  - `dagster-service/src/bridge/sqlmesh_resource.py`（environment 读 `DATACODE_ENVIRONMENT`，缺失即报错而非默认 prod）
  - `dagster-service/src/templates/defs/deployment.py.tpl`（**新增受管文件**：deployment 身份 + schedule/sensor gating）、`templates/defs/{schedules,sensors}/example_*.tpl`、`templates/{definitions.py,README.md}.tpl`、`core/scaffold.py`（受管文件集合新增 `defs/deployment.py`）
  - **新增** `dagster-service/src/api/rest/deployments.py`（ensure / reload / delete / list / diff），`api/rest/pipelines.py` 删除 code-location 端点并注入 run tag，`api/rest/{assets,schedules}.py` 改走 DeploymentManager
  - `dagster-service/src/api/models/{requests,responses}.py`、`src/api/deps.py`、`src/main.py`、`src/utils/errors.py`（`DEPLOYMENT_NOT_FOUND` / `DEPLOYMENT_INVALID`）、`src/config.py` + `conf/dagster-service.yaml`（supervisor 三项配置）
  - `sqlmesh-service/src/workspace/asset_dag_skeleton.py`（与模板同步：新增 `defs/deployment.py`，示例 schedule/sensor 改用门禁常量）
  - `gateway/internal/rbac/routemap.go`（deployments 三条显式规则：ensure/reload 必须能从 body 解析 workspace；DELETE 用 `env:delete` + scope 可选）、`internal/router/router.go`、`internal/handler/dagster_routes.go`（注释）
  - `frontend/src/types/dagster.ts`（`CodeLocationInfo` → `DeploymentInfo`）、`stores/dagster.ts`（`reloadDeployment` / `fetchDeployments`，并保存当前 deployment 供 Phase 6 展示）、`components/asset-dag/PipelineDagGraph.vue`
- 与原设计的两处偏差（均为实现约束，已在 §7.1 / §7.2 记录）
  1. gating 代码放在受管文件 `defs/deployment.py` 而非 `definitions.py`：后者 import `defs` 包，`defs` 下模块反向 import 会造成循环依赖。
  2. deployment 身份由 dagster-service 从 `workspace_path` 自行派生（镜像命名代数），注册表只下发**策略**（`env_type` / `schedules_enabled`）：名字是纯函数，没有理由为它引入一次跨服务调用；请求体里的 `environment` 只能确认不能改写。
- 交付：每个环境一个 deployment（名 `{workspace_id}__{env}`）；非生产环境 schedule/sensor 默认停用；run 带环境 tag；子进程异常自动 respawn；端口重启稳定。
- 验收：在 sandbox 环境创建 schedule 后，daemon 不触发（初始状态 STOPPED）；晋升到生产后其 deployment 以 `schedules_enabled` 加载即生效。
- 遗留（后续 Phase）：三段 diff 聚合推迟（§7.4）；DAG 上展示 deployment 态与 schedule 门禁提示归 Phase 6。（注册表侧调用 deployment 端点的 create/destroy 联动与 `missing/stale_deployment` 对账已在 Phase 5 收尾时落地。）

### Phase 5：代码合并 / Reconcile / RBAC（2 周）✅ 已实现（promote 编排改为拆分方案）

- 影响文件
  - **新增** `sqlmesh-service/src/environments/reconciler.py`（四方对账：注册表 / Git / SQLMesh state / Dagster deployment；分类见 §11.1）✅
  - **新增** `sqlmesh-service/src/environments/reaper.py`（sandbox TTL 回收：到期先告警、宽限后强制回收）✅
  - `sqlmesh-service/src/environments/registry.py`（`reconcile` 入口 + `release_archived_resources`：destroy 与后置补偿共用同一套释放序列）✅
  - `sqlmesh-service/src/environments/{sqlmesh_binder,git_binder}.py`（对账所需的诚实读取：`list_state_environments` 区分"不可读"与"空"；成员枚举与分支存在性）✅
  - `sqlmesh-service/src/api/rest/environments.py`（`POST /environments/{id}/reconcile`，`repair` 可选）✅
  - `gateway/internal/rbac/routemap.go`（reconcile 规则 → `workspace:env:create`，因为修复会建分支与虚拟环境）✅
  - `sqlmesh-service/src/workspace/git_ops.py`（新增 `GitMergeOperations`：`resolve_ref` / `diff_files` / `probe_merge` / `merge_in_bare` / `push_commit` / `merge_in_worktree` / `abort_merge`）✅
  - **新增** `sqlmesh-service/src/workspace/git_merge.py`（两个合并原语的编排：`preview_merge` / `promote_merge` / `sync_with_base`）✅
  - `sqlmesh-service/src/api/rest/git.py`（`GET /branch-diff`、`POST /merge`、`POST /sync` + `merge` 审计事件 + 目标为生产时的 `env:promote` 二次校验）✅
  - `sqlmesh-service/src/utils/errors.py`（新增 `GIT_MERGE_CONFLICT`，冲突清单内联进 409 响应体）✅
  - `gateway/internal/rbac/{catalog,routemap}.go`（`workspace:env:promote` 权限点 + merge/sync 路由规则）✅
  - `frontend/src/{stores/git.ts,components/git/GitDetailPanel.vue,components/plan/AssetDagDeployPanel.vue}`（合并对话框、同步入口、Pull & Reload）✅
  - **新增** `sqlmesh-service/tests/test_git_merge_ops.py`（真实 git + 真实 remote：干净晋升不动任何工作副本、冲突预检零写入、sync 冲突留在工作区、三点 diff 语义、ref 优先远端）✅
  - **新增** `sqlmesh-service/src/environments/deployment_binder.py`（注册表 → dagster-service 的 HTTP 绑定：`list_deployments` 以 `None` 区分"不可达"与"空"，`ensure` / `destroy` 失败只上报不抛）✅
  - `sqlmesh-service/src/{config.py}` + `conf/sqlmesh-service.yaml`（`dagster_service_url` 取自 platform 服务拓扑、`dagster.request_timeout` 因 ensure 会拉起子进程而放宽）✅
  - `sqlmesh-service/src/environments/registry.py`（create 末尾 best-effort `ensure_deployment`（失败不置 `drifted`，deployment 是零数据派生态）；`_release_all_resources` 末尾追加 deployment 释放；`ReleasedResources.deployment_destroyed` 贯穿 destroy 结果/事件/日志；`reconcile` 外层锁仅对 live 环境持有）✅
  - **新增** `sqlmesh-service/tests/test_deployment_binder.py`（`httpx.MockTransport`：不可达/非 2xx/畸形体一律 `None`，ensure 策略入参与 destroy 的 URL 编码）✅
  - `sqlmesh-service/src/workspace/git_ops.py` + `src/api/rest/git.py` + `frontend/src/{stores/git.ts,components/git/GitDetailPanel.vue,components/plan/AssetDagDeployPanel.vue}`（`pull` 由透传 `git pull` 改为平台自决的 fetch + ff-only/merge 集成，返回结构化 `PullResult`；修复分叉环境分支必然失败于 `Need to specify how to reconcile divergent branches`，见 §6.4.3）✅
  - **新增** `sqlmesh-service/tests/test_git_pull_ops.py`（真实 git + 真实 remote：已最新/本地领先不写入、仅落后走快进无 merge commit、分叉造双亲 merge、冲突留在工作区且为成功、脏工作副本零写入阻塞、未跟踪文件不阻塞、未解决的上次合并被单独识别、非法 ref 拒绝）✅
- 已交付：四方对账与自动修复、TTL 回收、`drifted → active` 状态收敛、destroy 的多成员释放与后置补偿（含 deployment）、**两原语代码合并 + `env:promote` 权限点**。
- 验收：人为删除分支或虚拟环境后 reconcile 可重建；重启 dagster-service 后 reconcile 可重建各环境 deployment 并恢复其 schedule 策略；archived 环境残留资源（含 deployment）可补偿释放；`env/dev` 的提交可经"合并 → 目标环境 Pull & Reload → apply"完成一次晋升闭环，且冲突时得到冲突文件清单并可经 sync 解决后重试。

### Phase 6：前端体验与回收治理（1.5 周）

- 影响文件
  - **新增** `frontend/src/components/environment/{EnvironmentManager.vue,EnvironmentDiffPanel.vue,CreateEnvironmentDialog.vue}`
  - `frontend/src/stores/environment.ts`（完整化）、`plan.ts`、`dagster.ts`、`models.ts`（环境联动）
  - `frontend/src/components/layout/{Toolbar.vue,WorkspaceSelector.vue}`
  - `frontend/src/components/asset-dag/{ScheduleManager.vue,PipelineDagGraph.vue}`（deployment 态展示）
  - `frontend/src/locales/*`（中英文文案）
  - **新增** sandbox TTL reaper 定时任务
- 交付：环境管理完整 UI、DAG diff 着色、TTL 自动回收。
- 验收：新用户可在不接触 Git 概念的前提下完成一次完整的开发—验证—晋升闭环。

### Phase 7（可选）：生产形态（K8s）

- DagsterInstance StatefulSet + PostgreSQL；dagster-service 无状态化；Code Location Worker Pool + 动态 `workspace.yaml` resolver；`K8sRunLauncher`；dev/prod namespace 隔离与 CI 晋升流水线。

### 里程碑总览

| Phase | 周期 | 关键风险 | 可独立交付 | 状态 |
|-------|------|---------|-----------|------|
| 0 设计冻结 | 0.5w | 命名规则返工 | — | ✅ |
| 1 布局 + 注册表 | 1.5w | worktree 与现有路径校验冲突 | 是 | ✅（Gateway 侧路径解析遗漏，已在 Phase 3 补齐） |
| 2 State 归一 | 1w | 共享 state 并发写 | 是 | ✅ |
| 3 Git 收敛 | 0.5w | 用户习惯变化 | 是 | ✅（含 RBAC env 权限点前置） |
| 4 Branch Deployment | 2w | schedule gating 覆盖不全 | 是 | ✅（gating 落在受管文件 `defs/deployment.py`；护栏边界见 §7.2） |
| 5 代码合并/Reconcile | 2w | merge 冲突与 apply 原子性 | 是 | ✅ 晋升改为两个用户动作（§6.4），冲突不再需要原子性；Dagster 侧对账待开始 |
| 6 前端与回收 | 1.5w | 交互复杂度 | 是 | 🟡 TTL 回收、合并/同步 UI、Pull & Reload 已实现；EnvironmentManager 完整 UI 与 DAG diff 着色待开始 |
| 7 生产形态 | 按需 | 运维成本 | 是 | 可选 |

合计约 9 周（不含 Phase 7）。

---

## 13. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| **共享 state schema 并发写** | 多成员同时 apply 造成 state 冲突 | 环境级串行锁 + 现有 `state_fingerprint` 乐观校验；失败返回明确的"环境正忙" |
| **Worktree 与 `.git` 损坏** | 用户工作副本不可用 | worktree 是可丢弃资源：提供"重建环境副本"操作（remove + re-add），未提交改动前置提醒 |
| **代码已合并、数据未晋升** | 目标分支领先于其数据 | 晋升拆成两个用户动作后，这是显式的中间态而非故障：reconciler 的 `commit_drift` 上报它，部署面板在 `behind > 0` 时提示"远端有未拉取的提交"（§6.4.2） |
| **合并冲突** | 无法自动完成晋升 | `merge-tree` 预检在写入任何东西之前拒绝，并返回冲突文件清单；解决路径固定为"源环境 sync → 编辑器解决 → 提交 → 重试合并"，因此不需要 Web 三方合并编辑器 |
| **晋升期间目标分支被他人推进** | 合并基准过期 | 合并前强制 `fetch`、ref 解析优先远端、push 非强推；被拒即要求基于新 tip 重算 |
| **环境数量膨胀** | state / schema / deployment 资源耗尽 | 每 workspace 环境数配额；sandbox 强制 TTL；idle deployment 卸载 |
| **schedule 误触发生产** | 数据污染 | 模板级 gating（managed file 强制覆盖）+ 开启需 promote 权限 + run tag 审计 |
| **环境名规则过严** | 用户不适应（无 `-`、无 `/`） | 前端创建向导自动规范化并实时预览分支名与 schema 名 |
| **Dagster 开源版无原生 deployment 隔离** | run 历史混在一起 | location 命名空间 + run tag + UI 过滤；生产可升级 K8s per-run Job 强隔离 |
| **平台外分支被忽视** | 用户已有仓库分支无法使用 | adopt 流程；`env_of` 返回 None 的分支只读可见 |

---

## 14. 与既有设计文档的关系

| 文档 | 需同步更新的部分 |
|------|-----------------|
| `domain-model.md` | 新增 `Environment` 实体及其与 `Workspace`、`Member`、`Deployment` 的关系；删除 `Workspace.git_branch` |
| `backend-architecture.md` | sqlmesh-service 职责新增 Environment Registry / Reconciler；dagster-service 新增 Deployment API |
| `rbac-permission-design.md` | 新增 4 个 `workspace:env:*` 权限点与角色模板变更；`workspace:env:promote` 把守"合并进生产分支" |
| `dataops-platform-design.md` | "环境管理"一节由"dev/staging/prod 多环境"更新为"Environment = Branch + SQLMesh Env + Dagster Deployment" |

---

## 15. 开放问题

1. **生产环境是否允许直接 apply**：允许，且这是设计的一部分——晋升拆分后（§6.4），对生产 apply 就是"操作二"。代码进入生产的关口在**操作一的 merge**（`workspace:env:promote`），而不是在 apply 上再加一道。
2. **Sandbox 环境是否需要独立 state**：当前设计所有环境共享 workspace 级 state。若个人实验频繁污染环境列表，可考虑 sandbox 使用 `sqlmesh__{workspace_id}__sandbox` 副 state，但会失去与 shared 环境之间的 virtual promote 能力。
3. **多人共享同一环境的 worktree**：当前每用户各持一份 worktree（同一分支被多人各自 checkout）。分支冲突交给 Git 常规流程，是否需要平台层"环境编辑锁"？
4. **Ontology 服务的环境感知**：语义层（entities/relationships/metrics）当前是否随环境变化？若是，需纳入 diff 范围（其代码若在 Git 内则已随合并一起晋升）。
5. **Agent 的环境上下文**：Agent 目前已上报 `environment`，但是否允许 Agent 自主创建/销毁环境（`trigger_type=agent`）？建议默认只允许在 sandbox 环境内 apply，创建与合并进生产保留人工。
