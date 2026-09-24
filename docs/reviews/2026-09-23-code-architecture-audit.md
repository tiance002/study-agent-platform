# 代码与架构审查报告（2026-09-23）

> 下方“范围与结论”“问题清单”“验证记录”记录初审时状态；修复后的实际状态见“2026-09-23 修复追踪”。

## 范围与结论

本次审查覆盖 `backend/app`、`backend/tests`、`frontend`、`alembic`、`tools`、CI、部署配置、README，以及工作区内未提交的第九轮资料获取与指标变更。以 06 号分层架构规格和现有 CI 约束为基线。审查以只读分析、静态检查、全量测试及一次临时目录中的 API 故障注入为证据；未删除或改动业务文件。

当前代码尚不满足合并或发布门禁：全量测试有 12 项失败、1 项跳过，Ruff 有 3 项错误，mypy 有 4 项错误。多数 PostgreSQL 失败是同一个迁移版本不一致造成的，不能按失败数理解为 12 个独立缺陷。当前工作区含大量未提交改动，结论是对**审查时状态**的快照。

优先级定义：P0 = 阻止当前版本合并/发布；P1 = 可导致持久状态错误或核心边界失效；P2 = 明确的设计、维护或行为缺口；P3 = 可在确认归属后清理的低风险项。

## 问题清单

