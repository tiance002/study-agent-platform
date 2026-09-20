# 第六轮计划：教学可靠性收口与真实 Provider

> 实施记录：第六轮已完成。强制 PostgreSQL 门禁与离线 provider 契约均已通过；真实云端冒烟因未提供凭据未执行。

**目标：**关闭第五轮七项发现，完成可审计、可恢复、可核验的教学后端；真实 provider 有独立验收证据。其后再进入普通用户前端。

**架构：**沿用 FastAPI、TeachingService、repository ports、独立 PG worker 角色；保留现有幂等与 RLS。围绕 durable run/attempt 冻结输入及配置，外部调用在事务外，结算与用户可见结果原子提交。

**技术：**Python、pytest、PostgreSQL/Alembic、现有 provider 接口；真实 SDK 的版本和行为在实施时依据官方文档核查并锁定。

## 范围与顺序

第六轮不扩展向量检索、reranker、多模型路由、Agent 工具调用或掌握度推断。关键词检索足以验证首版闭环；先修状态/费用/证据交付，再接真实服务，避免在不可靠链路上扩展功能。

### 任务 1：把验收门从“绿色”改成“证据完整”

- [x] 新增 `tools/run_round6_gate.py` 强制 PG 门禁；PG 不可达时退出非零，不报告通过。
- [x] 全量测试仍使用随机临时库，队列清理前验证测试库；门禁同时执行全量和 `-m postgres` 子集。
- [x] 第五轮回归与第六轮反例映射到 `test_round6_regressions.py`、`test_teaching_repositories.py`、`test_round6_provider.py`。
- [x] 实测：全量 `760 passed, 1 skipped`；PG 子集 `124 passed`。

### 任务 2：冻结状态机并修复失败/恢复路径

- [x] 两个仓储统一 attempt 状态决策；已派发无结果恢复为 `reconciliation_required`，不再次调用 provider。
- [x] 失败 payload 满足 PostgreSQL 非空约束；未知 usage 保留费用敞口。
- [x] 覆盖租约围栏、重复完成、旧 claim、无 usage 与 PG/内存双跑。

### 任务 3：冻结请求并兑现预算上界

- [x] 派发前保存请求快照，历史按消息序号截止，run 冻结 model/prompt/ranking 与输入/输出上限。
- [x] 预留在派发前按完整上下文上界 resize；锁顺序固定为租户后项目；4010 预算反例调用数为 0。
- [x] 恢复重放使用已存快照，不重新检索替换原请求。

### 任务 4：交付可核验引用

- [x] accepted/rejected citations 与 grounding 写入最终结果并通过 HTTP DTO 暴露；SSE 不泄漏请求快照。
- [x] Cookie API 流程覆盖引用回读，混合真假引用与恢复重放有测试。

### 任务 5：真实 Provider 适配器与对账最小流程

- [x] 新增无隐藏重试的 OpenAI Responses HTTP 适配器与 factory；离线本地 HTTP 契约覆盖成功、usage、鉴权、429/5xx。
- [x] 未知网络结果抛出对账所需异常，key、正文与供应商原始错误不进入公开错误。
- [ ] 真实云端冒烟与人工对账入口留到凭据/运维角色到位后；当前明确标记为发布前阻塞，不冒充完成。

## 统一退出门

- [x] 全量后端回归通过；PG 门禁在 PG 可达时无持久化 skip（全量中的 1 个 skip 为非 PG 条件测试）。
- [x] 迁移 `0010 → 0011` 已升级，契约文档同步到 0011；测试库隔离成立。
- [x] Cookie 端到端接口流程与引用回读通过。
- [x] 真实 provider 的离线契约通过；真实联调因无凭据明确阻塞。

## 第七轮预告：普通用户首版

第六轮通过后，做邀请登录、项目/会话、资料摄取状态、教学问答与引用回读、计划任务的实际操作界面。覆盖空/加载/失败/超时/预算不足/待对账状态，以及刷新、断线重连、移动端和多标签页。先沿用关键词检索，不以向量检索作为上线前提。

首版最终发布还需独立的部署门：TLS/生产配置校验、备份恢复演练、迁移回滚策略、健康检查、worker 运维、费用监控、数据删除与隐私说明。能启动前端不等于可向普通用户发布。
