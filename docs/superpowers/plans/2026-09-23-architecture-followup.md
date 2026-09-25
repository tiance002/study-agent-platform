# 架构收口与指标容量验证实施计划

> **给执行者：** 按任务顺序逐项实施和验证；使用 `superpowers:executing-plans` 的检查点方式。每项提示词可单独交给 Codex 执行。当前文档是计划，不代表这些改动已实施。
>
> **执行状态（2026-09-24）：** 七项任务全部完成并验收。任务级证据：任务 1/4 —— `tools/run_round8_gate.py` 对 5 个前端脚本逐一 `node --check`，浏览器回归（登录、作用域切换迟到响应、未知结果同幂等键重试、桌面与 390px 视口）通过并记录 `ROUND8_BROWSER_PASSED`；任务 2 —— 装配移至 `app/platform.py`，`api/`、`workers/` 对 `app.main` 的静态导入被 `backend/tests/test_import_direction.py` 拒绝，`app.main` 兼容导出保留；任务 3 —— 产品路由拆为 `product_schemas.py` 与会话/资料/学习三个领域路由，`product_routes.py` 仅聚合，OpenAPI 与生成契约和拆分前一致；任务 5 —— 幂等与预算生命周期分别拆至 `workflow/runtime_idempotency.py`、`workflow/run_budget.py`，并发/追踪/预算测试全过；任务 6 —— `tools/bench_metrics_snapshot.py` 临时库实测 1 万/10 万/100 万事实快照 p95 为 9.8/21.1/666.7 ms，决定保留现有实现，延期依据见 `docs/performance/metrics-snapshot-2026-09.md` 与 `docs/tech-debt.md`（TD-001）；任务 7 —— `tools/README.md` 入口索引，历史脚本原位标注。最终验收（2026-09-24）：全量 pytest、PostgreSQL 子集、Ruff、mypy（135 文件）、三份契约 `--check`、5 个脚本 `node --check`、`git diff --check` 与浏览器回归全部通过，详见 `docs/reviews/2026-09-23-code-architecture-audit.md` 修复追踪。

**目标：** 完成审查报告 A08、A12、A13 的剩余工作，消除 `api/workers → main` 组成根反向依赖，并补齐新前端脚本的门禁。

**架构：** 先保护现有行为，再沿 HTTP 路由、前端状态、工作流编排三个边界拆分；指标先在隔离数据库中测量，再依据结果优化。历史文件以可追溯、可回退的方式处置。

**技术栈：** Python 3.11、FastAPI、PostgreSQL/Alembic、pytest、Ruff、mypy、原生 React 脚本、Node、Playwright。

## 全局约束

- 当前工作树已有大量已暂存、未暂存及未跟踪改动。执行前记录 `git status --short`；不得 `reset`、`clean`、覆盖或顺手提交他人的改动。
- 保持现有 HTTP 路径、请求与响应、状态码、幂等键、重放标记、RLS、worker 凭据和迁移链兼容。重构任务不得顺带修改业务语义。
- 数据量实验只使用 `backend/tests/pg_support.py::temp_test_database()` 创建的 `study_test_*` 临时库；不得向业务库写入样本或执行重建。
- `.trae/`、`INTEGRATION_SUMMARY.md`、`verify_components.py` 与 `var/` 中的用户数据不在自动删除范围。
- 完成每项后跑该项定向测试。最终跑全量 pytest、PostgreSQL 子集、Ruff、mypy、三份生成契约、三个前端脚本语法检查、浏览器回归和 `git diff --check`；报告实际跳过项。

## 顺序与交付物

| 顺序 | 优先级 | 任务 | 完成标志 |
| --- | --- | --- | --- |
| 1 | P1 | 前端脚本与浏览器门禁 | 三个脚本均受语法门约束；页面加载与关键重放流程有浏览器证据。 |
| 2 | P1 | 组成根依赖收口 | `api/`、`workers/` 不再导入 `app.main`；旧 `app.main` 公共导出仍可用。 |
| 3 | P2 | 产品路由按职责拆分 | 路由签名、OpenAPI 契约和幂等行为与拆分前相同。 |
| 4 | P2 | 前端状态与命令拆分 | `App` 不再直接实现命令占用和项目数据加载；切换作用域与未知结果重试不回退。 |
| 5 | P2 | 工作流幂等与预算生命周期拆分 | 并发、防重放、预算清理与失败状态契约保持不变。 |
| 6 | P2 | 指标容量实验与条件优化 | 有可复现实验数据、内部查询计划、阈值判断和对应处置。 |
| 7 | P3 | 历史入口归档 | 运行入口、历史证据与可清理产物清单明确；无断链或误删。 |

任务 1 是所有前端改动的前置门禁。任务 2、3、4、5 在代码上可以分开审查，但应分别完成、验证后再合并。任务 6 的性能结论不能由小型测试库推断。任务 7 最后执行，以免打断验收脚本。

