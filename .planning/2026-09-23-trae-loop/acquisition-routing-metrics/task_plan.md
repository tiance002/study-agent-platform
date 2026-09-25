# 6sol ↔ 6luna max 闭环：获取与路由指标

## 目标

由 6luna max 子代理在现有 R9 工作区中补齐资料获取和教学路由的可观测指标；由 6sol 独立审查改动、验证边界，并在必要时形成下一轮 6luna max 修订任务。

## 保护边界

- 当前工作区已有大量未提交改动，包括 R9 产品代码、数据库迁移、组件集成配置和本地 Trae MCP 配置。不得 reset、checkout、clean、stash、提交或重写这些改动。
- 本轮范围只覆盖指标接口/实现、获取 worker、教学路由/provider 的必要埋点、相关测试和少量必要的配置/依赖。执行前须先报告实际计划改动文件；超出该范围先停止并向 6sol 报告理由。
- 不访问生产服务或真实 provider；不部署、不推送、不提交，不使用真实 API 凭据。
- 不记录资料正文、搜索 query、完整 prompt/响应、API key、URL、用户/租户/项目/请求/任务 ID。指标标签必须低基数并来自闭合枚举。
- 优先复用项目已有观测方式。当前扫描未发现统一 metrics 库；不得未经说明增加监控服务或数据库迁移。要新增运行依赖或改变部署拓扑，先提交设计理由并等待 6sol 审查。
- 获取 worker 与教学/Web 进程可能分离。必须如实说明指标能否跨进程聚合；进程内计数不能伪称全局指标。

## 计划步骤

1. **只读勘察**：检查 `backend/app/workers/acquisition.py`、`backend/app/knowledge/fetcher.py`、`backend/app/teaching/service.py`、`backend/app/teaching/routing.py`、现有 provider/预算结算/对账状态，以及应用装配与部署命令；把架构说明写入任务报告。
2. **指标模型**：提出最小机制，覆盖可计数的低基数指标及耗时/字节分布；明确指标所有权、进程聚合方式、暴露/采集方式和 worker 部署适配。不得只添加无消费者的计数变量。
3. **获取埋点**：覆盖真实 fetch 的耗时、接收字节、attempt 和终态（成功/失败/结果未知/待对账）；区分缓存 artifact 恢复与新 fetch，不能把恢复路径计作外部请求。
4. **路由/provider 埋点**：覆盖路由状态和原因、fallback、provider 终态、可用的 input/output token usage、缺失 usage 与 reconciliation 状态。必须从既有权威状态/usage 导出，不能推断缺失 usage 为零，也不能绕过预算和状态机。
5. **安全与测试**：为成功、失败、未知/对账、路由 fallback、usage 缺失、已有 artifact 恢复等路径添加确定性测试；验证标签闭合、数值边界、隐私字段未泄露、获取重试/恢复不重复计数。
6. **提交执行报告**：列出文件、指标名称/标签/单位、运行时如何采集、跨进程限制、测试命令和结果、未解决事项；不要自行扩展下一轮功能。
7. **6sol 审查与下一轮**：6sol 检查 diff、数据来源、进程聚合、信息泄漏、指标重复/漏记、语义一致性和测试；若有问题生成逐文件修订提示再派给 6luna max；若通过，记录结论和下轮建议。

## 验收标准

- 获取耗时和字节只来自实际执行的外部 fetch，缓存恢复不重复记 fetch。
- acquisition attempts、失败和 unknown/reconciliation 与持久化领域状态语义相符。
- 路由 decision/fallback 来自持久化的闭合 `RoutingDecision`；provider usage 来自权威 `TokenUsage`，缺失时明确记录“未报告/未知”。
- 所有计数器/直方图可通过项目实际部署的采集路径取得；worker 与 web 不同进程时行为经过说明和验证。
- 指标没有高基数或敏感标签；测试证明不泄露 query、prompt、响应正文、凭据和资源 ID。
- 目标测试与相关既有回归测试通过；未把 PostgreSQL、浏览器或真实 provider 缺失伪装成通过。
- 未修改批准清单之外的文件；原工作区改动保持原状。

## 第 1 轮状态：调研与架构审查

- [x] 用户选择本轮目标：补齐获取与路由指标。
- [x] 6sol 初步检查仓库、R9 计划和指标现状。
- [x] 6luna max 已收到 brief 并完成只读勘察。
- [x] 6sol 审查架构报告，确认缺少跨进程采集路径是有效阻塞点。
- [x] 用户接受 6sol 推荐的 PostgreSQL 持久化事实 + Web `/metrics` 聚合暴露方案（2026-09-23）。

## 第 2 轮：实现、独立审查与修订

详细执行计划：`task-2-plan.md`。执行 brief：`task-2-brief.md`。

1. **6sol 冻结方案**：获取 attempt 的 duration/response-body bytes 写入 PostgreSQL 唯一事实记录；路由、provider usage、provider outcome 和 reconciliation 优先从现有权威持久状态聚合。Web 通过保护的 Prometheus text exposition endpoint 返回只含数字和闭合标签的汇总值。
2. **6luna max 实现**：在共享现有工作区串行实施，新增后继迁移但不运行迁移；按 brief 先写失败测试，再做最小实现，执行定向回归并写 `task-2-report.md`。
3. **6sol 审查**：核对完整 diff、迁移/RLS/权限、事实唯一键与重试恢复、指标数学语义、隐私边界、endpoint 保护和实际测试输出。不能仅根据代理自述判定通过。
4. **闭环优化**：有明确缺陷时写入逐文件修改项和下一轮 brief，再交给 6luna max 修复并重审；若通过，记录余项和限制并关闭本轮。

## 当前状态

- [x] 第 1 轮只读勘察完成，6luna max 未改产品代码或运行测试。
- [x] 第 1 轮设计审查完成，确认跨进程聚合是正确阻塞项。
- [x] 用户采纳 PostgreSQL + Web endpoint 推荐方案。
- [x] 6sol 已写第 2 轮执行计划与实现 brief。
- [ ] 6luna max 实现、定向测试并提交报告。
- [ ] 6sol 完整审查实现，形成结论或修订 brief。
