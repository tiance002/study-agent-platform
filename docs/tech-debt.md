# 技术债登记簿

> 按 ship-first 技能 §16 记录 P2/P3 问题：记录而不是立即修，不阻塞当前里程碑。
> TD 编号全局递增，不回收复用。

## TD-001 study_metrics_snapshot() 在百万事实规模下 p95 超出 250 ms 目标

- 状态：已评估，暂缓实施（保留现有 SQL 聚合 + 15 秒缓存实现）
- 影响：100 万事实规模下快照 p95 = 666.7 ms（目标 250 ms）；10 万规模内 p95 = 21.1 ms 不受影响。/metrics 抓取经 15 秒 TTL 缓存，数据库查询频率上限 4 次/分钟/进程，当前不构成用户可感知问题。
- 触发条件：任一事实表（acquisition_jobs / acquisition_fetch_observations / teaching_runs / provider_attempts）接近 50 万行；或生产配置下实测 /metrics 数据库耗时 p95 > 250 ms；或需要把缓存 TTL 降到 5 秒以下。
- 当前临时方案：保留 0018 迁移的 SECURITY DEFINER SQL 聚合函数与 PostgresMetricsStore 的 15 秒缓存。
- 为什么现在不修：达标需增量汇总表（新迁移 + 获取/教学写路径计数更新 + 回填 + 并发 + 回滚 + 一致性 PG 测试），blast radius 大；当前真实数据量级在 10 万以内且缓存已把数据库占用钳制在 ~4.4% 单核；属 ship-first §11"极限性能优化需真实 workload"范畴。
- 后续验证方法：`tools/bench_metrics_snapshot.py --scales 10000 100000 1000000`（详见 `docs/performance/metrics-snapshot-2026-09.md`）。
- 建议处理阶段：Phase D（Latency 专项调优）；过渡项可先做函数内合并重复扫描（预计降至 350–400 ms，仍不达标，仅在需要小幅改善时单独评估）。
- 标记：ship-first、architecture-followup 任务 6

## TD-002 GitHub CI 覆盖面窄于仓库宣称的发布总门禁

- 状态：登记，暂缓（2026-09-25 审查 A08）
- 影响：`.github/workflows/ci.yml` 只跑 pytest / PostgreSQL / ruff / mypy / skills / contracts / migrations / licenses / secrets；**不含**前端 `node --check`、浏览器回归（round8 / p9 / library）、进程恢复检查、`git diff --check`。这些只存在于本地 `tools/run_round8_gate.py`，因此"某个提交本地跑过总门禁"常被误读为"PR 每次 push 都自动验证"。
- 触发条件：任何一次"新 head 的 CI 绿 = 发布证据"的判断之前；或再出现"本地门禁通过但 CI 未覆盖该维度"的事故。
- 当前临时方案：里程碑前在全新本地预览实例上单次运行 `tools/run_round8_gate.py`，把 JUnit 与浏览器结果留档（`var/`，gitignore）。
- 为什么现在不修：属于工程门禁扩展而非用户路径阻塞；本轮用户明确选择范围（后端核心 + 前端 + 知识库），CI 分层（fast gates / integration gates）需要单独设计与时长预算评估。
- 后续验证方法：CI 增加 `node --check`、`gen_contracts --check`、`git diff --check` 常驻；浏览器与进程恢复作为 merge/夜间集成作业；改动后用一次 PR push 验证两层作业均触发。
- 建议处理阶段：Milestone A 收口前后的工程卫生批次（与 TD-003 同批）。
- 标记：2026-09-25 审查 A08

## TD-003 开发工具与仓库卫生（机器绑定、假绿脚本、双份政策文档、历史堆积）

- 状态：登记，暂缓（2026-09-25 审查第七/八/九/十/二十一/二十二节）
- 影响：①`.trae/mcp.json`、`.codex/config.toml` 写死 `E:/codex_workspace/study-plan` 机器绝对路径，他人 clone 后 Trae/Codex 直接失效；②`.trae/mcp.json` 用 `@latest`（更有 `superpowers-mcp` 未锁版本）与 Codex 侧固定版本纪律不一致，MCP 可读代码并执行工具，供应链风险偏高；③`verify_components.py` 在"命令失败且 stderr 含 error 但同时含 INFO"时返回 True——会产出假绿，应合并到真正做 MCP session + tool call 的 `.codex/scripts/verify-integrations.py`；④`.agents/skills/study-plan-fast-development` 与 `.trae/skills/ship-first` 是同一份开发政策的两套正文，长期会导致两个代理按不同规则执行；⑤根目录 `progress.md`/`findings.md`/`task_plan.md` 持续累积过期状态与机器路径，`docs/reviews/2026-09-23-code-architecture-audit.md` 仍混合历史快照与后续修复状态，容易被当作最新结论。
- 触发条件：团队第二人接入开发环境之前；或任一次"文档结论互相矛盾导致返工"之后。
- 当前临时方案：已知内容按本文档与 `progress.md` 为准；`.trae/mcp.json` 已在 c28817e 入库（含机器路径），暂按个人环境使用。
- 为什么现在不修：与产品路径无关；政策正文合并（canonical policy + 各代理 adapter）、文档归档结构（`docs/archive/2026-09/`）与审查快照标注属于一次性整理，需要单独评审以免破坏他人工作流。
- 后续验证方法：`mcp.example.json` + 本地生成配置可被新机器一键启动；`verify_components.py` 假绿用例（失败+INFO）有回归测试或脚本被删除；两份政策文档指向同一正文；`docs/reviews/*` 顶部标注 SUPERSEDED/HISTORICAL。
- 建议处理阶段：工程卫生批次（与 TD-002 同批）。
- 标记：2026-09-25 审查（工具卫生组）