## 任务 1：补齐前端脚本门禁与浏览器回归

**文件：** 修改 `tools/run_round8_gate.py`、`backend/tests/test_frontend_assets.py`、`tools/check_round8_browser.py`；检查 `frontend/index.html`、`frontend/api-client.js`、`frontend/views.js`、`frontend/app.js`。

- [x] 核对 `index.html` 中 `api-client.js → views.js → app.js` 的加载顺序和同源响应。现有资产测试已覆盖顺序，增加浏览器控制台异常断言。
- [x] 将门禁里的单条 `node --check frontend/app.js` 扩为对三个脚本逐一执行，任一失败即退出非零。
- [x] 在浏览器脚本中覆盖登录、切换项目/会话时迟到响应不可污染当前视图，以及未知结果使用原幂等键重试；复用现有 `install_late_response_gate`，不另建模拟 API。
- [x] 运行 `node --check frontend/api-client.js`、`node --check frontend/views.js`、`node --check frontend/app.js`，以及 `.venv\Scripts\python -m pytest backend/tests/test_frontend_assets.py backend/tests/test_round8_http.py -q`。用 `tools/run_round8_preview.py` 启动一次性预览后运行 `tools/check_round8_browser.py --base-url http://127.0.0.1:8008`，记录浏览器结果并停止预览。

**验收：** 删除任一脚本中的一个闭合括号会使门禁失败；正常脚本通过，浏览器无未捕获异常。

## 任务 2：去除入口对 `app.main` 的反向依赖

**文件：** 创建 `backend/app/platform.py`；修改 `backend/app/main.py`、`backend/app/api/auth_routes.py`、`backend/app/workers/{acquisition,ingestion,teaching}.py`、`backend/tests/test_import_direction.py`；更新引用 `build_platform` 的运行文档。

- [x] 先在 `test_import_direction.py` 增加失败门：`api/` 和 `workers/` 的静态导入目标不允许为 `app.main` 或其子模块。
- [x] 把 `PlatformState`、`build_platform` 及其私有装配助手 `_verify_database_ready`、`_build_runtime`、`_seed_demo_membership` 移至 `app.platform`；将装配所需常量一并迁移并由 `app.main` 兼容导出（包括 `EXPECTED_SCHEMA_VERSION` 和演示主体/项目常量）。避免把 HTTP `create_app` 也移入平台装配模块。
- [x] `auth_routes.py` 的仅类型用途改从 `app.platform` 或局部 `Protocol` 取得；三个 worker 的 CLI 运行期改从 `app.platform` 装配。检查无导入循环、内存模式导入时无需 psycopg。
- [x] 运行 `backend/tests/test_import_direction.py`、`test_frontend_assets.py`、三组 worker 测试，以及 `.venv\Scripts\python -m mypy backend/app --ignore-missing-imports`。通过后将显式依赖清单中的 `api/workers → main` 边删去。

**验收：** 新导入门通过；`from app.main import build_platform, PlatformState` 仍可用；worker `--once` 与 Web 入口启动路径保持原语义。

## 任务 3：产品路由按领域拆分

**文件：** 创建 `backend/app/api/product_schemas.py`、`product_conversation_routes.py`、`product_source_routes.py`、`product_learning_routes.py`；把 `backend/app/api/product_routes.py` 收成路由聚合器；按需要调整 `backend/tests/test_product_api.py`、`test_acquisition_api.py`、`test_learning_loop_api.py`、`test_knowledge_search.py`。

- [x] 先记录 `create_app().openapi()` 中原产品路由的 `path + method + status code + request/response schema` 清单，作为拆分前基准。
- [x] 把 Pydantic 请求模型和字段上限移到 `product_schemas.py`；会话与人工计划路由移到 `product_conversation_routes.py`，资料候选/获取/上传/检索移到 `product_source_routes.py`，诊断/任务/提交移到 `product_learning_routes.py`。
- [x] `product_routes.py` 只聚合三个 `APIRouter`，保持 `main.py` 的 `router as product_router` 接口。`select_with_source` 仍只由一个领域命令调用；每个写端点保留现有 `idempotent_write` 和 `guard.complete` 的先后顺序。
- [x] 比较拆分前后的 OpenAPI 清单与生成协议契约；运行相关 API/PG 测试和 `.venv\Scripts\python tools/skills/gen_contracts.py --all --check`。任何规范差异先判断是模块移动造成的生成噪音还是公开契约变化，后者必须修正。

**验收：** `main.py` 的路由装配不变，URL、状态码和响应结构无变化；资料选择回滚和到期测试继续通过。

## 任务 4：前端状态与命令作用域拆分

