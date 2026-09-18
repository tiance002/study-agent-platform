# Agent 工程学习规划平台实施计划

> **For agentic workers:** 实施时按任务逐项执行；每项任务必须先写失败测试，再实现最小行为，并在进入下一项前通过该任务的测试与退出门。

**目标：** 将已冻结的 Agent 工程学习规划平台设计实现为可验证的多租户 SaaS 首版，并按安全边界逐级开放能力。

**架构：** 采用 React + TypeScript 前端、Python + FastAPI 模块化单体控制面、PostgreSQL/pgvector 事实源、独立异步 worker、独立沙箱执行面和独立审计 sink。首版默认只开放明确的 A0/A1 能力；A2/A3 必须在 capability token、确认 UI、幂等执行和对账全部通过后逐工具解锁。

**技术栈：** React、TypeScript、Python、FastAPI、Pydantic、PostgreSQL、pgvector、S3 兼容对象存储、两个独立 Redis/Valkey 部署、OpenTelemetry、Linux 隔离沙箱、KMS/Secret Manager。

## 全局约束

- `EvidenceEvent` 是掌握事实源；任何 API、模型或界面不得直接写掌握状态。
- 投影算法不得使用随机、当前时间、实时 ACL、外部模型或未进入 `projection_input_set_hash` 的配置。
- Policy Gateway 是无模型、确定性、版本化且特权路径 fail-closed 的授权边界。
- capability token 只允许在当前授权域内收缩；扩权必须重新策略评估，高风险扩权需要用户确认。
- 用户数据默认敏感；所有项目级实体包含 `tenant_id` 和 `learning_project_id`。
- 外部内容与模型派生值默认带 taint；只能通过 sink-specific、时限受控的 endorsement 使用。
- Redis/Valkey 只做加速；正确性必须回落到 PostgreSQL 和独立审计事实。
- 所有外部副作用采用 intent → 幂等 dispatch → outcome → reconciliation。
- 禁止静默降级；故障动作遵守系统不变量规格中的 fail-closed/fail-open 矩阵。

## 文件与模块边界

实现仓库建立以下边界，模块只通过显式接口和版本化 schema 交互：

- `backend/app/identity/`：身份、租户成员、项目上下文和 RLS 事务装配。
- `backend/app/learning/`：能力图谱、assessment contract、EvidenceEvent、correction 和 projector。
- `backend/app/knowledge/`：Source、ACL、摄取、切片、中文分词、Project Knowledge Index 和引用。
- `backend/app/workflow/`：typed node、run 状态、预算树、reservation 和恢复。
- `backend/app/policy/`：Policy Gateway、PolicyDecision、capability token、taint endorsement。
- `backend/app/execution/`：ActionIntent、幂等工具适配器、outcome、对账和确认状态。
- `backend/app/audit/`：独立 sink 写入、签名批次、索引引用和恢复校验。
- `backend/app/api/`：只读投影 API、命令 API、SSE 事件和错误码。
- `frontend/src/`：项目、会话、计划、知识库、assessment、证据、确认和 support 流程。
- `tests/`：按模块保存单元、契约、隔离、性质、故障注入和统计测试。

## 任务拆分

### 任务 1：工程骨架与模块门禁

**状态：** 部分完成 —— 目录结构、import 方向门（含禁止 L4 横向互调）、CI 的测试与密钥检查已就绪；schema 兼容门、依赖许可证扫描、分模块 schema 版本未实现。

**文件：** 创建 `backend/app/`、`frontend/`、`tests/`、`alembic/`、`infra/`；修改 CI 配置加入 import 方向、schema 兼容和 lint/type 门禁。

**步骤：**

- [ ] 建立 FastAPI、React、迁移和测试最小启动目标。
- [ ] 为每个模块定义公开接口包和 schema 版本；禁止跨模块访问内部 ORM。
- [ ] 写 import 方向测试，阻止 `knowledge` 直接依赖 `learning.projector`，阻止 `model_router` 绕过 `policy`。
- [ ] CI 运行单元测试、类型检查、迁移校验、依赖许可证扫描和秘密扫描。

