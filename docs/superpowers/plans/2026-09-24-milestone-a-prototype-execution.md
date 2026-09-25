# Milestone A 可用原型执行计划

> 2026-09-24；依据 [快速迭代交付策略](../specs/2026-09-24-08-rapid-iteration-delivery-policy.md) 和 [快速迭代 MVP 计划](2026-09-24-rapid-iteration-mvp-plan.md)。本计划只安排首版可演示的用户闭环；更早的架构计划保留为历史与契约参考。

**工作区约定：** 权威新策略与本计划位于主工作区 `E:/codex_workspace/study-plan`（尚未提交）。所有 Luna 产品代码与测试修改只在隔离工作区 `E:/codex_workspace/study-plan-prototype` 的 `codex/milestone-a-prototype-20260924` 分支进行。隔离工作区基于 `ad49ed4`，因此执行前须从主工作区**只读**上述两个未提交的策略/计划文件及本文件；不得假定隔离工作区已有它们，也不得复制覆盖主工作区用户修改。规划者在每批验收时比较两个工作区的状态。

## Goal / 不变量 / 退出门

**Goal：** 普通用户在浏览器中完成注册或登录、创建或选择项目、诊断、获得带任务的计划、提问并查看资料引用、完成任务与提交反馈；重新打开后读回这些状态。

**Invariants：**

1. 任何读取与写入都沿用现有认证、成员关系及 `tenant_id + project_id` 边界；浏览器不自报主体。
2. 所有修改命令沿用现有 `Idempotency-Key` 与未知结果重试；切换账号、项目或会话后，旧响应不能污染当前界面。
3. 原始会话和引用快照保持可回读；上下文裁剪不改变已保存消息、引用坐标或资料外发许可。
4. 自报提交保持 `self_report` 语义，不显示成自动评分或已验证掌握；`verified` 只取服务端计算值。
5. 模型输出不能自动变成用户事实；记忆必须有来源、作用域和状态。
6. 每次付费派发的预算、结果未知及重放语义沿用现有实现。

退出门：完整浏览器旅程可重复；重启后 PostgreSQL 读回；P0=0、核心路径 P1=0；P2/P3 按 `docs/tech-debt.md` 字段登记；里程碑总门禁一次通过。正式 provider 有凭据时单独验收；没有凭据时明确显示模拟运行的限制，不虚报为真实模型验收。

## 差距矩阵

| 工作 | 仓库现状 | 必须交付的最小差距 | 顺序 |
| --- | --- | --- | --- |
| A1 普通用户入口 | `frontend/app.js` 已有注册、登录、登出、项目创建与选择；后端账号和项目 API 已有 | 做一次真实旅程回归及可理解的入口文案；发现 P0/P1 才改 | 验收时 |
| A2 React 学习闭环 | 页面已有会话、手工空任务计划、资料、引用；后端已有诊断、生成计划、任务状态与自报提交 API | 接通诊断→生成带任务计划→任务开始/提交/完成→重开读回；保留旧手工计划可读 | 第一批 |
| A3 ContextBuilder v0 | `teaching/context.py` 已有关键词/混合检索快照、最近消息字符预算和问题长度；`service.py` 另有整体输入 token 上限 | 增入当前项目目标及当前进行任务，并用统一固定预算决定保留项；保留白名单、冻结快照和重放 | 第一批，与前端互不改同文件 |
| A4 Memory v0 | `learning/memory_store.py` 是**证据仓储**，不是长期记忆；暂无带来源的 user/project memory | 最小模型、内存/PG 仓储、RLS、来源与状态；只接受明确用户事实/选择，后续接入 A3 | 第二批，待 A3 接口稳定 |
| A5 真实场景指标 | 已有受保护 `/metrics`、provider usage、run/retrieval decision；缺各阶段耗时与失败归因 | 尽量从现有持久时间戳派生；只补无法派生的有界字段和脱敏低基数聚合 | 第二批，避开 A3 对 `service.py` 的修改 |

