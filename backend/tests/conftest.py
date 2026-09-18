"""测试公共夹具。

约定：每个测试使用独立的临时目录作为审计 sink，互不干扰；
平台实例逐个用例新建，避免状态串味。
"""

from __future__ import annotations

import pytest

from app.main import build_platform


@pytest.fixture
def platform(tmp_path):
    """一个全新的平台实例（内存适配器 + 临时审计目录）。"""
    return build_platform(var_dir=tmp_path)


@pytest.fixture
def tenant_ctx():
    from app.tenancy.context import TenantContext

    return TenantContext(tenant_id="tenant_a", principal_id="user_a", project_id="proj_a")
