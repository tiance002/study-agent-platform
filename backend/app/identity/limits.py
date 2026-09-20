"""认证相关的硬限制（内存与 PostgreSQL 适配器共同遵守）。

把上限写成**一份代码常量 + 一道数据库约束**，而不是只写在配置里：

- 应用层（`DeploymentSettings` / 内存适配器）在签发前拒绝超限请求；
- 数据库层（0004 迁移的 `CHECK` 与兑换函数守卫）独立再判一次 ——
  即使应用配置错误、或有人绕过应用直接调用函数，
  「100 年会话」也无法落库。

两边引用同一个数值的不同表现形式（Python timedelta / SQL interval），
改上限时必须同时改这两处与迁移 —— 契约测试会钉住实际生效值。
"""

from __future__ import annotations

from datetime import timedelta

#: 会话有效期的**硬上限**：30 天。
#:
#: 默认会话只有 8 小时（`main.DEFAULT_SESSION_TTL`）；30 天是给
#: 「记住我」类配置留的天花板，不是默认值。会话是可撤销的数据库行，
#: 但超长有效期仍然放大 cookie 被复制后的风险窗口。
#:
#: ⚠️ 与 0004 迁移里的 `interval '30 days'` **必须一致**，
#: 由 `tests/test_auth_hardening.py` 对数据库实测钉住。
MAX_SESSION_TTL = timedelta(days=30)
