# R9 获取与路由指标：第 2 轮实现计划

> **实施角色：** 6luna max 按 `task-2-brief.md` 执行；6sol medium 负责方案控制、独立审查和决定是否开启修订轮。

**目标：** 建立真实跨 acquisition worker、teaching worker 与 Web 进程的可采集指标路径，使获取 attempt、教学路由、provider usage 和 reconciliation 数据能从 PostgreSQL 持久状态以低基数、无业务内容的 Prometheus 格式读取。

**架构：** PostgreSQL 保存获取请求每次已观测 attempt 的安全数值事实，并以 attempt 唯一键防止重试/恢复重复计数。Web `/metrics` 只读取聚合快照；教学路由、provider 结果和 usage 优先由现有权威持久化表计算，只有确认现有状态不能表达所需事实时，才提出范围受限的最小持久化补充。指标 endpoint 必须默认受保护，不能公开租户或资源维度。

**技术栈：** 当前 FastAPI、SQLAlchemy/PostgreSQL、Alembic 和现有 worker/store adapters。优先使用现有依赖；不引入新服务，不运行 Alembic migration，不访问真实 provider 或生产 DB。

## 全局约束

- 本地代码可新增最小 Alembic migration，接在工作区现有未提交 `0017` 之后（预期编号 `0018`）；不得在任何数据库实际运行迁移。
- acquisition、teaching、Web 的实际进程边界必须通过持久化聚合解决；进程内计数器不得宣称全局可采集。
- 外部 fetch latency 使用 monotonic clock；response bytes 明确定义为 worker 消费的响应正文 bytes，不代表 headers 或 wire bytes。
- 成功 artifact 恢复不产生新 fetch 样本；每个 durable job attempt 最多一条 attempt 事实。
- 路由来自持久 `RoutingDecision`；provider outcome/usage 来自持久 attempt 及 authoritative `TokenUsage`；NULL usage 不是 0；不重派 timeout/unknown。
- 标签值只允许闭合枚举。禁止 URL、域名、tenant/user/project/source/request/run/acquisition ID、模型用户配置、exception 文本、query、正文、prompt、response 或凭据进入输出、标签或日志。
- `/metrics` 只提供 aggregate numeric exposition，不能返回原始记录、内部 ID 或租户切片；未配置访问凭据时必须默认拒绝访问。
- 不改生产状态：不执行迁移、不连接/更改生产数据库、不部署/推送/提交、不调用真实外部 provider。
- 共享工作区中所有当前变更均归用户所有。禁止 `reset`、`checkout`、`clean`、`stash`；禁止格式化、暂存或修改无关文件；不创建缺失这些变更的新 worktree。
- 按 TDD 工作：每个新增行为先写测试并确认预期失败，再实现，再运行新增测试及相关回归。

## 文件职责和预期改动范围

实现代理先核对现有架构和文件，再在报告中确认实际清单。预期范围：

- 新增 `alembic/versions/0018_...py`：获取 attempt 度量事实、必要索引/约束、最小 worker 写入与 Web 聚合读取权限、受限聚合函数；迁移只编写不执行。
- 新增 `backend/app/metrics.py` 和必要的 `backend/app/db/metrics_store.py`：闭合 metric schema、聚合快照读取/渲染及 endpoint 辅助逻辑。若项目既有模块更合适，可复用而不要强行拆文件。
- 仅为实际埋点/装配修改 `backend/app/knowledge/fetcher.py`、`backend/app/workers/acquisition.py`、必要的 acquisition store、`backend/app/teaching/service.py`/store adapter、`backend/app/main.py`、settings/config 和 `.env.example`。
- 在 `backend/tests/` 新增或扩展 acquisition metrics、metrics endpoint/隐私、migration/DB permission 的聚焦测试。
- 不改前端、Trae/Codex/Serena 集成文件、无关产品流程或已有迁移 `0015`–`0017`。只有批准范围内确定必需的配置才可新增；新增运行依赖须先写明不可避免的理由并停止等待 6sol，不可直接锁定/安装。

## 执行步骤

### 阶段 A：实现前核验

1. 阅读 `task-2-brief.md`、上轮 `findings.md`/report/review 和 R9 任务 6；确认共享工作区的用户改动保护边界。
2. 追踪 acquisition claim、fetch、artifact 保存/恢复、attempt_count 和状态完成事务；追踪 PostgreSQL teaching routing/provider usage/reconciliation 的权威表、RLS、role grants、Web adapter 注入方式。
3. 先在 `task-2-report.md` 写清数据映射、实际拟改文件、metric source 和 endpoint 安全模型；只要实现需要新增服务、生产变更、宽权限或未批准范围，就暂停并报告。

### 阶段 B：持久获取事实与安全聚合

