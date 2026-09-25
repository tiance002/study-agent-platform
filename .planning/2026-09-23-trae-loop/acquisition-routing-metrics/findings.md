# 调研发现：获取与路由指标

## 仓库现状

- 项目计划：`docs/superpowers/plans/2026-09-22-round-9-material-acquisition-and-routing.md` 第 6 项明确需要 fetch latency、bytes、attempt、provider usage、route decision、fallback 和 reconciliation 指标，并要求正文、API key、完整 prompt 默认不进入日志。
- 未发现已有统一 Prometheus/OpenTelemetry metrics 实现或后端依赖；`pyproject.toml` 的运行依赖为 FastAPI、Uvicorn、Pydantic、PyYAML、argon2-cffi。
- acquisition worker 在 `backend/app/workers/acquisition.py`；`FetchResult` 定义在 `backend/app/knowledge/fetcher.py`；worker 将成功原文保存为 `AcquisitionArtifact`。存在恢复已保存 artifact 的路径，应避免恢复重跑造成 fetch 指标重复。
- 教学服务在 `backend/app/teaching/service.py`，当前已有 `RoutingDecision`、provider `ProviderResult`/`TokenUsage`、预算结算和 reconciliation 状态。provider 缺 usage 与零用量语义不同。
- Web/教学与外部获取 worker 可独立运行；单纯模块全局变量不构成跨进程汇总。
- 当前工作区有大量预存且未提交的产品代码、测试、迁移和组件配置。Trae 必须限定改动，不得清理或重置。

## 执行代理与环境

- 本机没有可用的个人版 Trae 子代理接口。官方套餐页将 CLI 列为企业旗舰版权益，用户不能使用企业版，因此取消了 CLI 登录尝试并移除该企业版 CLI 二进制。
- 用户现在明确选择 Codex 内部子代理 6luna max 执行、由 6sol 规划和审查；本轮按此方式派单。
- 当前为普通 Git checkout，分支 `codex/round8-residual-20260922`。大量 R9 实现处于未提交状态，因此不能用一个只从 HEAD 创建、缺少这些文件的 worktree 执行；6luna max 将在共享工作区串行工作，严格遵守保护边界。

## 待 6sol 审查的架构决策

Trae 必须先识别采集端/worker 的进程边界，并提出最小、可运维的采集方案。没有实际采集路径、跨进程状态却声称“全局指标”属于不通过；引入数据库迁移或部署服务不在已批准首轮范围，须先说明并暂停等待审查。

## 已确认的第 2 轮设计（2026-09-23）

- 用户接受 6sol 推荐的 PostgreSQL 持久化事实 + Web `/metrics` 聚合暴露方案。此选择批准本地代码新增最小后继 Alembic migration 与 metrics endpoint；不批准执行数据库迁移、访问生产数据库、部署或推送。
- 获取 fetch duration 与已消费 response-body bytes 是现有 artifact/job 表中缺少的事实，必须按持久化 acquisition attempt 记录并用稳定唯一键去重。artifact 恢复不产生 fetch observation；重试按不同 attempt 编号计数。
- teaching routing、provider attempts、authoritative TokenUsage、usage missing/unknown 和 reconciliation 先从已有 PostgreSQL 权威状态聚合；仅当确实缺少历史事件事实时，才提出受限且幂等的最小新增持久事实，不能另建 provider 生命周期或把 NULL 解释为零。
- `/metrics` 必须返回低基数闭合标签，且默认不向公网裸露；应采用运行时可验证的访问保护。聚合读取不得暴露任一原始资源 ID、租户维度或业务内容。
- 现有共享工作区包含大量未提交 R9 变更和尚未提交的 `0015`–`0017` 迁移；第 2 轮迁移应接在当前链末尾（预期 `0018`），不能重写这些文件或改动无关文件。