| 编号 | 优先级 | 类别 | 问题与证据 | 建议 |
| --- | --- | --- | --- | --- |
| A01 | **P0** | 迁移与运行一致性 | 新迁移 `alembic/versions/0018_acquisition_metrics.py:7` 定义 head `0018`，但 `backend/app/main.py:119` 仍将 `EXPECTED_SCHEMA_VERSION` 固定为 `0017`。全量测试临时库迁移至 `0018` 后，多个 PostgreSQL 测试在 `main.py:321` 的启动自检被拒绝；同样的顺序若用于部署会使 Web/worker 无法启动。 | 将代码预期版本、迁移、生成契约和部署顺序在同一变更内闭合；保留启动拒绝机制，并增加“迁移 head = 代码预期版本”的静态门禁。 |
| A02 | **P1** | 跨仓储事务 | `backend/app/api/product_routes.py:524-552` 先调用 `products.register_source`，再调用 `acquisition.select`。PG 实现分别在 `backend/app/db/product_store.py:384` 与 `backend/app/db/acquisition_store.py:218` 开独立事务；内存实现亦分两个临界区。故障注入令 `select` 拒绝后，API 返回 403，但 sources 从 0 变为 1，候选仍是 `discovered`。 | 把“登记来源 + 锁定候选 + 创建下载任务”下沉为同一领域命令；PG 在一个事务内写入，内存适配器在一个锁内模拟原子性。增加失败回滚与并发竞争测试。 |
| A03 | **P1** | 检索契约 | `backend/tests/test_knowledge_search.py:699-707` 要求冻结期望引用包含 `document_id`、`span`、`content_hash`，但 `backend/tests/fixtures/retrieval_v1.json:37-57` 仍只有 `source_id` 和 `chunk_index`，该不变量测试失败。 | 以稳定文档 ID、真实切块跨度和内容哈希补齐手工评测夹具；复核引用是否与不可变原文对齐，避免只为了过测填占位值。 |
| A04 | **P1** | 质量门禁 | Ruff 在 `backend/app/metrics.py:3`、`backend/tests/test_metrics.py:1` 报 import 顺序，在 `backend/tests/test_acquisition_worker.py:111` 报未用变量 `queued`。mypy 在 `backend/app/knowledge/memory_acquisition_store.py:240`、`backend/app/db/acquisition_store.py:385` 报无效 `type: ignore` 和 `str` 不能赋给封闭的 outcome 字面量类型。新迁移还使 `docs/skills/contracts/sql-schema.md` 的 source hash 过期。 | 修正导入和未用变量；将端口参数类型收紧为 `Literal` 或显式转换/校验，删除无效 ignore；重新生成 SQL 契约并跑全门禁。 |
| A05 | **P1** | 分层依赖 | `backend/app/db/idempotency_store.py:3` 从 `app.api.http_idempotency` 继承 PostgreSQL 核心类，真正的 SQL 实现位于 `backend/app/api/http_idempotency.py:350-505`。这让数据访问层依赖 HTTP 接入层，并把协议、内存仓储、SQL 仓储和 FastAPI 守卫放在同一文件。 | 将幂等协议/结果类型移至中立模块，SQL 实现移到 `app.db`，API 只保留 HTTP 守卫；保留兼容导出并添加 `db -> api` 禁止导入门。 |
| A06 | **P2** | 架构门禁盲区 | `backend/tests/test_import_direction.py:26-39` 的 `LAYER` 未列 `identity`、`product`、`teaching`、`db`、`main` 等主要包；`test_no_upward_dependencies` 对未知包直接跳过（约 100 行）。例如 A05 的 `db -> api` 因而未被 CI 捕获。 | 定义完整包层级或单独的允许依赖矩阵；对未登记的新包 fail closed。组成根 `main` 可单独声明例外，类型检查专用导入可显式处理。 |
| A07 | **P2** | 候选状态机 | `backend/app/api/product_routes.py:406,492` 为候选设置 7 天过期；`backend/app/knowledge/acquisition.py:108,282-286` 定义 `expires_at`/`EXPIRED`，但 `backend/app/knowledge/memory_acquisition_store.py:90-126` 与 `backend/app/db/acquisition_store.py:218-260` 的选择路径仅校验 `discovered`，没有检查到期时间；`transition_candidate` 只被模型测试引用。过期候选仍可进入下载队列。 | 在仓储的原子选择条件中同时检查 `expires_at > now`；到期时确定性转换为 `expired` 或返回明确错误；为内存与 PG 适配器加入时钟固定测试。 |
| A08 | **P2** | 单一职责 | `backend/app/api/product_routes.py` 超过 1000 行，混合会话、计划、资料搜索/获取、知识检索、诊断和提交；其中 `_build_bundle`（271）、`_generated_bundle`（877）等业务组装逻辑直接位于 HTTP 层。`frontend/app.js:76-824` 的 `App` 同时管理 31 个 state、认证、命令幂等、轮询、资料和引用；`backend/app/workflow/runtime.py` 也兼管执行、幂等、预算与失败收尾。 | 先按领域抽出业务服务和纯转换函数，再按会话/计划/资料/学习拆分路由；前端抽出 scoped API/命令与轮询 hook，并给状态作用域建立明确接口。逐步拆分，保持公开路由和重放语义。 |
| A09 | **P2** | 重复代码 | API 路由中出现 36 处 `with idempotent_write` / `if guard.replay` 片段（主要在 `api/product_routes.py`），每个端点重复构造 `JSONResponse`、`X-Idempotent-Replay` 和 `guard.complete`。重复主要是接入层模板，内存/PG 仓储的相似方法则承担不同持久化语义，不宜直接合并。 | 将重放响应和完成响应的固定部分收成可测试 helper/依赖，保留每个命令显式的业务体；避免一次性装饰器化而掩盖事务边界。 |
| A10 | **P2** | 文档/配置漂移 | `README.md:24-25,33` 仍称 Fetcher 不出网、没有网页抓取、没有集中指标；实际 `backend/app/knowledge/fetcher.py` 有受限 HTTP 获取和 HTML 处理，当前变更含 `/metrics` 与持久指标。`backend/app/main.py:22` 仍写安全配置在 `app.core.deployment`，实际为 `app.deployment`。 | 随第九轮实现更新能力表和模块注释，分别说明已实现、默认关闭、未部署及计划中能力；README 不再把有真实抓取的路径描述为占位。 |
| A11 | **P3** | 未使用接口 | `backend/app/tenancy/ports.py:15-25` 的 `RecordRepository` 在应用与测试中均无导入，注释仍称 PostgreSQL/RLS 适配器未实现，与当前项目状态不符。 | 确认没有外部插件依赖后删除该端口，或为实际用途补调用与契约测试；不要仅因静态零引用删除动态加载模块。 |
| A12 | **P3** | 历史工具与本地文件 | `tools/run_round7_preview.py`、`tools/check_round7_browser.py` 和 `tools/run_round6_gate.py` 是轮次专用入口，当前 CI 只调用直接门禁；`tools/run_round8_gate.py` 仍包含旧轮次流程。`verify_components.py` 与 `INTEGRATION_SUMMARY.md` 是未跟踪的 Trae 集成材料，前者将工作区绝对路径写死在 `verify_components.py:94,112`，与新的 `.codex/` 集成配置并存。 | 先确认是否有团队仍运行旧入口；将历史脚本和说明移到归档或合并成参数化工具。Trae 材料由其所有者确认归属后再决定纳入、迁出或移除。 |
| A13 | **P2** | 指标扩展性风险 | `alembic/versions/0018_acquisition_metrics.py:33-225` 的 `study_metrics_snapshot()` 每次抓取汇总全表 `acquisition_jobs`、fetch observations、`teaching_runs` 与 `provider_attempts`。函数虽只输出封闭标签，但随着任务增长会把监控抓取压力直接压到主库。当前没有基准或执行计划，属于容量风险，非已证明的性能故障。 | 用目标规模数据测 `EXPLAIN (ANALYZE, BUFFERS)` 和抓取耗时；若超出预算，再改为增量汇总/物化事实或限频缓存，并保持跨租户聚合权限边界。 |

