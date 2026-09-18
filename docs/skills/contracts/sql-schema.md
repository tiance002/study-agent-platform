# L1 契约 · sql-schema

> `skill_id: sql-schema` | version: 0 | expiry: 2027-03-01 | owner: 待指派
> 权威来源：`specs/2026-09-17-04-multitenancy-and-data-governance.md`、`03-policy-tools-and-execution.md`、`01-learning-loop-and-mastery-evidence.md`
> **生成区已启用**：由 `tools/skills/gen_contracts.py` 从代码导出，并参与 CI 一致性校验。
> ⚠️ 当前导出的是**代码层实体**，不是 PostgreSQL 表结构；迁移（alembic）落地后应改由迁移导出 DDL。

## 1. 生成区

<!-- BEGIN GENERATED: source=AST 扫描 backend/app 实体, source_hash=sha256:ff957194503239d863df087460998ac5ee7ff7361ff13edee0638e76198996f4, generated_at=2026-09-18T16:55:58Z -->
> 生成时间：2026-09-18T16:55:58Z

> 由 `tools/skills/gen_contracts.py` 从代码扫描导出，请勿手工编辑本区。
> **注意**：这是**代码层实体**，不是 PostgreSQL 表。迁移落地后应改由 alembic 导出 DDL。

| 实体 | 定义模块 | 字段数 | 含 tenant_id | 含 learning_project_id |
|---|---|---:|---|---|
| `AuditRecord` | `app.audit.sink` | 9 | 是 | — |
| `Account` | `app.budget.ledger` | 8 | 是 | — |
| `Reservation` | `app.budget.ledger` | 7 | — | — |
| `ArtifactRef` | `app.core.artifacts` | 5 | — | — |
| `PlatformError` | `app.core.errors` | 5 | — | — |
| `EvidenceAssessment` | `app.core.evidence_issues` | 3 | — | — |
| `EvidenceIssue` | `app.core.evidence_issues` | 6 | — | — |
| `ChildEnvelope` | `app.execution.child_run` | 6 | — | — |
| `Claim` | `app.execution.child_run` | 3 | — | — |
| `SpawnedChild` | `app.execution.child_run` | 4 | — | — |
| `BudgetCeiling` | `app.execution.confirmation` | 3 | — | — |
| `ConfirmationRecord` | `app.execution.confirmation` | 10 | 是 | — |
| `ConfirmationStore` | `app.execution.confirmation` | 2 | — | — |
| `ToolOutcome` | `app.execution.outbox` | 7 | — | — |
| `LogicalAction` | `app.execution.state_machine` | 9 | 是 | — |
| `MembershipStore` | `app.identity.membership` | 3 | — | — |
| `ProjectRecord` | `app.identity.membership` | 3 | 是 | — |
| `Principal` | `app.identity.models` | 4 | 是 | — |
| `SessionToken` | `app.identity.session` | 8 | 是 | — |
| `FetchFailure` | `app.knowledge.evidence_state` | 3 | — | — |
| `RetrievalSignals` | `app.knowledge.evidence_state` | 9 | — | — |
| `Chunk` | `app.knowledge.retrieval` | 9 | 是 | 是 |
| `ScoredChunk` | `app.knowledge.retrieval` | 2 | — | — |
| `ComponentVerdict` | `app.learning.evidence` | 7 | — | — |
| `EvidenceCorrection` | `app.learning.evidence` | 6 | — | — |
| `EvidenceEvent` | `app.learning.evidence` | 11 | 是 | — |
| `ComponentMastery` | `app.learning.projector` | 7 | — | — |
| `MasteryProjection` | `app.learning.projector` | 6 | — | — |
| `PlatformState` | `app.main` | 15 | — | — |
| `ExecutorCapabilities` | `app.policy.gateway` | 1 | — | — |
| `PolicyDecision` | `app.policy.gateway` | 7 | — | — |
| `PolicyInput` | `app.policy.gateway` | 16 | 是 | — |
| `Endorsement` | `app.policy.taint` | 10 | — | — |
| `TaintedValue` | `app.policy.taint` | 4 | — | — |
| `CapabilityToken` | `app.policy.token` | 15 | 是 | — |
| `TokenIssuer` | `app.policy.token` | 1 | — | — |
| `NodeSpec` | `app.registry.models` | 16 | — | — |
| `ToolSpec` | `app.registry.models` | 15 | — | — |
| `TenantContext` | `app.tenancy.context` | 3 | 是 | — |
| `NodeContext` | `app.workflow.context` | 8 | 是 | — |
| `InteractionRequest` | `app.workflow.runtime` | 9 | 是 | 是 |
| `InteractionResult` | `app.workflow.runtime` | 10 | — | — |

