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

from app.db.settings import app_dsn, worker_dsn


def connect(dsn: str | None = None) -> psycopg.Connection:
    """打开一条应用角色连接。"""
    return psycopg.connect(dsn or app_dsn())


def connect_worker(dsn: str | None = None) -> psycopg.Connection:
    """打开一条 **worker 角色**连接。

    ⚠️ 队列的跨租户可见性只授予 `study_worker`（0008 迁移的 `TO study_worker`）。
    用应用角色连接去 `claim_next` 会一条任务也看不见 —— 而症状是"队列空了"，
    完全不指向真正原因。所以"用哪个角色"这件事必须在**这里**唯一定义，
    不要在调用点各写一次 `psycopg.connect(...)`。
    """
    return psycopg.connect(dsn or worker_dsn())


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


def set_principal_context(
    conn: psycopg.Connection, *, tenant_id: str, principal_id: str
) -> None:
    """在当前事务内设置租户/主体上下文。

    `user_sessions` / `http_idempotency` 的策略是「租户 + 主体」叠加：
    只设租户时，同租户的另一个主体的会话行依然不可见 —— 这正是
    会话仓储（`get_live` / `revoke`）需要的隔离级别。
    """
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
    conn.execute("SELECT set_config('app.principal_id', %s, true)", (principal_id,))


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


@contextmanager
def worker_transaction(
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    principal_id: str | None = None,
    dsn: str | None = None,
) -> Iterator[psycopg.Connection]:
    """**worker 角色**的连接 + 可选租户/项目上下文。

    为什么上下文是可选的：`claim_next` 必须**先发现"哪个租户有活干"**，
    才可能建立任何租户上下文 —— 那正是这条路径存在的理由。而它之后的
    三步（`load_document` / `complete` / `fail`）都带着任务自带的
    租户 + 项目，必须收窄到该项目内。

    把这两种形状收在一个助手里的好处是"worker 用哪个角色"只有一处定义：
    认领拿不到任务、或者落定被 RLS 拒绝，都不会退化成"某个调用点少写了一行"。
    """
    with connect_worker(dsn) as conn:
        with conn.transaction():
            if tenant_id is not None and project_id is not None:
                set_tenant_context(conn, tenant_id=tenant_id, project_id=project_id)
            if principal_id is not None:
                # 结算路径的主体上下文：worker 是在**替某次运行的提问者**落定，
                # teaching_runs 的标准策略因此能命中行（不是伪造身份 ——
                # principal_id 来自运行行，运行行只能被 worker 角色合法认领到）。
                conn.execute(
                    "SELECT set_config('app.principal_id', %s, true)", (principal_id,)
                )
            yield conn


@contextmanager
def tenant_only_transaction(
    *, tenant_id: str, dsn: str | None = None
) -> Iterator[psycopg.Connection]:
    """只带租户、不带项目的事务。

    适用范围要认清：`projects` 的**读取**策略是成员感知的（EXISTS 子查询
    读 `app.principal_id`），列出/读取项目必须用 `principal_transaction`。
    本助手用于**写入**供给动作 —— 创建项目（此时还没有 project_id 可设）、
    写授权行（`project_grants` 是租户级策略）。
    """
    with connect(dsn) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            yield conn


@contextmanager
def principal_transaction(
    *, tenant_id: str, principal_id: str, dsn: str | None = None
) -> Iterator[psycopg.Connection]:
    """租户 + 主体级事务。会话表（`user_sessions`）的读写走这里。"""
    with connect(dsn) as conn:
        with conn.transaction():
            set_principal_context(conn, tenant_id=tenant_id, principal_id=principal_id)
            yield conn


@contextmanager
def full_transaction(
    *,
    tenant_id: str,
    project_id: str,
    principal_id: str,
    dsn: str | None = None,
) -> Iterator[psycopg.Connection]:
    """租户 + 项目 + 主体三层上下文的事务。

    第 3 轮的 `task_submissions` / `diagnoses` 策略同时约束三个维度
    （铁律 33：有主体列就必须约束主体维度）—— 少设一层，写入直接被
    RLS 拒绝，而"查不到"的错误信息完全不指向真正原因。
    """
    with connect(dsn) as conn:
        with conn.transaction():
            set_tenant_context(conn, tenant_id=tenant_id, project_id=project_id)
            conn.execute(
                "SELECT set_config('app.principal_id', %s, true)", (principal_id,)
            )
            yield conn