## 2026-09-23 修复追踪

| 编号 | 状态 | 处理结果与剩余工作 |
| --- | --- | --- |
| A01、A03、A04 | 已修复 | 预期迁移版本改为 `0018` 并增加 head 一致性门；冻结引用补齐实际文档 ID、跨度和哈希；修复 Ruff/mypy 并重生成 SQL 与协议契约。 |
| A02、A07 | 已修复 | API 使用 `select_with_source`；PG 在一个事务中登记来源并选择候选，内存实现共同加锁；到期候选被拒绝，失败回滚由定向测试覆盖。 |
| A05、A06 | 已修复主要缺口 | 幂等协议/内存适配器移至 `identity`，SQL 实现移至 `db`；新增 `db → api` 禁止导入测试和覆盖全部顶层模块的显式依赖清单。**组成根依赖已收口**（2026-09-23 跟进轮任务 2）：`PlatformState`、`build_platform` 及装配助手移至 `app.platform`，`api/`、`workers/` 对 `app.main` 的反向依赖已消除并加入静态失败门，`app.main` 保留兼容导出。 |
| A08 | 本轮拆分完成 | 计划构造移至 `learning.plan_builders`；前端请求客户端与展示组件分别移至 `frontend/api-client.js`、`frontend/views.js`。**跟进轮任务 3-5 完成**：产品路由按领域拆为 `product_schemas.py` 与会话/资料/学习三个领域路由（`product_routes.py` 仅聚合，OpenAPI 无差异）；前端命令作用域与项目数据加载拆为 `frontend/commands.js`、`frontend/project-state.js`（App 仅保留认证/表单/选择/轮询/编排）；`workflow.runtime` 的进程内幂等与预算生命周期分别拆至 `runtime_idempotency.py`、`run_budget.py`（组件不反向依赖运行器）。 |
| A09 | 已修复重复模板 | 18 个路由中的幂等重放响应统一调用 `WriteGuard.replay_response()`；业务完成与事务边界仍由端点显式控制。 |
| A10、A11 | 已修复 | 更新 README 对网页获取和指标的描述；删除零引用的 `RecordRepository` 端口并修订包说明。 |
| A12 | 已整理入口清单 | Trae 检查脚本去除绝对工作区路径。**跟进轮任务 7 完成**：新建 `tools/README.md` 作为入口索引，区分 CI 门禁、本地总门禁（Round 8 系列）、历史复现入口（Round 6/7 脚本保留原位并标注替代者与历史引用）与可重建产物；未跟踪的集成材料保留，不作不可逆清理。 |
| A13 | 已完成容量验证 | **跟进轮任务 6 完成**：`tools/bench_metrics_snapshot.py` 在临时库实测 1 万/10 万/100 万事实规模（p95 分别 9.8/21.1/666.7 ms），各表 `EXPLAIN (ANALYZE, BUFFERS)` 见 `docs/performance/metrics-snapshot-2026-09.md`。决定保留现有实现（15 秒缓存钳制查询频率 ≤4 次/分钟/进程，100 万规模数据库占用约 4.4% 单核）；100 万规模 p95 超 250 ms 目标，增量汇总方案记入 `docs/tech-debt.md` TD-001 并附触发条件。 |

修复后验证：全量 `pytest -q --tb=short` 退出码 0（1 项条件性跳过）；Ruff、mypy（128 个源文件）、JavaScript 语法、三份生成契约、行尾测试和 `git diff --check` 均通过。以上结果不包含生产规模压测或真实浏览器交互验收。
跟进轮最终验收（2026-09-24）：全量 `pytest -q --tb=short` 与 `pytest backend/tests -m postgres -q` 退出码 0；`ruff check backend tools verify_components.py` 零告警；`mypy backend/app` 135 个源文件无问题；三份契约 `gen_contracts.py --all --check` 一致；前端 5 个脚本 `node --check` 通过；浏览器回归 `ROUND8_BROWSER_PASSED`；指标容量实测与"保留现有实现"决定见 `docs/performance/metrics-snapshot-2026-09.md`（TD-001 记于 `docs/tech-debt.md`）。

## 模块与分层评估

