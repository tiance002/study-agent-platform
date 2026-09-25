# P9 验收与 PR #2 同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以可复现的浏览器、PostgreSQL 和发布门禁证据收口 P9，并把已完成的八个本地提交安全同步到 PR #2。

**Architecture:** 当前代码的生产装配仍以 `keyword/v1` 为检索基线；这不表示 P9 已部署到 ECS。浏览器验收使用本地一次性预览，性能实验只写 `pg_support.temp_test_database()` 创建的随机临时库；通过这些门禁后更新草稿 PR。pgvector 安装和 reranker 实验另行决策，不与 PR 同步混为一次生产变更。

**Tech Stack:** Python 3.11、pytest、Playwright、React 18、PostgreSQL 16、Alembic 0019、GitHub Actions。

## Global Constraints

- 数据与访问边界优先：租户/项目隔离、RLS、原文引用和预算/幂等语义不得回退。
- 仅针对浏览器证据和 0019 容量证据补缺；不要顺带重构检索或部署路径。
- 保留现有 `.planning/`、`.trae/mcp.json`、`INTEGRATION_SUMMARY.md`、`verify_components.py` 未跟踪内容，归属确认前不删除、不纳入 P9 提交。
- 所有性能写入只在 `study_test_` 前缀的临时库；不连接生产 ECS 执行 DDL，也不安装 pgvector。
- `run_round8_gate.py` 已包含全量与 PG 测试、ruff、mypy、前端语法、契约、浏览器和恢复检查；完成本里程碑时运行一次，失败时按受影响范围复测。
- 当前本地分支比远端领先 8 个提交；PR #2 仍是 Draft，旧远端绿色 CI 不能证明这些提交通过。

## 文件边界

| 文件 | 职责 |
|---|---|
| `tools/check_p9_evidence_browser.py`（新增） | 用已加载的前端组件验证 P9 证据栏标签、旧 run 和窄屏布局；不伪造后端持久化结论。 |
| `tools/run_round8_gate.py` | 把 P9 浏览器检查加入既有发布前总门禁。 |
| `tools/bench_metrics_snapshot.py` | 在现有合成 `teaching_runs` 中填充合法的 0019 检索决策，并分析新增聚合。 |
| `docs/performance/metrics-snapshot-2026-09.md` | 记录 0019 的实际采样、与 0018 的比较及容量决定。 |
| PR #2 描述、`progress.md` | 只在对应证据形成后记录结果，不提前宣称通过。 |

## Task 1：补齐证据栏浏览器验收

**责任角色：** 前端负责人；**目标时间：** 2026-09-25 12:00（北京时间）。

**Interfaces:** 读取 `window.StudyViews.EvidenceRail`；输入与 run HTTP 响应同形的 `activeRun`，输出仅为浏览器 DOM。现有 `tools/check_round8_browser.py` 继续验证真实登录、资料、问答和引用旅程。

- [x] 新增 `tools/check_p9_evidence_browser.py`。（2026-09-24 完成：在预览页 `#p9-evidence-root` 容器中用 `ReactDOM.createRoot` 挂载实际 `window.StudyViews.EvidenceRail`（views.js 末尾已显式暴露），不复制 views.js 文案。）

  ```javascript
  const root = document.createElement("div");
  root.id = "p9-evidence-root";
  document.body.appendChild(root);
  ReactDOM.render(
    React.createElement(window.StudyViews.EvidenceRail, {
      activeRun: run,
      sources: [],
      plan: null,
      view: "conversation",
      onReadCitation: () => {},
      citationReading: null,
    }),
    root
  );
  ```

- [x] 对同一个容器依次渲染：无 `retrieval_decision` 的旧 run；`keyword`；`hybrid`；三个闭集降级原因；未知原因；`routing_decision.query_rewrite_status="fallback"`。（2026-09-24 完成：共 11 个场景，按 Counter 行集合做完整 DOM 文本精确断言；旧 run 不多出标签、三种原因各有中文文案、未知码不原样显示、改写失败与向量回退不混称，另覆盖检索/路由标签并存。）
- [x] 在 390px 视口断言证据栏可见、`document.documentElement.scrollWidth <= innerWidth + 1`；保存截图到 `var/round8-browser-gate/`，检查页面脚本错误。（2026-09-24 完成：截图 `var/round8-browser-gate/p9-evidence-390.png`（var/ 为 gitignore，产物留本地）；收集 pageerror 与 console error，网络层资源失败不计。）
- [x] 在 `tools/run_round8_gate.py` 的浏览器步骤附近加入此检查，保持现有 Round 8 旅程不变。（2026-09-24 完成：接入在 check_round8_browser 之后；Round 8 旅程仅把过时的合并文案断言拆成两条独立标签断言，步骤未动。）
- [x] 启动一次性预览，运行两个浏览器脚本并关闭预览。PowerShell 命令如下；预期分别打印 `P9_EVIDENCE_BROWSER_PASSED`、`ROUND8_BROWSER_PASSED`；失败时只修复可复现的前端行为或测试夹具。（2026-09-24 完成：两者均通过；`check_round8_process_recovery.py` 首跑暴露 Windows 下响应丢弃代理被 RST 复位为 ReadError 的断言缺口，已按夹具修复并单独提交。）

  ```powershell
  $preview = Start-Process -FilePath ".venv/Scripts/python.exe" -ArgumentList @("tools/run_round8_preview.py", "--port", "8008") -WindowStyle Hidden -PassThru
  try {
      .venv/Scripts/python tools/check_p9_evidence_browser.py --base-url http://127.0.0.1:8008
      .venv/Scripts/python tools/check_round8_browser.py --base-url http://127.0.0.1:8008
  } finally {
      Stop-Process -Id $preview.Id -ErrorAction SilentlyContinue
  }
  ```
