# 开放注册发布与处置门槛

这份运行手册与 `0013` 数据模型同一发布批次交付。它不改变认证契约，只规定谁在什么条件下切换开关。

## 发布顺序

1. 所有实例保持 `STUDY_PLATFORM_REGISTRATION_ENABLED=0`、`STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED=0`，完成备份、迁移演练和健康检查。
2. 先把 `STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED=1` 发布到灰度实例，验证既有密码账号登录、登出和 `logout/all`。
3. 确认 Argon2 队列无持续饱和、认证失败率和 `429` 在基线内，再逐步设置 `STUDY_PLATFORM_REGISTRATION_ENABLED=1`。
4. 付费派发开关与注册开关独立；缺省为关闭。PostgreSQL 部署以 `platform_budget_config`
   单例行为事实源为准（`0013` 的受限函数读取它）。`monthly_cap_micro` 使用整数微单位；
   `NULL` 表示不按平台月度金额拒绝，但仍记录预留与实际用量。配置为有限整数时，
   额度降低只阻止新的付费派发，不释放 `held`/`in_flight` 预留。内存/演练模式才使用
   `STUDY_PLATFORM_PAID_DISPATCH_ENABLED` 与 `STUDY_PLATFORM_MONTHLY_CAP_MICRO` 初始值。

## 告警与动作

- Argon2 队列持续饱和：先降低注册流量或关闭注册，保留密码登录；只有确认密码验证本身有问题时才关闭密码登录。
- 注册量异常上升、单账号/IP 限流集中触发：冻结注册并保留现有账号登录，检查代理信任配置和来源摘要。
- 首次平台预算拒绝：暂停 `paid_dispatch_enabled`，对账 `held`、`in_flight`、provider 账单和当前 UTC 周期；不得重启后自动释放在途预留。

生产预算止损/恢复由受限运维变更完成：在备份当前行后，仅更新
`public.platform_budget_config` 的 `paid_dispatch_enabled` 或 `monthly_cap_micro`；
当产品选择按用户需求计费时可将月度值设为 `NULL`，但不得关闭单次 token、超时、
未知结果对账和 worker 租约边界；
不要直接删除或重置 `platform_paid_reservations`。更新后先用合成运行验证预留、
`held → in_flight → settled` 及 `DISPATCH_FAILED` 释放，再恢复流量。

每条告警必须绑定值班负责人、首个查询、升级时限和关闭条件；阈值在灰度压测后调整，但不能删除告警。

## 无邮箱账号处置

首版不提供自助找回、改用户名或自助注销。密码遗失不允许仅凭用户名手工改 `password_hash`；禁用、导出、删除请求必须通过受理人核验、工单、结果和审计记录。以后若增加管理员重置，必须使用一次性恢复凭证、递增 `security_generation` 并撤销旧会话。

## 回滚

回滚应用包时保留 `0012`/`0013` 表和函数，保留密码会话与凭据表；可先关闭注册，确认既有密码登录仍符合当前安全评估，再决定是否关闭密码登录。