**重要发现：** `POST /projects/{id}/diagnosis`、`POST /projects/{id}/plan/generate`、`POST /projects/{id}/tasks/{id}/transition` 和 `POST /projects/{id}/tasks/{id}/submissions` 已存在于 `backend/app/api/product_learning_routes.py`。`plan/generate` 要求先诊断且返回 3 个有评估映射的任务。当前手工计划页面创建的 `tasks: []` 无法走任务闭环；手工任务还不能提交映射证据。因此首批应让主入口使用现有生成计划 API，不能靠给空计划画三个假任务。旧手工计划若存在，界面应能查看，并允许用户明确触发新版本生成，不能悄悄覆盖。

## 第一批并行执行：两个独立 Luna max

### Luna-1：浏览器学习闭环

**独占文件：** `frontend/app.js`、`frontend/views.js`、`frontend/project-state.js`、`frontend/app.css`，以及新建 `tools/check_milestone_a_browser.py`。必要时可改 `backend/tests/test_frontend_assets.py` 中与资源加载直接相关的测试。不要改后端业务、迁移、`frontend/api-client.js` 或 `frontend/commands.js`，除非向规划者报告现有契约无法使用。

**直接复制的任务提示词：**

> 你负责 Study Plan Milestone A 的浏览器学习闭环。**仅在 `E:/codex_workspace/study-plan-prototype` 修改产品代码和测试。**先从主工作区 `E:/codex_workspace/study-plan` 只读 `docs/superpowers/specs/2026-09-24-08-rapid-iteration-delivery-policy.md`、`docs/superpowers/plans/2026-09-24-rapid-iteration-mvp-plan.md`、`docs/superpowers/plans/2026-09-24-milestone-a-prototype-execution.md` 和 `.agents/skills/study-plan-fast-development/SKILL.md`，再读隔离工作区的 `frontend/app.js`/`views.js`/`project-state.js`/`commands.js` 及 `backend/app/api/product_learning_routes.py`/`product_schemas.py`。仅修改你独占的文件。用现有 API 接通：项目选择后读取 `GET /diagnosis`（204 为空）；三字段诊断表单 (`experience_level`, `weekly_hours` 1–40, `preferred_style`)；`POST /diagnosis` 后 `POST /plan/generate`（body `{}`）生成真实三任务计划；从 `GET /plan` 展示任务状态，按 `pending → in_progress → done` 的合法状态流转；对进行中的任务提交 `{mode:'self_report',content}`，展示提交记录和服务端 `verified`，清楚标注自报反馈不等于自动评分。每次 mutation 经现有 `executeCommand`，沿用未知结果同键重试；读请求沿用 scope/epoch guard。刷新和重新登录后能读回诊断、计划、任务与提交；切账号/项目/会话时旧响应不能覆盖新状态。已有手工计划保持可读，生成新计划须显式操作。不要扩成设置页或重做设计系统。先写与本次路径对应的浏览器验收，再实现；只跑定向验证并报告具体命令/结果、截图路径、未完成事项。不得修改主工作区文件，不推送或部署。

**验收：** 使用一个新账号和项目，浏览器完成“诊断→生成真实任务→开始→自报→完成→刷新读回”；另测项目切换时异步旧响应、网络响应丢失的同键重试；画面不把自报称为自动验证。`node --check` 覆盖修改的 JS；运行新增浏览器脚本及相关前端资源测试。

### Luna-2：ContextBuilder v0

**独占文件：** `backend/app/teaching/context.py`、`backend/app/teaching/prompts.py`、`backend/app/teaching/service.py`、`backend/app/workers/teaching.py`、`backend/tests/test_teaching_context.py`；必要时新增一个聚焦测试文件。不得修改 frontend、DB migration、`backend/app/metrics.py` 或 provider/预算仓储。若确需改端口，应先向规划者说明最小改动和冲突点。

**直接复制的任务提示词：**