- [ ] 单独提交浏览器验收与门禁入口，提交信息建议：`test: cover P9 evidence rail states in browser gate`。

## Task 2：重测 0019 指标快照容量

**责任角色：** 后端/数据库负责人；**目标时间：** 2026-09-25 15:00。

**Interfaces:** 只扩充 `tools/bench_metrics_snapshot.py` 的现有 `seed_phase()` 与 `EXPLAIN_QUERIES`；不改变产品指标函数或生产 schema。

- [x] 修改合成 `teaching_runs` 的 `INSERT`，为每条 run 生成合法 `retrieval_decision`：80% `keyword`、10% `hybrid`、10% `degraded`；非降级 `reason_code=''`，降级使用 `vector_index_unavailable`，`policy_version='retrieval-route/v1'`，`ranking_version` 对应 `keyword/v1` 或 `hybrid-rrf/v1`。在种子阶段断言非空决策数等于 run 数、三种模式均出现；避免测到空 JSONB 分组后误称完成 0019 基准。（2026-09-24 完成：按序号取模确定性分配，断言失败即退出；实际分布 1 万=975/125/125、10 万=9975/1250/1250、100 万=99975/12500/12500，全部非空。已知既有种子 JOIN off-by-one（≈0.02% 行被静默丢弃）在 0018 基准同样存在，为可比性未改动，已在报告中注明。）
- [x] 给 `EXPLAIN_QUERIES` 增加与 0019 等价的 `teaching_retrieval_decisions` 聚合。（2026-09-24 完成；计划形状为 Seq Scan + HashAggregate（3 组），与函数内其余聚合同构。）

  ```sql
  SELECT retrieval_decision, count(*)::bigint AS decision_count
  FROM public.teaching_runs
  WHERE retrieval_decision IS NOT NULL
  GROUP BY retrieval_decision
  ```

- [x] 先运行小规模校验：`.venv/Scripts/python tools/bench_metrics_snapshot.py --scales 1000 --repeat 2`；预期临时库创建、快照可读、检索聚合非空，退出码 0。（2026-09-24 完成：退出码 0。）
- [x] 再运行 `.venv/Scripts/python tools/bench_metrics_snapshot.py --scales 10000 100000 1000000 --repeat 20`；保存每规模 p50/p95、`EXPLAIN (ANALYZE, BUFFERS)`、PG 版本和采样环境至 `docs/performance/metrics-snapshot-2026-09.md`。10 万事实规模以既有 250ms p95 目标判断；百万规模若超目标，记录与旧版 666.7ms 的差值并按 TD-001 触发条件决定是否优化，不凭空宣称通过。（2026-09-24 完成，PG 16.4 本地实测：10 万 p95=28.5ms **达标**；100 万 p95=1099.2ms **超标**（旧 666.7ms，约 +65%），主因 jsonb 列使 teaching_runs 行宽 +60% 拖慢函数内全部该表扫描，新增聚合本身约 88ms；按 TD-001 新基线维持不优化，触发条件已更新进报告。）
- [x] 单独提交基准脚本和报告，提交信息建议：`perf: rebaseline metrics snapshot with 0019 retrieval decisions`。（2026-09-24 完成：提交 `afb349d`；浏览器验收为提交 `47809b4`；恢复检查夹具修复为提交 `9b40e81`。）

## Task 3：确认本地文件归属

**责任角色：** 仓库维护者与 Trae 配置所有者；**目标时间：** 2026-09-25 15:00。

- [x] 逐项确认 `.planning/` 是否为需要归档的工作记录；如需共享，筛选可复核结论迁至 `docs/`，否则继续留本地。（2026-09-24 处理：15 个文件为三轮（architecture-followup / code-audit-fixes / trae-loop）工作底稿，正式结论已由 progress.md 与计划文档承载；维持未跟踪，是否归档留维护者决定。）
- [x] 确认 `.trae/mcp.json`、`INTEGRATION_SUMMARY.md`、`verify_components.py` 是否面向团队。团队共享前去除机器绝对路径、核对文档中的安装状态，并单独评审/提交；个人配置保持未跟踪。（2026-09-24 处理：三者均含机器本地信息（绝对路径 / 待复核安装状态 / Trae 工具验证），判定为个人环境配置，维持未跟踪。）
- [x] 用 `git status --short --untracked-files=all` 和秘密扫描记录处理结果；不把以上文件混入 P9 浏览器或性能提交。（2026-09-24 处理：跟踪文件 370 个扫描通过；未跟踪文件定向凭据模式扫描无命中；上述文件均未混入提交。）