| 现有区域 | 主要职责 | 审查判断 |
| --- | --- | --- |
| `frontend/`、`app.api` | 展示、输入、HTTP 边界 | 路由体系基本独立；A08 的业务组装和多仓储事务越过接入职责。 |
| `identity`、`policy`、`tenancy` | 身份、授权和租户上下文 | 端口与实现大体分开；未用的泛化 `RecordRepository` 是历史设计残留。 |
| `workflow`、`teaching.service` | 命令编排、预算和运行恢复 | 持久工作与 Web 请求已区分；单文件职责偏多，需逐步抽取。 |
| `knowledge`、`learning`、`execution` | 资料、证据、能力执行 | 内存/PG 双实现适用于开发和契约测试；候选到期状态未闭合。 |
| `app.db`、Alembic | 事务、RLS、持久事实 | 数据库适配器普遍位于独立包；A05 是明显的反向依赖，A02 是跨适配器原子性缺口。 |
| `app.main` | 组成根 | 静态导入扇出高（约 66 个内部模块）符合组成根职责；不应仅因扇出高判为过度耦合。需要让迁移 head 与装配版本保持同步。 |

06 号规格要求 L1→L2→L3→L4→L5 的单向主链及 L3/L4→L5 的事务交互。当前较大的偏差是“DB 实现落在 API 内”和“一个业务命令跨两个独立仓储事务”。建议先修边界与一致性，再做文件拆分；单纯拆文件不能解决事务问题。

## 文件必要性与清理边界

| 对象 | 判定 | 处理建议 |
| --- | --- | --- |
| `output.json`、`pytest_html_report.html`、`archive/output_*.json` | 已由 `.gitignore` 忽略的测试报告/历史输出，不是应用运行输入。 | 确认不作审计证据后可清理；可重新生成。 |
| `var/` 中以 `.pt-*`、`pytest-*`、`round*-preview` 命名的目录与本地日志/截图 | 测试与预览残留。 | 按生成任务归属和保留期清理；**不要**对 `var/` 整体删除，里面还含审计记录、数据库 dump 和发布包。 |
| `var-ui-test/` | 本地测试残留，当前只有 audit 目录。 | 核对审计数据后清理。 |
| `.pytest_cache/`、`.ruff_cache/`、`.mypy_cache/`、`.playwright-mcp/`、`.serena/runtime/` | 缓存/工具运行状态。 | 需要空间时可重建；不纳入产品发布。 |
| `.trae/`、`verify_components.py`、`INTEGRATION_SUMMARY.md` | 未跟踪的本地集成资料；可能属于正在进行的另一项工作。 | 列为归属待确认，**本次不删除**。 |
| `alembic/versions/0001..0018`、`docs/adr/`、`docs/superpowers/` | 迁移链、决策和设计历史；虽非 HTTP 运行入口，仍是可追溯和新环境建库输入。 | 保留；不得以“历史文件”名义删除。 |
| `frontend/vendor/react*.min.js` | `frontend/index.html:11-12` 正在直接加载，当前构建模式下为运行必需。 | 保留，除非先完成前端构建方案替换。 |

## 验证记录与限制

- `.venv/Scripts/python -m pytest -q -p no:warnings`：**12 failed、1 skipped**。其中 10 项 PostgreSQL 重建/启动失败均可追到 `0018` 与预期 `0017` 不一致；其余为 SQL 生成契约和冻结检索引用样例。该轮测试使用临时 PostgreSQL 测试库，未触碰业务库。
- `.venv/Scripts/python -m ruff check backend tools`：**3 errors**。
- `.venv/Scripts/python -m mypy backend/app --ignore-missing-imports`：**4 errors / 2 files**，检查 127 个源文件。
- 独立临时目录的 TestClient 故障注入：创建候选后让 `acquisition.select` 返回状态转换拒绝，选择接口 403；`sources` 从 0 到 1，候选仍为 `discovered`。这证明内存路径会留下部分写入；PG 代码中的两个独立事务表明相同边界风险，尚未单独运行 PG 故障注入。
- 静态分析发现 127 个 Python 应用模块、约 472 条内部导入边；`main` 的高扇出按组成根解释。零静态导入不等于无用：`db.budget_store` 与 `workflow.tools_impl` 均有相对导入/包导入的实际调用；只有 `tenancy.ports.RecordRepository` 被列为高置信未用接口。
- 本次未做生产请求、部署、删除，也未跑前端真实浏览器和压力测试。A13 需实测后才能定性为性能问题。

## 建议实施顺序

1. 闭合 `0018` 迁移、启动版本、SQL 契约和静态检查，恢复可执行门禁。
2. 修复候选选择的单事务命令，补失败回滚与到期/并发测试。
3. 迁出 API 内的 PostgreSQL 幂等实现，扩大 import 方向门的包覆盖。
4. 补齐冻结检索引用夹具，再按领域拆分产品路由和前端状态/命令逻辑。
5. 更新 README 与工具文档；与本地集成材料所有者确认后再清理历史入口及可重建产物。
