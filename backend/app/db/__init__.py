"""数据库访问层（PostgreSQL 适配器）。

当前包含：

- `settings`：DSN 与角色约定（应用角色与迁移角色必须分开）；
- `session`：连接与**租户上下文事务**（`set_config(..., is_local => true)`）；
- `confirmation_store`：确认记录的 PostgreSQL 实现 —— 审查修复的正式落地，
  单次消费用条件更新实现跨进程原子。
"""