## TD-004 架构延后项（PlatformState 瘦身、legacy ChunkIndex、单 worker audit sink、向量后端、前端 hooks）

- 状态：登记，数据触发后再做（2026-09-25 审查第五批）
- 影响：`PlatformState` 承载数十个成员并作为 Service Locator 被所有路由使用，依赖隐式化；`platform.py` 约 565 行同时负责装配/适配器选择/启动检查/演示数据；`main.py` 仍兼容 re-export 含私有名的旧符号，延缓旧架构退出；`ChunkIndex`（workflow legacy）与 `KnowledgeRepository`（产品检索）两套检索并存；生产被限制为单 Web worker（文件 AuditSink 哈希链无法多进程安全写），横向扩展前必须把权威审计链迁到 PostgreSQL 或改为单写入者；混合检索框架已就位但生产装配仍是 `keyword/v1`（无 embedding provider / vector index）；`frontend/app.js` 仍承担认证/项目/会话/教学轮询等多项职责，适合抽 `useAuthSession`/`useWorkspace`/`useTeachingRun`/`useSources`。
- 触发条件：①单 worker 成为压测或容量瓶颈；②两套检索导致"同一 bug 修两遍/指标不知道测哪套"；③学习闭环 MVP 稳定且需要应用服务层收敛；④生产启用 pgvector 时。
- 当前临时方案：保持现状，按里程碑策略不提前重构；`/metrics`、引用与隔离不变量已由测试锁定。
- 为什么现在不修：属 review 第五批"等 MVP 真正可用后再处理"；单 worker 目前是安全保护而非缺陷；向量后端需要非生产环境先验证扩展与回填。
- 后续验证方法：拆分后 `test_import_direction` 与全部 PG E2E 仍通过；审计链多进程写入有一致性测试；生产装配切换到 hybrid 前先在冻结评测集上比较 recall@5/MRR/证据覆盖/延迟/成本。
- 建议处理阶段：Milestone C（数据驱动优化）及之后。
- 标记：2026-09-25 审查（架构组）

## TD-005 知识库已知边界（URL 关联、幂等命中、媒体类型收窄）

- 状态：登记（2026-09-25，知识库 v1）
- 影响：①URL 类库材料「关联到项目」时走抓取路径，响应 `ingestion_job_id` 为 `null`，UI 需按候选/下载状态跟踪而非直接轮询摄取任务；②`POST /library/sources` 幂等命中既有 `(tenant, principal, identity_hash)` 时返回既有记录并**忽略本次 `content`**，如需补原文必须显式调 `POST /library/sources/{id}/content`；③库里 `media_type` 是自由元数据（上限 100），入队摄取时会收窄为闭集（未知类型按 `text/plain`），入库语言固定 `zh`。
- 触发条件：真实用户反馈"关联后资料一直没准备好"或"补传的原文没生效"；或需要支持非中文资料/更多格式。
- 当前临时方案：面板文案说明关联后资料需处理完成后才可参与引用；补原文走显式上传。
- 为什么现在不修：MVP 阶段 URL 类材料复用抓取路径是既有能力的最短闭环；媒体类型闭集收窄是摄取层的既有约束，不为知识库单独放宽。
- 后续验证方法：为"URL 关联→抓取完成→检索命中"补端到端 PG 用例；若支持多语言，补 `language` 参数与测试。
- 建议处理阶段：Milestone B（真实场景验证）后按反馈处理。
- 标记：知识库 v1

## TD-006 自报裁决记账（NONE 仍计入 evidence_count 与独立组）

- 状态：登记（2026-09-25，A03 最小改动）
- 影响：自报证据裁决改为中性（`direction = NONE`）后，它仍会计入证据计数与 `independence_group` 去重，只是不再抬升独立水平、不让任务 `verified`。若将来引入"多来源证据加权"，自报的权重语义需要显式定义。
- 触发条件：引入评分器/多来源证据融合，或掌握度算法改为按证据计数加权时。
- 当前临时方案：投影器只对 `POSITIVE` 提升独立水平；自报保持可回放、可审计。
- 为什么现在不修：A03 要求的是"自报不得成为已验证/正向"，最小改动已满足；改动记账语义会牵动掌握度不变量与历史事件回放。
- 后续验证方法：引入加权时补"自报权重"专项测试与历史事件兼容性用例。
- 建议处理阶段：掌握度/评估能力增强时。
- 标记：2026-09-25 审查 A03

## TD-007 前端 vendor 单文件体积（antd UMD 1.43MB 未分包）

- 状态：登记，暂缓（2026-09-25）
- 影响：无构建步骤下 antd UMD 与 dayjs 以整包 vendor 进 `frontend/vendor/`（antd.min.js 约 1.43MB），首屏需下载完整组件库；对局域网/本地预览无感，对公网首访有影响。
- 触发条件：公网部署且首屏体积成为可感知问题；或引入按需加载/构建步骤的决策。
- 当前临时方案：同源静态文件 + 浏览器缓存；未启用 CDN。
- 为什么现在不修：无构建步骤是当前前端架构约定（门禁脚本、资源清单契约都建立在此之上），引入 bundler 会改变整个前端交付方式，属独立决策。
- 后续验证方法：若引入按需加载，记录首屏传输体积对比与门禁脚本迁移结果。
- 建议处理阶段：性能/交付方式专项。
- 标记：前端重构 v1