> 你负责 Study Plan Milestone A 的 ContextBuilder v0。**仅在 `E:/codex_workspace/study-plan-prototype` 修改产品代码和测试。**先从主工作区 `E:/codex_workspace/study-plan` 只读 `docs/superpowers/specs/2026-09-24-08-rapid-iteration-delivery-policy.md`、`docs/superpowers/plans/2026-09-24-rapid-iteration-mvp-plan.md`、`docs/superpowers/plans/2026-09-24-milestone-a-prototype-execution.md` 和 `.agents/skills/study-plan-fast-development/SKILL.md`，再读隔离工作区的 `backend/app/teaching/context.py`、`prompts.py`、`service.py`、`backend/app/product/ports.py` 与现有上下文/引用/恢复测试。保留现有固定 system prompt、资料外发 `display_policy=full` 白名单、冻结检索快照、引用校验、原始消息持久化和 run 幂等重放。以已授权的项目读取取得项目目标，以当前计划中确定性的 `in_progress` 任务取得当前任务（无任务则省略）；将这两项作为带边界的数据而非 system 指令加入 prompt。继续使用最近消息及最多 5 条允许外发的证据；设单一可解释的输入 token 上限，先保留问题与必要元数据，再按排名选择证据、按新近度选择历史；预算不足时明确拒绝或裁剪，不能在 provider 派发后才发现超额。不得让旧项目任务/资料混入新项目，不在本批实现记忆、摘要、语义检索或 provider 调优。写定向测试覆盖无计划、有进行中任务、项目隔离、超预算、资料许可与重放稳定；运行相关测试、ruff/mypy 定向检查并报告结果。不得修改主工作区文件，不推送或部署。

**验收：** provider 请求可看到当前项目目标及一个确定的进行中任务；无计划时仍能教学；历史和证据在预算内；跨项目数据不得进入；原有引用/预算/重放测试通过。注意 `TeachingService` 当前只持有 `products` 仓储，而项目目标在 `membership.get(actor, project_id)`；若要注入 membership，必须在 worker 装配与测试构造处保持兼容，不用裸项目 ID 绕开鉴权。

### 第一批定向验证命令

以下命令均在隔离工作区根目录执行；新增浏览器脚本的启动方式由 Luna-1 在文件头和交付报告中写清。

```powershell
node --check frontend/app.js
node --check frontend/views.js
node --check frontend/project-state.js
.venv/Scripts/python -m pytest backend/tests/test_frontend_assets.py backend/tests/test_learning_loop_api.py -q
.venv/Scripts/python -m pytest backend/tests/test_teaching_context.py backend/tests/test_teaching_citations.py backend/tests/test_teaching_recovery.py -q
.venv/Scripts/python -m ruff check backend/app/teaching backend/app/workers/teaching.py backend/tests/test_teaching_context.py
.venv/Scripts/python -m mypy backend/app/teaching backend/app/workers/teaching.py
git diff --check
```

浏览器关键旅程必须用真实运行的应用执行，不能用 DOM 文字单测代替。测试环境若缺数据库/provider，则分别使用现有 PG 临时库和 ScriptedProvider；报告须区分真实 API+持久化、模拟 provider、真实 provider 三种证据。

## 第二批：审查第一批后再派发

### 现状与串行依赖

迁移位置是 `alembic/versions/`，当前单头 `0019`；`backend/app/platform.py` 的 `EXPECTED_SCHEMA_VERSION` 也是 `0019`。现有 `learning/memory_store.py` 是证据仓储，不能借名当长期记忆。现有 `teaching_runs.created_at/updated_at` 可给出**包括排队的运行总历时**，`provider_attempts.input_tokens/output_tokens` 是权威用量，`retrieval_decision` 已持久化。`provider_attempts.updated_at` 在 `record_result` **之后**还会被 `finish_run` 更新，不能当成准确 provider 耗时。`messages` 表没有 `principal_id`；只看 `role='user'` 无法证明记忆来源属于当前用户，必须联到 `teaching_runs.user_message_id + principal_id`。

第一批 A3 审查通过、`context.py`/`service.py` 的接口稳定后启动第二批。Memory 与指标可以并行，但**Memory 独占迁移 `0020` 与 `platform.py`/`main.py`；指标本批不新建迁移，也不改这些文件**。Memory 只建带来源的仓储/API，不在并行期间编辑 A3 文件；指标只加已有运行存证中的数字字段及脱敏报告，不编辑 Memory 文件。两支工作在同一个隔离分支，执行代理只动各自文件，规划者检查 `git diff` 后集成。

