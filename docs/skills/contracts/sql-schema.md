# L1 契约 · sql-schema

> `skill_id: sql-schema` | version: 0 | expiry: 2027-03-01 | owner: 待指派
> 权威来源：`specs/2026-09-17-04-multitenancy-and-data-governance.md`、`03-policy-tools-and-execution.md`、`01-learning-loop-and-mastery-evidence.md`
> **本文件是设计期版本：生成脚本尚不存在，全文属手写区，生成区为占位。** 实施时按第 4 节约定填充。

## 1. 生成区

<!-- BEGIN GENERATED: source=alembic+models, source_hash=PENDING, generated_at=PENDING -->
> 待实施后由 `tools/skills/gen_sql_schema.py` 导出，内容为：全部表、列、类型、约束、索引、RLS 策略、迁移链摘要。
> 生成脚本未就绪前，本区不填任何手工内容。CI 校验：导出结果与本区不一致即构建失败。
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

## 4. 待填充的生成脚本约定

| 项 | 约定 |
|---|---|
| 脚本路径 | `tools/skills/gen_sql_schema.py` |
| 输入 | `alembic/versions/**`、`backend/app/**/models.py` |
| 输出 | 本文件第 1 节替换内容（表/列/类型/约束/索引/RLS/迁移链） |
| 排序 | 表名升序、列按定义顺序，保证导出稳定可比对 |
| `source_hash` | 全部输入文件内容按路径排序后的 SHA-256 |
| CI | `--check` 模式：导出结果与本区不一致则退出码非零 |
| 禁止 | 生成区出现任何人工编辑内容；CI 检测到即失败 |

## 5. 手写区 · 常见坑

- **外键只写 `id` 而不带租户列**：跨租户引用在数据库层就拦不住，必须复合外键。
- **迁移使用应用角色**：迁移角色必须独立，否则 RLS 会在迁移时被绕过，问题在测试环境看不出来。
- **把 `SET LOCAL` 写在连接级**：必须是事务级，连接复用时否则会串租户。
- **给 `EvidenceEvent` 加"便于修正"的 UPDATE 路径**：修正只能追加 `EvidenceCorrection`，永不改写事实。
- **在 vector 检索里做应用层过滤**：过滤必须在数据库内执行，禁止把跨租户候选拉到应用层再筛。