**退出门：** 空数据库可完成健康检查；CI 能机械拒绝反向依赖；每个受控资产都有 owner、版本、回滚字段。

### 任务 2：租户隔离、密钥和数据治理底座

**状态：** 部分完成 —— 租户/项目上下文强制（应用层）与跨租户拒绝测试已实现；PostgreSQL + RLS、KMS/Secret Manager、数据谱系与 DSR 字段级擦除未实现。

**文件：** `backend/app/identity/`、`backend/app/privacy/`、`backend/app/crypto/`、`alembic/versions/`、`tests/isolation/`、`infra/kms/`。

**步骤：**

- [ ] 创建 Tenant、User、TenantMembership、LearningProject 和项目级实体的迁移；应用角色使用非所有者、无 `BYPASSRLS` 的数据库账号。
- [ ] 用事务内 `SET LOCAL` 注入租户和项目上下文，加入跨租户/跨项目拒绝测试。
- [ ] 接入数据、审计签名、token 签名三类密钥；所有密文保存 `key_version`，撤销广播清除 DEK 缓存。
- [ ] 实现数据谱系、生命周期策略版本、DSR 删除计划、字段级擦除和删除报告。

**退出门：** 租户 A 无法读写租户 B；KMS 故障对新写入 fail-closed；恢复测试能证明删除后的投影仍可重建。

### 任务 3：证据、assessment 和掌握投影

**状态：** 大部分完成 —— 证据模型与组件级四维裁决、`projection_input_set_hash`、顺序无关回放、Projector 唯一写入者、correction 链、confidence 四档均已实现；graph migration dry-run 与 `MasteryPresentedEvent` 未实现。数据库层的禁 UPDATE/DELETE 约束依赖 PostgreSQL，当前由接口层面保证。

**文件：** `backend/app/learning/contracts.py`、`evidence.py`、`corrections.py`、`projector.py`、`graph_migration.py`、`tests/learning/`。

**步骤：**

- [ ] 实现不可变 `AssessmentContract`、`CompetencyTaskMapping`、`EvidenceEvent`、`EvidenceCorrection` 和组件级 contribution。
- [ ] 实现 `MasteryProjection` 的唯一写入 worker；API、模型和人工界面仅能发出事实/裁决命令。
- [ ] 实现 `projection_input_set_hash`、事件顺序无关回放、同事件幂等、correction 链和 graph migration dry-run。
- [ ] 实现 `valid / inconclusive / voided / attribution_pending / indeterminate` 筛选与复测 SLA；confidence 仅输出四档。
- [ ] 将 `MasteryPresentedEvent` 作为非掌握事实单独记录，不把展示记录写入掌握投影。

**退出门：** 数据库层禁止 Evidence/Correction UPDATE/DELETE；全量规范回放与增量投影一致；迁移可回滚；非 assessment 产品任务不能生成 LearningEvidence。

### 任务 4：知识摄取、taint 和检索质量

**状态：** 少量完成 —— 片段级 taint、sink-specific endorsement、检索的租户/项目内过滤、精确回读的作用域强制已实现；Fetcher 与 Package Proxy（含 SSRF 防护）、中文分词、pgvector 混合检索、索引与词典版本化未实现。

**文件：** `backend/app/knowledge/`、`backend/app/egress/fetcher.py`、`backend/app/egress/package_proxy.py`、`tests/knowledge/`、`tests/security/test_egress.py`。

**步骤：**

