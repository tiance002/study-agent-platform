# 第 2 轮实现报告：持久化获取与路由指标

## 状态与阶段记录

**当前状态：实现尚未开始；阶段 A 只读勘察已完成并已记录。** 本报告先于产品代码修改创建，之后将追加 TDD、验证和最终文件范围证据。

开始时分支为 `codex/round8-residual-20260922`。工作区已有大量 R9 staged、unstaged 与 untracked 用户改动，包括 acquisition/teaching store、worker、Web、迁移 `0015`–`0017` 及其测试；这些文件在本轮视为只读基线，后续仅按本清单逐项改动必要部分，不回写/格式化无关改动。未发现仓库内 `AGENTS.md`。本轮不会切分支、建 worktree、暂存、提交、部署或运行 migration。

## 勘察结论与实际来源

- acquisition worker 在 `backend/app/workers/acquisition.py` 独立运行。`claim_next` 在 `backend/app/db/acquisition_store.py` 中给 `acquisition_jobs.attempt_count` 加一并签发 lease；worker 随后先查 `source_fetch_artifacts`。命中 artifact 时走恢复摄取，不再调用 fetcher；未命中时才执行 HTTP 获取。成功 artifact 与 job settle 当前分属不同事务。
- `FetchResult.content` 是 worker 完整消费的 response body；headers/wire bytes 未计入。`fetch_url` 已以 `time.monotonic()` 管理 deadline，但未保留本次调用耗时。retryable `FetchPolicyError` 表示结果不确定，不能改标成功；异常路径不提供已消费的 partial-body 长度，因此 bytes 只能在有完整 `FetchResult` 时记录。
- 现有 `acquisition_jobs` 权威保存 durable job 状态和 claim 次数，状态 CHECK 闭合为 `queued/running/succeeded/failed/unknown`，最多三次 attempt。`source_fetch_artifacts` 按 acquisition 唯一，包含正文、租户/项目 ID 与墙钟 `fetched_at`，不得把其恢复路径计作新 fetch。
- 教学 `teaching_runs.routing_decision` 由 `0017` 以 JSONB/CHECK 保存闭合路由字段；`provider_attempts` 是每个 run 至多一条的持久 attempt，状态闭合为 `dispatched/unknown/completed/failed`，权威 token 列允许 NULL。NULL 不可按零求和。教学事件表含 append-only reconciliation 事件；acquisition 目前没有历史 reconciliation event 表，不能为它伪造累计 entries。
- 现有 DB roles 为 `study_app` 与 `study_worker`，均不得绕过 RLS。`study_worker` 具有 acquisition/teaching 工作队列操作权限；Web 的 `study_app` 无法直接查询跨租户数据。拟用受限 `SECURITY DEFINER` 聚合函数提供只含闭合标签与数值的 snapshot：固定 `search_path=pg_catalog`、所有业务表名 schema-qualified、撤销 `PUBLIC` 执行，仅授权 `study_app` 执行；metrics fact 表不给 `study_app` 直接表权限。事实表继续 FORCE RLS，并只允许 worker 在事务租户/项目上下文下写入。
- 现有 `/metrics`、Prometheus exposition 或 metrics 依赖均不存在；本实现不增加运行依赖。endpoint 使用单独 bearer credential，凭据未配置时默认拒绝；配置从环境载入，不写入响应/标签/日志。

## 实际拟修改文件（代码开始前清单）

1. 新增 `alembic/versions/0018_acquisition_metrics.py`：添加按 `(acquisition_id, attempt_number)` 唯一约束的数值 fetch observation、约束与索引；新增只读聚合 snapshot 函数及 `PUBLIC`/`study_app`/`study_worker` 最小 grants。只写迁移文件，不执行迁移。
2. 新增 `backend/app/metrics.py` 和 `backend/app/db/metrics_store.py`：固定 metric schema/labels、纯 exposition 渲染与 PostgreSQL 聚合 snapshot adapter。
3. 修改 `backend/app/knowledge/acquisition_ports.py`、`backend/app/db/acquisition_store.py`、`backend/app/knowledge/memory_acquisition_store.py`、`backend/app/workers/acquisition.py`：真实 fetch 开始时写入 durable unknown observation；fetch 返回/安全失败时以 monotonic elapsed 更新该唯一 attempt，完整 `FetchResult` 才写 body bytes；artifact recovery 不启动观测。
4. 修改 `backend/app/db/teaching_store.py` 及必要的 teaching service/port/model：仅补持久 source 缺少的 provider family 闭合事实；路由/provider/usage/reconciliation 指标从其权威 PostgreSQL 行聚合，不创建第二套 provider 状态机。
5. 修改 `backend/app/deployment.py`、`backend/app/main.py` 和 `.env.example`：装配 metrics store、受保护的 `/metrics` 入口及空白 token 配置说明。
6. 新增聚焦 `backend/tests/` 回归，覆盖 attempt 幂等、retry/recovery、聚合闭合 labels、NULL usage、默认拒绝和隐私；优先扩展 acquisition worker、teaching repositories 与 deployment 测试，不改迁移 `0015`–`0017`。

如果实际实现需要上述范围之外的新服务、专用 DB role、直接扩大 `study_app` 表权限或放宽 RLS，将停止并改为 `NEEDS_CONTEXT`。开始修改前还需完成 provider outcome/family 的权威字段映射核验。

## 指标来源初步映射

- `study_acquisition_fetch_duration_seconds` / `study_acquisition_response_body_bytes`：新 fetch-attempt observation 表；duration 只在执行真实 fetch 时按 monotonic 计时，body bytes 只在完整响应正文由 worker 消费后有值。
- `study_acquisition_claims_total`、`study_acquisition_jobs_total` 与 `study_acquisition_jobs`：从 `acquisition_jobs` 的 durable attempt_count/终态/current status 聚合，Counter 是保留行上的累计视图，删除或归档重置语义需在完成报告说明。
- `study_teaching_route_decisions_total` 与 `study_teaching_fallbacks_total`：由 `teaching_runs.routing_decision` 的四字段组合聚合；标签仅 `disabled/applied/fallback`、三个 `reason_code` 与固定 `answer_route=cloud`。
- `study_provider_attempts_total`、`study_provider_tokens_total`、`study_provider_usage_total`：由 `provider_attempts` 的闭合 status、持久 provider family、NULL/non-NULL authoritative token 字段聚合；未知 status/family 映射为 `unknown`，未观测 usage 不记为零 token。
- `study_reconciliation_pending`：现有 acquisition `unknown` jobs 与 teaching `reconciliation_required` runs 当前数量。`study_reconciliation_entries_total` 只在每个 domain 都有可靠历史来源时暴露；若 acquisition 不具备历史记录，将按计划省略该指标并说明原因。

## 报告更新约定

后续在此文件追加每次 RED/GREEN 证据、所有实际测试与静态检查命令、migration/RLS 静态核查、结束时的文件范围、metric 类型/单位/label 全值语义、权限和默认拒绝实测、隐私覆盖、保留/reset 语义、限制与需 6sol 决策项。