### Luna-3：Memory v0 仓储、API 与 RLS

**独占文件：** 新建 `backend/app/learning/user_memory.py`、`backend/app/learning/user_memory_ports.py` 或等价清晰命名，新建 `backend/app/learning/user_memory_store.py`、`backend/app/db/user_memory_store.py`、`backend/app/api/memory_routes.py`，`alembic/versions/0020_user_memory.py`；修改 `backend/app/platform.py`（装配、`EXPECTED_SCHEMA_VERSION=0020`）和 `backend/app/main.py`（挂路由）；新增聚焦测试 `backend/tests/test_user_memory.py` 与 `backend/tests/test_user_memory_postgres.py`。不要改 `teaching/*`、`product/*`、现有证据仓储或指标文件。需要生成的契约只在本任务迁移完成后更新。

**最小数据契约：** 新表暂定 `user_memories`，`memory_id`，`tenant_id`，`principal_id`（即用户身份，沿用仓库命名），可空 `project_id`（空=user scope），`source_project_id`（来源所在项目），`source_message_id`，`type ∈ {fact, goal, constraint, preference, decision}`，有界 `content`，`status ∈ {active, archived}`，`created_at/updated_at`。`scope=project` 时 `project_id=source_project_id`；`scope=user` 首版只允许用户明确保存的 `preference`，由用户主动全局使用，不能把项目事实或目标转成跨项目记忆。来源必须来自当前主体在来源项目发起的 `teaching_runs.user_message_id`，其 `question` 含保存的原文片段；不从模型答案、系统消息或资料中自动抽取。数据库用组合外键约束来源运行的租户、项目、消息与主体，并启用 `FORCE ROW LEVEL SECURITY`，策略同时限制 tenant/principal，项目记忆再限制 project；应用角色只授必要权限。`source_project_id` 即使对 user scope 也必须保留，以便追溯和撤销来源。服务端写入前必须 `membership.get(actor, source_project_id)`，不得凭客户端 `principal_id`。

**最小 HTTP 契约：** `POST /projects/{project_id}/memories`：`{source_message_id,scope,type,content}`，服务端解析并核对当前用户的来源 run；沿用 `idempotent_write`，返回新记忆。`GET /projects/{project_id}/memories`：当前项目 active 记忆及该用户明确保存的 active 全局偏好，返回稳定排序和来源字段。`PATCH /projects/{project_id}/memories/{memory_id}`：仅 `{status:'archived'}`，不物理删历史；已有归档项重复请求幂等。跨用户、跨项目、来源不合法与不存在统一安全拒绝，不把别人的资源是否存在泄给客户端。用户可用的 UI 保存入口留给第三批 A3 接线后统一补，不能因为暂时没有 UI 而省掉 API 契约与持久化验证。

**直接复制的任务提示词：**

> 你负责 Study Plan Milestone A 的 Memory v0。仅在 `E:/codex_workspace/study-plan-prototype` 修改上述独占文件。先从主工作区 `E:/codex_workspace/study-plan` 只读最新快速交付策略、MVP 计划、本执行计划与 `.agents/skills/study-plan-fast-development/SKILL.md`，再检查隔离工作区的 `0005_learning_loop.py`、`0010_teaching_runs.py`、`db/session.py`、`platform.py`、`api/product_learning_routes.py` 和 `backend/tests/pg_support.py`。实现本节模型、API、内存/PG 仓储与 `0020` 自包含可逆迁移。尤其注意 `messages` 没有作者列，必须通过 teaching run 的 `principal_id` 证明 `source_message_id` 是该用户自己发出的消息；user scope 仅明确保存的偏好，project scope 不跨项目。内容必须是用户原话的有界片段，不能从模型推断自动入库。写反例测试证明缺身份上下文不可读、跨用户/项目不可读/写、伪造来源拒绝、存档后上下文查询不再返回、重启后恢复及幂等重放；升级→降级→升级用随机 `study_test_*` 临时库，不能碰业务库。审查第一批 A3 的实际 diff，但不要改 `teaching/context.py`/`service.py`，把待接线接口写在交付报告。只跑聚焦测试与静态/迁移/契约验证；列明 P0/P1/P2/P3 和实际命令结果。不改主工作区，不推送或部署。