- [ ] 实现统一 `DocumentProcessor`、Source、ACL、ProjectSourceGrant、PlatformContentRelease 和索引 provenance；标题路径、父子章节、原文 span 与切块版本必须可回溯。
- [ ] Fetcher 固定 DNS 解析结果并在连接层拒绝私网/元数据 IP；限制重定向、响应大小、解压后大小、文件数、路径穿越和符号链接。
- [ ] 沙箱默认无网络；依赖只经白名单包代理，禁用 lifecycle scripts，结果只能经 broker 回传。
- [ ] 实现片段级 `TaintSource[]`、独立 `acquisition_method`、`derived_from[]`、source span、参数级 lineage 和 sink-specific endorsement；模型派生显式增加 `MODEL_OUTPUT`，ACL 收窄时重算索引可见性并失效缓存。
- [ ] 先用标题路径 + 外部分词 + `tsvector`；标题/章节粗召回不得成为全局片段召回的必经门。建立中文查询分层基准和 BM25-like/RRF/向量融合评测，索引与词典版本化并支持双写回填。
- [x] 将 `unresolved[]` 升级为 knowledge 域闭集 `EvidenceIssueCode` 驱动的 `issues[]`，按相关性、核心结论覆盖、冲突、新鲜度和必需步骤完成度计算四级证据状态。
      **部分完成（2026-09-19）**：闭集、`EvidenceIssue` 模型、四级状态与确定性判定器已落地
      （`core/evidence_issues.py` + `knowledge/evidence_state.py`）；ChildRun 信封与检索 node
      均已切换到 `issues[]`，旧的 `unresolved` 字段与 `bool(hits)` 判定已移除。
      已产生 6 个码：`NO_CANDIDATES`、`LOW_RELEVANCE`、`SOURCE_FETCH_FAILED`、`SCOPE_BLOCKED`、
      `TOOL_RESULT_UNKNOWN`、`MISSING_SUPPORT`（后者当前由"必需步骤未完成"触发）。
      **`SOURCE_CONFLICT` 与 `FRESHNESS_UNKNOWN` 尚未产生** —— 判定它们需要冻结的核心结论标注集，
      输入当前不存在。不假装实现：一个永不触发的码比缺失的码更糟，因为它看起来已经做好。
- [ ] 冻结查询—文档—片段—核心结论标注集；只有分层指标证明无关键回归后才启用 reranker。

**退出门：** SSRF、DNS rebinding、解压炸弹、路径逃逸和依赖外传测试均拒绝；检索结果在数据库层强制项目过滤；索引可由版本化源重新构建；标题召回失败时全局片段兜底仍可命中；reranker 未通过冻结评测集不得上线；证据问题可按稳定 code 统计且不泄露未授权资源存在性。

### 任务 5：工作流、预算、策略和外部执行

**状态：** 大部分完成 —— typed node registry、capability token（只减不增）、撤销 epoch、树形预算与原子预留、额度回收、intent→dispatch→outcome→对账、独立审计 sink（哈希链、无删除接口、fail-closed）均已实现，并有失败注入测试。确认 UI、持久化 outbox、durable task table 未实现（当前为内存适配器）。

**文件：** `backend/app/workflow/`、`backend/app/policy/`、`backend/app/execution/`、`backend/app/audit/`、`tests/policy/`、`tests/execution/`。

**步骤：**

- [ ] 实现 typed node registry、节点实例 ID、capability token、PolicyDecision 快照和撤销 epoch/facts version。
- [ ] 为每个工具填写闭合参数 schema；在 Policy Gateway 和预算之前完成 JSON 信封、node 工具白名单、参数 schema 与字段语义校验。
- [ ] 实现树形 BudgetAccount 与原子 reservation；昂贵工具事前扣费，廉价工具按规则结算，父节点保留 completion reserve。
- [ ] 分别实现 `max_generation_repairs`、`max_tool_calls`、`max_execution_attempts`；格式修正不占工具调用但必须消耗 token/step/货币预算。
- [ ] 实现 ActionIntent → 幂等 dispatch → ToolOutcome；幂等键由客户端 request id、run、node instance 和 tool 规范化生成，不含 attempt/时间/随机 UUID。
- [ ] 实现 `unknown` 对账、补偿语义标记、确认 UI 所需的确切参数/资源/可撤销性展示和确认疲劳限流。
- [ ] 实现版本化 `ToolCallFallbackStrategy`；修正耗尽或无合法工具时显式澄清、无工具回答、暂停或终止，并记录策略版本与未完成事项。
- [ ] 审计正文写独立对象锁/WORM sink；主库只保存索引引用，sink 不可用时高影响动作拒绝。