合计 42 个实体。
<!-- END GENERATED -->

## 2. 手写区 · 不可协商的数据库约束

以下内容**必须在数据库层强制**，不得只靠应用代码自觉：

| 约束 | 具体要求 | 违反后果 |
|---|---|---|
| 应用角色 | 使用非表所有者、无 `BYPASSRLS` 的独立数据库角色；迁移使用另一个角色 | 应用可绕过 RLS |
| 租户上下文 | 只通过事务内 `SET LOCAL` 设置；连接池归还前结束事务；后台任务同样建立租户事务上下文 | 上下文泄漏到下一个请求 |
| 缺上下文必失败 | 查询缺少租户/项目上下文必须报错，**不得回落为无过滤查询** | 越权读取（不变量 #2） |
| 项目级实体 | 一律包含 `tenant_id` 与 `learning_project_id` | 无法隔离 |
| 约束列 | 外键、唯一约束、索引包含适当的租户/项目列，不能只依赖应用过滤 | ID 猜测即越权 |
| 证据不可变 | `EvidenceEvent`、`EvidenceCorrection` 仅允许 INSERT；应用角色无 UPDATE/DELETE 权限 | 事实可被篡改（不变量 #14 的基础） |
| 掌握只读 | `MasteryProjection` 只有 Projector Worker 有写权限 | API 直写掌握状态 |
| 事件顺序 | 排序以服务端 `event_seq` 为准；客户端时间不得决定事件顺序 | 投影顺序错乱 |
| 队列内容 | 队列表只存 task id，任务内容与状态在业务表 | 状态分裂 |
| 认领方式 | worker 使用 `FOR UPDATE SKIP LOCKED` 原子认领 | 重复执行 |

## 3. 手写区 · 主要实体归属

| 实体 | 归属 | 备注 |
|---|---|---|
| User、TenantMembership | 租户级 | — |
| LearningProject、Session、Message | 项目级 | 会话只是交互载体，长期状态归项目 |
| Source | **租户级** + ACL | 项目通过 `ProjectSourceGrant` 借用；删项目不删源 |
| ProjectKnowledgeIndex | 项目级 | 由 Source 派生 |
| PlatformContentRelease | 租户级（平台发布） | 不可变；项目显式 pin release |
| CompetencyComponent、Plan | 项目级 | 图谱带 `graph_version` |
| EvidenceEvent、EvidenceCorrection、MasteryProjection、MasteryPresentedEvent | 项目级 | 前三者写入权受限 |
| WorkflowRun、NodeRun、ActionIntent、ToolOutcome | 项目级 | 执行链路 |
| BudgetAccount、BudgetReservation | Tenant → User/Project → Run → Node | 树形 ownership |
| CapabilityGrant、PolicyDecision | 项目级 | 授权与决策记录 |
| AuditIndex | 项目级 | 只存索引与引用，正文在独立 sink |
| LearningProject 之外的跨项目能力引用 | **用户级** | 不得使项目查询自动扩大范围 |

## 4. 生成脚本约定（已实现）

| 项 | 约定 |
|---|---|
| 脚本 | `tools/skills/gen_contracts.py --target sql-schema` |
| 当前输入 | `backend/app/**` 的 dataclass 定义（AST 扫描，不 import，无副作用） |
| **目标输入** | `alembic/versions/**` —— 迁移落地后**必须替换**，届时导出真正的 DDL |
| 输出 | 本文件第 1 节生成区（实体、模块、字段数、是否含租户/项目列） |
| 排序 | 按模块名、类名排序，保证导出稳定可比对 |
| `source_hash` | 输入文件按路径排序后内容的 SHA-256 |
| CI | `--check` 重新渲染并与文件比对（不只看哈希，避免手改内容绕过） |
| 禁止 | 生成区出现人工编辑内容 |

## 5. 手写区 · 常见坑

- **外键只写 `id` 而不带租户列**：跨租户引用在数据库层就拦不住，必须复合外键。
- **迁移使用应用角色**：迁移角色必须独立，否则 RLS 会在迁移时被绕过，问题在测试环境看不出来。
- **把 `SET LOCAL` 写在连接级**：必须是事务级，连接复用时否则会串租户。
- **给 `EvidenceEvent` 加"便于修正"的 UPDATE 路径**：修正只能追加 `EvidenceCorrection`，永不改写事实。
- **在 vector 检索里做应用层过滤**：过滤必须在数据库内执行，禁止把跨租户候选拉到应用层再筛。