## Task 4：发布前总门禁与 PR #2 同步

**责任角色：** 质量负责人、仓库维护者；**目标时间：** 最早 2026-09-25 18:00，门禁未闭合则顺延至下一工作日。

- [x] 用本地预览运行 `.venv/Scripts/python tools/run_round8_gate.py --base-url http://127.0.0.1:8008`，保存 full/PG JUnit 结果，核对跳过原因；预期 `ROUND8_GATE_PASSED`，PG 子集没有 skip。（2026-09-24 完成：`ROUND8_GATE_PASSED`；full 967 通过 / 1 跳过（`test_concurrent_reservation_on_last_slot_has_one_winner[memory]`：并发争抢只在 PG 双连接下有意义，内存锁天然串行——该场景由 PG 子集覆盖）；PG 子集 145 通过 / 0 跳过；JUnit 写入 `var/round8-gate/full.xml`、`postgres.xml`（gitignore，留本地）。）
- [x] 运行 `.venv/Scripts/python tools/migrations/check_migrations.py --require-active`、`.venv/Scripts/python tools/security/scan_licenses.py`、`.venv/Scripts/python tools/security/scan_secrets.py`、`git diff --check`；核对三份契约由总门禁验证。（2026-09-24 完成：迁移 19 条单头 0019；许可扫描通过（2 个 MPL-2.0 弱 copyleft 提示，非阻断）；秘密扫描 370 文件通过；`git diff --check` 由总门禁执行通过；三份契约 `--check` 由总门禁执行通过。）
- [x] `git fetch origin` 后确认远端没有新增提交、基底仍为 `codex/architecture-boundaries-20260922`、待推送文件与提交清单仅为预期范围。若远端前进，先重新审查差异，不使用强推。（2026-09-24 完成：远端 head 仍为 `1349352` 无新提交；PR #2 base 仍为 `codex/architecture-boundaries-20260922`；待推送 12 个提交 = P9 审查修复 4 + 验收（浏览器/性能/恢复夹具）3 + 计划与进度文档 1 + 本轮验收计划文档，均在本计划文件边界内。）
- [x] 上述证据齐备后，推送 `codex/round8-residual-20260922` 到原分支以更新 [PR #2](https://github.com/tiance002/study-agent-platform/pull/2)；更新 PR 描述，列出 0019、P9 修复、浏览器与容量证据，以及未完成的 pgvector/reranker 项。待新 head 的 GitHub CI 成功后再请求代码评审；保持 Draft，合并/部署另作决定。（2026-09-24 完成：`1349352..c490cca` 推送 12 个提交；PR 描述追加「R9 混合检索与 P9 审查修复」「验收证据」「未完成项」三节，原架构四节保留；新 head `c490cca` 的 GitHub CI（gates）1m15s 通过；PR 保持 Draft，未请求评审。）
- [x] 把实际命令、关键结果、跳过原因和新 PR head 记录到 `progress.md`；若门禁失败，只记录已完成的证据与阻断原因，不写“P9 全部完成”。（2026-09-24 完成：见 `progress.md`「2026-09-24 · P9 验收收口与 PR #2 同步」节。）

## 后续独立阶段：pgvector 与 reranker

- **pgvector（数据库/运维负责人，最早 2026-09-28）：** 先在与 ECS 匹配的非生产 PostgreSQL 16.15 环境验证包版本、扩展权限、RLS 查询形状、跨项目同文隔离、索引构建/回填、备份与回退。用扩展后的检索评测集比较 recall@5、MRR/nDCG、证据覆盖、p95 延迟和成本；只有形成可审阅的生产变更方案后，才讨论目标 ECS 安装。生产装配在此之前继续保持关键词基线。
- **reranker（算法/后端负责人）：** 只做离线实验；不接生产开关。评测要包含跨项目、历史版本、同义问题和无关噪声，不以当前 25 条单项目夹具的 1.0 分数代替上线依据。

## 风险与停止条件

| 风险 | 停止条件与处理 |
|---|---|
| 浏览器测试只证明静态标签 | 同时保留 Round 8 真实问答/引用旅程；若 P9 状态无法通过实际 run 响应进入 UI，补端到端用例。 |
| 性能脚本仍测到空决策 | 种子数量或聚合断言不通过即停止容量结论。 |
| 0019 与旧服务版本不兼容 | PR 同步不等于生产发布；部署阶段单独安排迁移、服务切换和有数据时的回退策略。 |
| 远端分支或 CI 状态变化 | 重新获取远端与 PR 状态，保留 Draft；不强推、不将旧 CI 结果当作新 head 证据。 |