**退出门：** 畸形 JSON、未知工具、额外字段、越界参数、未授权工具、未注册节点和预算耗尽均在副作用前拒绝；格式修正最多两次且与工具/执行次数分别记账；已派发写操作不能由模型盲目重试；重复投递不重复执行；降级结果可审计且不伪装成功；审计 sink 故障矩阵测试通过。

### 任务 6：模型路由、验证器与教学工作流

**状态：** 少量完成 —— L/A/D 轴与 obligation 支持性校验、确定性契约验证已实现；
证据充分性判定已于 2026-09-19 按 02 号规格 §4 改造为**结构化**（四级状态 + 闭集 `issues[]`，
取代原先的 `"supported" if hits else "insufficient"`），但「核心结论覆盖率 / 来源冲突 / 新鲜度」
三项依赖冻结标注集，对应码尚未产生；prompt/model/retrieval 组合 registry、L1 准入校准、
risk-coverage、canary、assessment 全流程与近/远迁移评测未实现。

**文件：** `backend/app/routing/`、`backend/app/validators/`、`backend/app/prompts/`、`backend/app/teaching/`、`tests/routing/`、`tests/evaluation/`。

**步骤：**

- [ ] 实现 L0/L1/L2 与 A0-A3、D0-D3 三轴策略矩阵；默认仅开放明确 A0/A1 子类。
- [ ] 建立 prompt/model/retrieval 组合 registry；每个组合保存评测集、validator、canary、owner 和回滚版本。
- [ ] 实现确定性契约验证、证据充分性、任务级归因和有限重规划；行为信号只作下界，不能据此证明准确率。
- [ ] 实现 Guided/Assisted/Independent assessment、诊断退出条件、失败响应和近迁移/远迁移/新情境任务。

**退出门：** 冻结数据集上风险上置信界满足 node 的 epsilon 后才优化 coverage；模型升级触发阈值重标定；不输出未经统计门槛支持的概率数值。

### 任务 7：前端学习与安全体验

**状态：** 未开始（仅有单页演示）—— 演示页覆盖摄取、交互、注册表、掌握、审计、预算六个入口，用于人工观察边界行为；React 应用、确认弹窗、证据解释、无障碍（WCAG 2.2 AA）与端到端测试均未实现。

**文件：** `frontend/src/features/projects/`、`sessions/`、`assessments/`、`evidence/`、`confirmations/`、`support/`、`tests/e2e/`。

**步骤：**

- [ ] 实现项目/会话/计划/知识库主流程，所有长期状态按项目展示。
- [ ] 实现 assessment contract 展示、帮助记录、证据解释、confidence 四档、迁移观察和申诉入口。
- [ ] 实现高影响确认弹窗：显示确切参数、影响资源、是否可撤销、剩余预算和审计状态。
- [ ] 实现 pending/indeterminate、失败归因可见性、复测选择和 support break-glass 用户提示。
- [ ] 按 WCAG 2.2 AA 和低带宽/低端设备目标完成键盘、读屏、焦点、字幕和时限延长测试。

**退出门：** 用户无法通过 UI 绕过权限、确认、项目边界或审计；高影响动作的拒绝路径能在端到端测试中证明没有副作用。

### 任务 8：可观测性、容量、故障演练和发布

**状态：** 未开始 —— OTel 链路、容量压测、故障注入演练、canary 与自动回滚、ADR 复审记录均未实现。CI 目前有三道机械门（测试、manifest、契约一致性）与基础密钥检查，尚不含类型检查与依赖许可证扫描。

**文件：** `backend/app/observability/`、`infra/load/`、`infra/chaos/`、`runbooks/`、`tests/release/`、`docs/adr/`。

**步骤：**