**文件：** 创建 `frontend/commands.js`、`frontend/project-state.js`；修改 `frontend/app.js`、`frontend/index.html`、`backend/tests/test_frontend_assets.py`、`tools/run_round8_gate.py`、`tools/check_round8_browser.py`。

- [x] 先为现有命令重放、未知结果、切换项目/会话后的迟到响应写浏览器回归；确认原实现通过。
- [x] 将 `executeCommand`、命令引用表、作用域 epoch 及相关可重试状态抽成 `useScopedCommands`；输入为当前主体/项目/会话标识，输出为执行命令和待处理命令状态。保持同一请求重试沿用原 `Idempotency-Key`。
- [x] 把项目资料列表、会话和计划的数据读取/取消逻辑抽成 `useProjectState`；组件只消费 hook 返回的状态与动作。`api-client.js` 继续只管 HTTP，不持有 UI 状态。
- [x] 在 `index.html` 先加载两个新脚本再加载 `app.js`，把它们加入语法门和资源测试；运行 Node 检查与完整浏览器回归，检查桌面和 390px 视口。

**验收：** 切换作用域后旧响应不能覆盖新数据；未知命令重试不重复执行；`App` 的多领域状态和命令机制不再混在同一函数中。

## 任务 5：拆分工作流运行器的幂等与预算生命周期

**文件：** 创建 `backend/app/workflow/runtime_idempotency.py`、`run_budget.py`；修改 `backend/app/workflow/runtime.py`；重点验证 `backend/tests/test_concurrency_safety.py`、`test_idempotency_and_tracing.py`、`test_execution_and_budget.py`。

- [x] 先锁定现有并发契约：同键并发只执行一次、不同指纹拒绝、失败后可接手、重放使用本次 `request_id`、预算树创建原子且失败后无残留。
- [x] 把 `_IdempotencyEntry`、条件变量、指纹与 claim/complete/release 封装为单一幂等组件；运行器负责将组件结果转换为 `InteractionResult`，避免新组件反向依赖整个运行器。
- [x] 把 `_ensure_budget_tree`、`_close_run_quietly` 及审计所需依赖封装为运行预算生命周期组件，保持原锁与结算/未知结果语义。不要把 HTTP 幂等存储和 workflow 幂等机制合并。
- [x] 先跑并发与预算定向测试，再跑全量测试和 `test_import_direction.py`；出现并发失败时用现有 `racy_scheduling` 复现，不靠增加 sleep 掩盖竞争。

**验收：** 运行器主要负责执行编排；幂等和预算独立可测，所有并发、追踪及预算用例保持通过。

## 任务 6：指标容量实验与条件优化

**文件：** 创建 `tools/bench_metrics_snapshot.py`、`docs/performance/metrics-snapshot-2026-09.md`；视实验结果修改 `backend/app/db/metrics_store.py`、新 Alembic 迁移与 `backend/tests/test_metrics.py`。

- [x] 用 `pg_support.temp_test_database()` 创建独立库并迁移到 head。脚本提供可配置规模，至少测 1 万、10 万和 100 万条事实记录；种子使用合成租户/项目与封闭标签，写入前调用 `require_test_database()`。
- [x] 每个规模先 `ANALYZE`，记录整体函数调用时间和连续抓取延迟；对 SQL 函数中读取四类表的查询分别执行 `EXPLAIN (ANALYZE, BUFFERS)`。只看 `EXPLAIN SELECT public.study_metrics_snapshot()` 不足以定位函数内部开销。
- [x] 在报告中写明硬件、PostgreSQL 版本、规模、每次耗时、buffer 命中/读取和 15 秒缓存的实测查询频率。以 100 万事实规模下数据库快照 p95 ≤ 250 ms 作为本轮工作目标；达不到时，优先评估增量汇总表或按事实写入更新的固定标签计数，继续保持跨租户汇总权限和封闭标签。
- [x] 若实施增量方案，为回填、并发更新、回滚和与原 SQL 聚合一致性加入 PG 测试；验证 `/metrics` 默认关闭、令牌校验、只输出固定标签。若已达目标，保留现有实现并附执行计划，不引入新迁移。

**验收：** 有可重现实测报告和明确的“保留/优化”决定；任何优化均有数值对照与权限回归。

## 任务 7：历史入口与可重建文件整理

**文件：** 检查 `tools/run_round6_gate.py`、`tools/run_round7_preview.py`、`tools/check_round7_browser.py`、`tools/run_round8_gate.py`、`docs/superpowers/plans/`、`.gitignore`；创建或更新 `tools/README.md`。

- [x] 用 `rg`、CI 配置和 `git ls-files` 建表：当前执行入口、仅被历史文档引用的入口、可重建缓存、业务/审计数据。
- [x] 将确定仅供历史复现的脚本标注为历史入口；若移动文件，给旧路径保留一轮转发脚本并修正文档引用。不要改动 Alembic 迁移、ADR、审计记录或未跟踪的 Trae 材料。
- [x] 对 `output.json`、`pytest_html_report.html` 和缓存只给出可重建说明与忽略规则；不递归清空 `var/`。运行当前门禁入口，确认历史整理没有破坏测试与浏览器流程。