**最小验收命令：**

```powershell
.venv/Scripts/python -m pytest backend/tests/test_user_memory.py backend/tests/test_user_memory_postgres.py -q
.venv/Scripts/python tools/migrations/check_migrations.py --require-active
.venv/Scripts/python tools/skills/gen_contracts.py --all --check
.venv/Scripts/python -m ruff check backend/app/learning backend/app/db/user_memory_store.py backend/app/api/memory_routes.py backend/tests/test_user_memory.py backend/tests/test_user_memory_postgres.py alembic/versions/0020_user_memory.py
.venv/Scripts/python -m mypy backend/app
git diff --check
```

PG 用例必须通过 `pg_support` 创建随机临时库、断言 `study_app` 实际受 RLS 约束并覆盖迁移往返。有已保存记忆时若降级会丢数据，迁移要明确拒绝或由里程碑回滚计划处理，不能悄悄删除。

### Luna-4：真实场景指标 v0

**独占文件：** `backend/app/api/teaching_routes.py`、`backend/app/teaching/ports.py`、`backend/app/teaching/memory_store.py`、`backend/app/db/teaching_store.py`、`backend/app/teaching/service.py`，以及新建 `tools/report_learning_scenarios.py`、`backend/tests/test_learning_scenario_metrics.py`。不改 `alembic/versions/*`、`platform.py`、`main.py`、Memory 文件或现有 `/metrics` 聚合函数；真实数据样例输出到 gitignore 的 `var/`。第一批 A3 审查通过后，确认 `service.py` 的最终形状再下发此任务。

**最小存证/报告契约：** 现有 `run.created` 事件 payload 增服务端 `request_id`（由 `current_request_id()` 取，客户端不能自报）；`run_id` 是异步教学任务的稳定关联键，两者分别保留。`mark_dispatched` 的 `request_payload` 可增加 `telemetry: {retrieval_latency_ms, retrieval_hit_count, approved_material_count}`（非负有界整数；只存数字，不存新增正文）。成功收到 provider 结果时在 `record_result` 的既有 `result_payload.telemetry` 记 `provider_latency_ms`，由单次 `monotonic()` 调用区间测量；结果未知、拒绝、无权威时只写可证明值或 `null`，不从最后一次 `updated_at` 冒算。报告从已有表/事件抽取：`run_lifetime_ms = run.updated_at - run.created_at`（明确含排队）、上述 retrieval/provider 耗时、权威 `input_tokens/output_tokens`、命中数、闭集 failure category（如 `none`, `retrieval`, `provider`, `budget`, `unknown`），以及 run/request id。旧 run 字段缺失输出 `null`，不可填 0。报告读取原始行但仅输出稳定 ID、数字、闭集分类与状态，不输出 question、prompt、资料、provider 响应正文；CLI 默认只允许测试库 DSN，真实环境运行要显式授权与限定时间窗。当前 `/metrics` 仍用于低基数全局指标；高基数 request/run ID 只留在私有场景报告，绝不做 Prometheus label。

**直接复制的任务提示词：**

> 你负责 Study Plan Milestone A 的真实场景指标 v0。仅在 `E:/codex_workspace/study-plan-prototype` 修改上述独占文件。先从主工作区只读最新快速交付策略、MVP 计划、本执行计划和 `study-plan-fast-development` 技能，再看隔离工作区已审查的 A3 实现、`0010/0011/0018/0019` 迁移、`metrics.py`、`db/metrics_store.py` 与教学恢复测试。复用现有 run/attempt/event 存证，按本节契约增加 request id、检索耗时/命中、provider 耗时及只输出脱敏数字的真实场景报告。不要新建迁移、修改预算/结算语义、改现有 `/metrics` 鉴权或记录敏感正文。`provider_attempts.updated_at` 会被最终落定再次更新，不得拿它当 provider 延迟；结果未知时也不能伪造 0ms 或 token 用量。保证幂等重放不二次调用 provider，指标不会为了重放重复计数。聚焦测试覆盖正常、无资料、provider 超时/结果未知、老 run 缺字段、跨项目报告限制、敏感正文哨兵不泄露、有限失败分类。仅跑相关测试与 ruff/mypy，报告命令、证据与未完成项；不改主工作区，不推送或部署。

