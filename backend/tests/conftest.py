"""测试公共夹具。

约定：
- 每个测试使用独立临时目录与全新平台实例，互不干扰。
- 身份通过 `auth_headers` **走完整认证路径**生成（签发 → 序列化 → Bearer 头）。
  不允许为了方便在测试里绕过认证 —— 那会让"客户端自报身份"以另一种形式复活。
"""

from __future__ import annotations

import sys

import pytest
from app.main import (
    DEMO_PRINCIPAL,
    DEMO_PROJECT,
    DEMO_TENANT,
    build_platform,
    create_app,
)
from fastapi.testclient import TestClient


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