1. 先写测试，验证每个真实 fetch attempt 只产生一个带安全 outcome、duration、consumed body bytes 的持久事实；重试 attempt 各自可计数，重复 finalize 不重复写。
2. 测试真实 artifact recovery 不发外网且不新增 fetch histogram sample；无响应正文的失败不得编造 bytes，未观测到 fetch 的 claim 不得编造 latency。
3. 编写最小后继 migration：唯一键绑定 durable acquisition job + attempt number；字段类型、非负值和 outcome 用 DB 约束；查询/索引只服务明确的 aggregate。
4. 实现单调计时与幂等写入，尽量与现有 artifact/outcome 事务一致；对 lease 恢复中未完成的事实明确记录 unknown/未观测语义，不把其伪造成成功 fetch。
5. 提供只返回 aggregate 数值的读取路径。若需 SECURITY DEFINER function，固定安全 `search_path`、撤销 PUBLIC 执行权，只授权所需 app role，并验证函数不返回 ID/业务字段。

### 阶段 C：教学指标与 exposition

1. 将 route decisions、fallback、provider outcomes、tokens、usage missing/unknown、reconciliation entries/pending 映射到现有持久权威状态。清楚区分累计值与当前 gauge；只对有可靠持久来源的指标暴露数值。
2. 先写针对 aggregate/source mapping 的测试：闭合 labels、unknown/null usage、fallback 原因、attempt outcome 和多状态聚合均符合契约；任何资源 ID 与用户正文都不能出现在聚合结果。
3. 实现 Prometheus text exposition：只返回低基数聚合指标；histogram buckets/count/sum 有确定单位与边界。优先复用现有依赖，不可因便利加入新运行依赖。
4. 增加 bearer-token 保护或等价的已存在内部访问保护；默认未配置时 endpoint 不可访问。令牌不出现在响应、异常、日志或标签中；配置占位说明放入 `.env.example`，不得写入实际秘密。
5. 将 endpoint 只装配到 Web app，不让 worker 启动额外监听端口；采集 worker 的事实由 Web 从数据库读取。

### 阶段 D：验收与交接

1. 运行新增确定性测试和相关既有 acquisition/teaching/endpoint 回归；不接真实 provider。若环境凭据不足，单独报告缺失并运行不依赖外部服务的测试，不得报 skip 为通过。
2. 静态检查 migration 顺序、up/down 对称性、权限撤销/授权、闭合 CHECK constraints、索引、endpoint 默认拒绝和所有输出标签。
3. 检查 `git diff --` 只包含本任务必需文件，确认没有 staging/commit/deploy；记录实际命令及原始通过/失败数。
4. 写完整 `task-2-report.md`：文件变更、metric 名称/type/unit/labels/semantics、DB facts 和幂等键、权限模型、fetch recovery/retry 语义、endpoint 授权、隐私测试、所有命令结果、限制和未完成项。返回 `DONE`、`DONE_WITH_CONCERNS`、`NEEDS_CONTEXT` 或 `BLOCKED`。

## 固定指标契约

沿用上轮候选名，若仓库事实证明某项没有安全、可靠的来源，执行代理必须在报告标为未实现/需调整，不能用猜测值补齐：

| 指标 | 类型 / 单位 | 标签 |
|---|---|---|
| `study_acquisition_fetch_duration_seconds` | Histogram / seconds | `outcome`: `succeeded`, `failed`, `unknown` |
| `study_acquisition_response_body_bytes` | Histogram / bytes；正文已被 worker 消费的字节 | `outcome`: 同上 |
| `study_acquisition_claims_total` | Counter / claims | 无 |
| `study_acquisition_jobs_total` | Counter / durable terminal jobs | `outcome`: `succeeded`, `failed`, `unknown` |
| `study_acquisition_jobs` | Gauge / current jobs | `status`: `queued`, `running`, `succeeded`, `failed`, `unknown` |
| `study_teaching_route_decisions_total` | Counter / decisions | `query_rewrite_status`: `disabled`, `applied`, `fallback`; `reason_code`: `local_not_configured`, `local_rewrite_accepted`, `local_unavailable_or_invalid`; `answer_route`: `cloud` |
| `study_teaching_fallbacks_total` | Counter / decisions | `reason_code`: `local_unavailable_or_invalid` |
| `study_provider_attempts_total` | Counter / attempts | `provider`: `openai`, `unknown`; `outcome`: `completed`, `refused`, `dispatch_failed`, `timeout`, `malformed`, `truncated`, `unknown` |
| `study_provider_tokens_total` | Counter / tokens | `provider`: `openai`, `unknown`; `direction`: `input`, `output`; only authoritative reported token usage |
| `study_provider_usage_total` | Counter / attempts | `provider`: `openai`, `unknown`; `state`: `reported`, `missing`, `unknown` |
| `study_reconciliation_entries_total` | Counter / entries | `domain`: `acquisition`, `teaching` |
| `study_reconciliation_pending` | Gauge / pending rows | `domain`: `acquisition`, `teaching` |

Durable database totals must have explicit retention/reset semantics. Do not call a query-derived gauge a monotonic counter without evidence. If the current persistence cannot support historical cumulative reconciliation entries, expose only the reliable pending gauge and document the missing source pending 6sol review.

## 6sol exit review

6sol will independently inspect the diff and task report, then evaluate spec completeness, migrations/permissions/RLS, source-of-truth mapping, deduplication/concurrency, Prometheus semantics, endpoint access protection, privacy, scope and test evidence. Any concrete issue becomes a file-by-file next-round plan and a new Luna brief; implementation is not accepted solely because tests pass or the report says DONE.
