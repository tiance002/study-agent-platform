"""测试公共夹具。

约定：
- 每个测试使用独立临时目录与全新平台实例，互不干扰。
- 身份一律通过**真实 HTTP 注册/登录**取得（`POST /auth/register` +
  `POST /auth/login`），不再有仓储直发会话或自签 Bearer 的旁路 ——
  绕过认证会让"客户端自报身份"以另一种形式复活。
- `cookie_project` 是 **cookie 主路径**的共享起点：注册（自动建默认项目）→
  取默认项目 id。摄取、检索、教学等用例共用它。每个文件各抄一份登录舞蹈，
  正是某一天两份会分叉的地方（比如一处忘了断言注册状态码），
  而分叉的结果是"有一个文件的身份根本不是真的"。
- `auth_headers` 注册一个真实账号并返回其 **Cookie 请求头**（含同源 Origin）；
  `demo` 与 `auth_headers()` 指向**同一个**注册身份。
- `pg_database` 是**PostgreSQL 测试的强制前置**：整场会话跑在一个随机临时库上，
  结束即删除。业务库里的在途任务不是测试的耗材（见 `pg_support.py` 的模块 docstring）。
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pg_support
import pytest
from app.identity.cookie_auth import SESSION_COOKIE_NAME
from app.main import build_platform, create_app
from fastapi.testclient import TestClient

SAME_ORIGIN = "http://testserver"

#: 注册夹具使用的口令（6–12 码点策略内的合法值）。
DEFAULT_TEST_PASSWORD = "test-pass-1"


@pytest.fixture(scope="session", autouse=True)
def pg_database() -> Iterator[pg_support.TestDatabase | None]:
    """整场会话共用一个**随机临时库**，并把 DSN 环境变量指向它。

    为什么是会话级而不是逐用例：建库要跑一遍全部迁移（秒级），
    逐用例建库会把测试时间乘以用例数；而用例之间的互相干扰由
    「每个用例用随机 id 建自己的租户/项目」解决，不靠换库。

    为什么必须换库（而不是只用随机 id 就够）：`claim_next` 是跨租户的
    系统级操作，测试为了独占队列必须排空**全库**未终态任务 ——
    在业务库上做这件事就是终结用户的任务，而且测试全绿看不出来。

    ⚠️ **`autouse` 不是图省事，是必需的**：环境变量是本会话的全局状态。
    如果某个模块 opt-in、另一个模块不管，就会出现"模块级夹具（播种租户）
    连业务库、用例体连临时库"的分裂 —— 表现是"单独跑绿、一起跑红"，
    而根因（环境变量在这一场里翻转过）从失败信息里完全看不出来。

    PG 不可达时 yield `None`：模块级的 `skipif` 负责跳过 PG 用例，
    内存用例不受牵连。
    """
    if not pg_support.reachable():
        yield None
        return

    database = pg_support.create_test_database()
    saved = {name: os.environ.get(name) for name in pg_support.DSN_ENV_VARS}
    os.environ.update(database.env())
    try:
        yield database
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        pg_support.drop_test_database(database)


@pytest.fixture(autouse=True)
def _reset_pg_rate_limits(pg_database) -> Iterator[None]:
    """共享临时库上的认证限流桶会跨用例累积：每个用例前清空，保持用例独立。

    内存装配每次都是全新限流器；PostgreSQL 装配把计数写进
    `auth_attempt_counters`，不清理会让后续用例（注册限额 5 次/窗口）
    意外撞 429 —— 那是夹具耦合，不是被测行为。
    """
    if pg_database is None:
        yield
        return
    import psycopg

    with psycopg.connect(pg_support.migration_dsn()) as conn:
        conn.execute("TRUNCATE public.auth_attempt_counters")
        conn.commit()
    yield


@pytest.fixture
def racy_scheduling():
    """把 GIL 的线程切换间隔压到最小，让竞态真的有机会发生。

    默认间隔是 5ms，而「读 → 判断 → 写」这种临界区只有微秒级。
    默认设置下线程几乎不可能被切到中间，于是**一个并不原子的实现
    也能稳定通过并发测试** —— 它给出的信心是假的。

    这不是推测，是量出来的（同一份并发脚本，8 线程，各 100 轮）：

        默认 5ms 间隔 ：无锁实现 0/100 轮穿透
        压到 1μs 间隔 ：无锁实现 **92/100** 轮穿透，加锁实现 0/100

    所以并发测试必须显式制造切换机会。用完恢复，避免影响其它测试。

    放在 `conftest.py` 而不是某个测试文件里：它已经是**多处复用的基建**
    （确认消费、审计追加、幂等占用、预算账本都要用）。散在单文件里，
    新写的并发测试很容易忘了用它 —— 那正是"假并发测试"的来源。
    """
    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(original)


@pytest.fixture
def platform(tmp_path):
    """一个全新的平台实例（内存适配器 + 临时审计目录）。"""
    return build_platform(var_dir=tmp_path)


@pytest.fixture
def client(platform):
    return TestClient(create_app(platform=platform))


@dataclass(frozen=True)
class RegisteredAccount:
    """一个通过真实 HTTP 注册得到的账号身份与可用请求头。"""

    username: str
    password: str
    principal_id: str
    tenant_id: str
    project_id: str
    cookie: str
    headers: dict[str, str]


def _register_http(session_client: TestClient, *, password: str | None = None) -> RegisteredAccount:
    """通过 `POST /auth/register` 真实注册一个账号。

    注册会原子地创建租户、主体、凭据、首个会话与默认项目；返回的身份
    全部来自服务端响应（`principal_id` / `default_project_id`）与回读的
    `/me`，没有任何一处是测试自己拼出来的。
    """
    username = "u" + uuid.uuid4().hex[:10]
    password = password or DEFAULT_TEST_PASSWORD
    response = session_client.post(
        "/auth/register",
        json={"username": username, "password": password},
        headers={"Origin": SAME_ORIGIN},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    cookie = session_client.cookies.get(SESSION_COOKIE_NAME)
    assert cookie, "注册必须种下会话 cookie"
    headers = {"Cookie": f"{SESSION_COOKIE_NAME}={cookie}", "Origin": SAME_ORIGIN}
    me = session_client.get("/me", headers=headers)
    assert me.status_code == 200, me.text
    identity = me.json()
    return RegisteredAccount(
        username=username,
        password=password,
        principal_id=identity["principal_id"],
        tenant_id=identity["tenant_id"],
        project_id=body["default_project_id"],
        cookie=cookie,
        headers=headers,
    )


@pytest.fixture
def register_user(platform):
    """注册额外用户的工厂：每次调用得到独立客户端、cookie 与身份。

    用于"另一个用户"的隔离用例（越权、确认转让等）。每个账号在**独立**
    的 TestClient 上注册 —— 同一个客户端的 cookie jar 已有会话时会命中
    `already_authenticated`，无法再注册第二个账号。

    `on=<platform>` 可在自定义平台上注册（默认用当前 `platform` 夹具）。
    """

    def _make(
        password: str | None = None, *, on=None
    ) -> tuple[TestClient, RegisteredAccount]:
        target = on if on is not None else platform
        session_client = TestClient(create_app(platform=target))
        return session_client, _register_http(session_client, password=password)

    return _make


@pytest.fixture
def primary_account(platform):
    """每个用例的**主身份**：真实注册得到，`demo` 与默认 `auth_headers()` 都用它。"""
    return _register_http(TestClient(create_app(platform=platform)))


@pytest.fixture
def auth_headers(primary_account, platform):
    """返回一个可调用的请求头工厂（签名与历史一致）。

    默认参数返回**主身份**的 Cookie 头；传入不同的 (tenant_id, principal_id, name)
    会注册一个**独立**的真实账号（用于需要"另一个主体"的用例）。
    请求头同时带同源 `Origin`，以便不安全方法通过严格 CSRF 校验。
    """
    cache: dict[tuple, RegisteredAccount] = {(None, None, ""): primary_account}

    def _make(
        tenant_id: str | None = None,
        principal_id: str | None = None,
        name: str = "",
    ) -> dict[str, str]:
        key = (tenant_id, principal_id, name)
        account = cache.get(key)
        if account is None:
            account = _register_http(TestClient(create_app(platform=platform)))
            cache[key] = account
        return dict(account.headers)

    _make.accounts = cache  # type: ignore[attr-defined]
    return _make


@pytest.fixture
def cookie_project(client, platform):
    """已注册登录的 cookie 会话 + 注册自动创建的默认项目，返回 `(client, project_id)`。"""
    account = _register_http(client)
    return client, account.project_id


@pytest.fixture
def demo(primary_account):
    """与 `auth_headers()` 同一个注册身份（租户/主体/默认项目）。"""
    return {
        "tenant": primary_account.tenant_id,
        "principal": primary_account.principal_id,
        "project": primary_account.project_id,
    }


@pytest.fixture
def tenant_ctx():
    from app.tenancy.context import TenantContext

    return TenantContext(tenant_id="tenant_a", principal_id="user_a", project_id="proj_a")