- [ ] 为 tenant/project/run/node、policy/validator/prompt/model/dataset 版本建立 trace、指标和审计关联。
- [ ] 以目标数据规模、会话长度、上下文增长/压缩、检索类型、沙箱比例和 think time 构造 500 峰值活跃用户压测。
- [ ] 注入 KMS 超时、数据库切换、Redis 拒绝写、模型 429、审计 sink 变慢、对象存储不可用和沙箱未知状态，逐项核对失败矩阵。
- [ ] 运行备份恢复、预算未结 reservation、in-flight action、taint lineage、孤儿沙箱和 tombstone 对账演练。
- [ ] 对 policy/node/validator/prompt/model/retrieval/index 执行 canary、序贯阈值和自动回滚；完成 ADR 版本、许可证、owner 和到期复审记录。

**退出门：** 首发规模与目标规模的容量假设有压测证据；所有故障注入动作符合矩阵；回滚演练能在规定时限内恢复；批次一门、批次二门均由 CI/演练报告机械判定。

### 任务 9：技能契约、子任务运行时与工具边界

**状态：** 已实现 —— manifest 校验（结构、依赖方向、体积预算、到期复审）、三份契约的生成与 `--check` 内容级一致性门、工具声明五字段与重叠门、单 node 工具数上限、工具按需加载、ChildRun（深度上限 1、权限派生、信封四规则校验）均已完成并有测试。

**遗留：** `sql-schema` 的生成源当前是**代码层实体**（AST 扫描），待 alembic 迁移落地后必须替换为 DDL 导出；`protocol` 的生成区尚未覆盖请求体字段级 schema。

**文件：** `docs/skills/`、`tools/skills/`、`backend/app/skills/`、`backend/app/execution/child_run.py`、`tests/skills/`、`tests/execution/test_child_run.py`。

**步骤：**

- [ ] 落地 `docs/skills/manifest.yaml` 与 L0 常驻层；实现层级、依赖方向（无环、`active` 不得依赖 `planned`）与体积预算校验。
- [ ] 实现契约生成脚本 `tools/skills/gen_sql_schema.py`、`gen_protocol.py`、`gen_tool_catalog.py` 及 `--check` 模式，接入 CI。
- [ ] 补齐 `sql-schema`、`protocol`、`tool-catalog` 三份契约的生成区；**机制验证通过后再补其余五份**。
- [ ] 为工具注册表增加 `intent_tag`、`sink_class`、`owner_module`、`min_authority`、`exclusivity`，缺失或歧义即拒绝注册。
- [ ] 实现工具重叠门：同一 `(intent_tag, sink_class, min_authority)` 下多个 `exclusive` 工具即构建失败。
- [ ] 实现 ChildRun：权限从父 token 派生、深度硬上限 1、并发与预算受限、回传信封 schema 与校验。
- [ ] 实现信封四条硬规则的测试：自然语言必须挂 `evidence_refs`；无来源推断标 `kind: inference` 且不得进入学习证据；taint 只增；`status: unknown` 不得推断结果。
- [ ] 实现 skill 与工具 schema 按需加载，并校验单 node 常驻工具数 ≤ 8。

**退出门：** 源码改动未同步契约文档时 CI 失败；同一 intent 的重复工具无法注册；越权或超深度的 ChildRun 被拒绝且无外部副作用；以散文摘要充当证据的路径在测试中失败；单 node 工具数超限即拒绝。

## 批次顺序

1. **批次一：上线门槛** — 任务 1、2、3 的最小闭环，加上任务 5 的策略/预算/outbox/审计最小实现和任务 4 的安全摄取边界。默认仅 A0/A1。
2. **批次二：受控开放** — 完成任务 4 的检索质量、任务 6 的路由/验证、任务 7 的确认 UI、**任务 9 的技能契约与子任务运行时**，逐工具解锁 A2/A3。
3. **批次三：数据驱动优化** — 完成任务 8 的校准、risk-coverage、ANN 选择性调优、模型重标定和教学掌握证据深化。

每批次必须先通过拒绝路径、恢复对账和资产回滚门，才能进入下一批次；任何性能优化不得放松安全、隔离、证据和失败方向不变量。