**验收：** 团队能从 `tools/README.md` 找到当前入口；历史证据仍可定位，仓库中没有误删的运行输入。

## 可直接使用的提示词

以下提示词按顺序使用。每条均可作为一次独立任务；在每次任务开始时先读取本计划与审查报告，并保留当前工作树改动。

### 提示词 1：门禁

> 请执行 `docs/superpowers/plans/2026-09-23-architecture-followup.md` 的任务 1。当前 `tools/run_round8_gate.py` 只语法检查 `frontend/app.js`，请覆盖 `api-client.js` 和 `views.js`，并补浏览器控制台及作用域切换/未知结果重试回归。保持所有 HTTP 行为不变。先写能暴露缺口的验证，再实现、跑定向测试和真实预览浏览器检查。保留工作树已有的所有未提交改动；报告命令、结果和剩余风险。

### 提示词 2：组成根

> 请执行本计划任务 2，消除 `backend/app/api` 和 `backend/app/workers` 对 `app.main` 的导入。先写静态失败门，再将 `PlatformState`、`build_platform`、装配助手及其常量移到 `app.platform`，保留 `app.main` 的兼容导出和现有 CLI/Web 入口。验证内存模式导入、三个 worker、导入方向、mypy 和全量测试；不要重置或覆盖现有改动。

### 提示词 3：产品路由

> 请执行本计划任务 3，把 `backend/app/api/product_routes.py` 按会话/计划、资料/知识、学习/证据拆成领域路由与共享请求模型，原文件只聚合路由。先固定 OpenAPI 的路径、方法、状态码和 schema 基准；保持 `select_with_source` 的单事务边界及所有写端点幂等完成顺序。跑产品、资料、学习 API/PG 测试与契约生成检查，列出拆分前后差异。

### 提示词 4：前端状态

> 请执行本计划任务 4，将 `frontend/app.js` 的命令作用域/未知结果重试和项目数据加载拆为独立 hook，保持 `api-client.js` 只负责 HTTP。先补浏览器回归：跨项目/会话迟到响应、同键重试、窄屏；再改脚本加载顺序与门禁。必须保持请求体、幂等键复用、错误提示和既有 UI 行为。不要覆盖当前工作树中的其他未提交文件。

### 提示词 5：工作流运行器

> 请执行本计划任务 5，将 `workflow/runtime.py` 的进程内幂等状态机和预算账户生命周期抽成两个职责明确的组件。保持 HTTP 幂等与 workflow 幂等分离，保持同键并发只执行一次、失败释放、重放追踪 ID、预算原子创建及未知结果语义。先跑现有并发测试作为基线，再分步重构并跑全量门禁；报告接口变化与证据。

### 提示词 6：指标容量

> 请执行本计划任务 6。只在 `pg_support.temp_test_database()` 临时库加载合成数据，测 1 万、10 万、100 万事实规模下 `study_metrics_snapshot()` 的整体延迟和内部各聚合查询的 `EXPLAIN (ANALYZE, BUFFERS)`，写可复现实验报告。不要连接或写入业务库。以 100 万事实下数据库快照 p95 ≤ 250 ms 为工作目标；未达标才实现增量汇总，并证明值、权限和固定标签与现有实现一致。跑指标及 PG 回归。

### 提示词 7：历史整理

> 请执行本计划任务 7。清点当前 CI/部署入口、旧轮次工具、历史文档引用和可重建产物，写 `tools/README.md`。对确定只用于历史复现的脚本保留可追溯入口；若移动，保留旧路径转发并修复引用。不要删除 `.trae/`、`INTEGRATION_SUMMARY.md`、`verify_components.py`、Alembic 迁移、ADR 或整个 `var/`。验证当前门禁仍可运行，并提交一份保留/归档/可重建清单。

## 最终验收

在所有任务完成后，执行并记录：

```powershell
.venv\Scripts\python -m pytest -q --tb=short
.venv\Scripts\python -m pytest backend/tests -m postgres -q
.venv\Scripts\ruff check backend tools verify_components.py
.venv\Scripts\mypy backend/app --ignore-missing-imports
.venv\Scripts\python tools/skills/gen_contracts.py --all --check
node --check frontend/api-client.js
node --check frontend/views.js
node --check frontend/app.js
git diff --check
```

新增的前端脚本也必须加入 Node 语法门；浏览器验收按任务 1 和 4 的记录执行。最后更新 `docs/reviews/2026-09-23-code-architecture-audit.md` 中 A08/A12/A13 与组成根依赖的状态，逐条附文件、测试和性能证据。
