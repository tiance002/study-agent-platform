"""测试公共夹具。

约定：
- 每个测试使用独立临时目录与全新平台实例，互不干扰。
- 身份通过 `auth_headers` **走完整认证路径**生成（签发 → 序列化 → Bearer 头）。
  不允许为了方便在测试里绕过认证 —— 那会让"客户端自报身份"以另一种形式复活。
"""

from __future__ import annotations

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
