"""测试公共夹具。

约定：
- 每个测试使用独立临时目录与全新平台实例，互不干扰。
- 身份通过 `auth_headers` **走完整认证路径**生成（签发 → 序列化 → Bearer 头）。
  不允许为了方便在测试里绕过认证 —— 那会让"客户端自报身份"以另一种形式复活。
- `cookie_project` 是 **cookie 主路径**的共享起点：签发邀请 → 兑换会话 → 建项目。
  摄取与检索两处都要用它。每个文件各抄一份登录舞蹈，正是某一天两份会分叉的地方
  （比如一处忘了断言兑换状态码），而分叉的结果是"有一个文件的身份根本不是真的"。
- `pg_database` 是**PostgreSQL 测试的强制前置**：整场会话跑在一个随机临时库上，
  结束即删除。业务库里的在途任务不是测试的耗材（见 `pgtest.py` 的模块 docstring）。
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pgtest
import pytest
from app.identity.ports import SystemContext
from app.main import (
    DEMO_PRINCIPAL,
    DEMO_PROJECT,
    DEMO_TENANT,
    build_platform,
    create_app,
)
from fastapi.testclient import TestClient


@pytest.fixture(scope="session", autouse=True)
def pg_database() -> Iterator[pgtest.TestDatabase | None]:
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
    if not pgtest.reachable():
        yield None
        return

    database = pgtest.create_test_database()
    saved = {name: os.environ.get(name) for name in pgtest.DSN_ENV_VARS}
    os.environ.update(database.env())
    try:
        yield database
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        pgtest.drop_test_database(database)


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


@pytest.fixture
def cookie_project(client, platform):
    """已登录的 cookie 会话 + 一个刚创建的项目，返回 `(client, project_id)`。"""
    token = "cookie-" + uuid.uuid4().hex
    now = platform.clock.now()
    platform.invitations.issue(
        SystemContext(DEMO_TENANT, "cookie 会话夹具"),
        invitation_id="inv_" + uuid.uuid4().hex[:8],
        token_hash="sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        issued_by=DEMO_PRINCIPAL,
        invitee_principal_id=DEMO_PRINCIPAL,
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )
    exchanged = client.post("/auth/invitations/exchange", json={"token": token})
    assert exchanged.status_code == 200, "夹具必须走完整认证路径"
    created = client.post(
        "/projects",
        json={"name": "夹具项目"},
        headers={"Origin": "http://testserver", "Idempotency-Key": "fix-" + uuid.uuid4().hex},
    )
    assert created.status_code == 201, created.text
    return client, created.json()["project_id"]


@pytest.fixture
def auth_headers(platform):
    """签发会话令牌并组装 Authorization 头。"""

    def _make(
        tenant_id: str = DEMO_TENANT,
        principal_id: str = DEMO_PRINCIPAL,
        name: str = "",
    ) -> dict[str, str]:
        token = platform.sessions.issue(
            principal_id=principal_id,
            tenant_id=tenant_id,
            display_name=name,
            issued_at=platform.clock.now(),
        )
        return {"Authorization": f"Bearer {platform.sessions.serialize(token)}"}

    return _make


@pytest.fixture
def demo():
    return {"tenant": DEMO_TENANT, "principal": DEMO_PRINCIPAL, "project": DEMO_PROJECT}


@pytest.fixture
def tenant_ctx():
    from app.tenancy.context import TenantContext

    return TenantContext(tenant_id="tenant_a", principal_id="user_a", project_id="proj_a")
