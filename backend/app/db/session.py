"""数据库会话与租户上下文。

## 为什么用 `set_config(..., is_local => true)` 而不是 `SET LOCAL`

`SET LOCAL app.tenant_id = '...'` 的值只能**拼进 SQL 字符串** ——
tenant_id 哪怕来自服务端令牌，拼接也等于重新打开注入的门。
`SELECT set_config($1, $2, true)` 走参数绑定，没有拼接。

第三个参数 `true` 与 `SET LOCAL` 等价：**事务结束自动清除**。

这一点是连接池安全的根基：连接归还池时如果还带着上一个租户的
`app.tenant_id`，下一个请求就会「继承」别人的身份 —— 这是池化部署里
最阴险的越权方式，而且极难在测试里暴露（它只在两个请求恰好复用同一条
连接时发生）。用 `is_local => true`，这条路径在机制上就不存在。

## 为什么开发适配器不做连接池

本地连接的开销可以忽略，而「每事务一连接」让上下文残留从机制上不可能。
生产换连接池时，`set_config(..., is_local => true)` 的语义不变 ——
这也是为什么把上下文设置放在事务内部而不是连接生命周期里。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from app.db.settings import app_dsn


def connect(dsn: str | None = None) -> psycopg.Connection:
    """打开一条应用角色连接。"""
    return psycopg.connect(dsn or app_dsn())


def set_tenant_context(
    conn: psycopg.Connection, *, tenant_id: str, project_id: str
) -> None:
    """在**当前事务**内设置租户/项目上下文（RLS 策略读取这两个变量）。

    必须已在事务内：`is_local => true` 只作用于当前事务，
    在自动提交模式下调用等于没设 —— 而"等于没设"意味着 RLS 会把
    所有行过滤成零行，错误会在使用处立刻暴露，而不是静默放行。
    """
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
    conn.execute("SELECT set_config('app.project_id', %s, true)", (project_id,))


@contextmanager
def tenant_transaction(
    *, tenant_id: str, project_id: str, dsn: str | None = None
) -> Iterator[psycopg.Connection]:
    """打开一个携带租户上下文的事务。

    上下文用 `is_local => true` 设置：事务提交或回滚后自动清除，
    连接归还时不会残留上一个租户的身份。
    """
    with connect(dsn) as conn:
        with conn.transaction():
            set_tenant_context(conn, tenant_id=tenant_id, project_id=project_id)
            yield conn