**最小验收命令：**

```powershell
.venv/Scripts/python -m pytest backend/tests/test_learning_scenario_metrics.py backend/tests/test_teaching_recovery.py backend/tests/test_teaching_context.py backend/tests/test_metrics.py -q
.venv/Scripts/python -m ruff check backend/app/teaching backend/app/db/teaching_store.py backend/app/api/teaching_routes.py tools/report_learning_scenarios.py backend/tests/test_learning_scenario_metrics.py
.venv/Scripts/python -m mypy backend/app
git diff --check
```

### A3 接线与用户旅程整合时序

1. 6-sol high 审查首批 A3 实际接口后冻结：`build_context` 的预算入口、Memory 可注入的只读列表接口、冻结快照的重放策略。若首批修改未通过，先派修复任务，不让第二批围绕错误接口建设。
2. Memory 与指标并行。Memory 在 `0020` 测试库完成且组合根装配后，指标仍不依赖此表；若两者都需碰 `teaching/service.py`，Memory 此批不得触碰，冲突交给下一步集成。
3. 两项分别经 6-sol high 与 Open Code Review 审查，P0/P1 修完。由下一位 Luna max 只改 A3 接线文件和必要前端入口：从已授权项目读最多 K 条 active 项目记忆/明确全局偏好，按固定预算进入数据区；用户在消息上明确“记住”或管理归档；旧 run 重放只用冻结快照，不重新抓当前记忆。此步需检查 Memory 内容是否应随教学请求快照持久化，不能让重试悄悄改变上下文。
4. 接线完成后执行完整浏览器旅程、PG 重启读回与里程碑总门禁。`EXPECTED_SCHEMA_VERSION` 应最终为 `0020`；指标本批不另升迁移版本。若审查认为 A5 必须进入公共 `/metrics` 聚合，则另开 `0021` 单独任务，先说明真实使用为什么需要它，再做自包含快照函数及性能重测；不把它塞进并行批次。

## 审查—优化闭环

1. 每个 Luna 返回：修改文件、行为契约、定向测试命令与结果、手工旅程证据、P0/P1/P2/P3 自评、已知限制。共享工作区下不得清理现有未提交文件，不得自行合并、推送或部署。
2. 规划者 6-sol high 审查完整 diff、影响面、测试证据及主路径；`Open Code Review` 独立审查同一批产出。尤其检查项目隔离、幂等未知结果、证据真实性、付费派发与引用快照。
3. 两份审查意见逐项复现和分级。P0 立即修；核心路径 P1 当前里程碑修；P2/P3 记录到 `docs/tech-debt.md` 后继续。修复提示词写具体失败测试、文件边界和退出条件，再派 Luna max。
4. 第一批审查收口后冻结 ContextBuilder 接口，第二批并行 Memory 与指标；随后做一次 ContextBuilder/Memory 接线和完整浏览器旅程。每批保持“规划→执行→审查→优化”，不逐个小函数做全量门禁。
5. Milestone A 末统一运行全量 pytest、PostgreSQL E2E、Ruff、mypy、生成契约检查、迁移往返（若 schema 有改动）、浏览器主路径和重启读回。记录真实 provider 是否验收及模拟器边界；不因无真实凭据声称真实模型成功。

## 本轮规划边界

“满足所有功能的原型”按用户最新优先级解释为 **Milestone A 九步用户闭环 + A1–A5 最小技术支持**。LightRAG、reranker、完整多级记忆、PDF/Office/OCR、100k 并发优化和复杂沙箱等，依据最新策略放在数据触发阶段，不混入首版任务。仓库当前有未提交的 `task_plan.md`、`progress.md` 及未跟踪文件；执行代理必须保留它们的原样。
