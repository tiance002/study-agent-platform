"""会话模型的不变量收口：单一密码登录方式下，会话必须绑定凭据与安全代际。

邀请会话分支已随邀请码移除；`auth_method` 字段也已删除。这里守住剩下的约定：
`credential_id` 非空、`security_generation` 为正、时间戳带时区且过期晚于签发。
"""

from datetime import datetime, timedelta, timezone

import pytest
from app.product.models import UserSession


def _window(**kwargs):
    now = datetime.now(timezone.utc)
    values = {
        "session_id": "sess_test",
        "tenant_id": "tenant_test",
        "principal_id": "principal_test",
        "issued_at": now,
        "expires_at": now + timedelta(hours=1),
        "credential_id": "cred_test",
        "security_generation": 1,
    }
    values.update(kwargs)
    return values


def test_password_session_requires_a_credential_and_generation():
    session = UserSession(**_window())
    assert session.credential_id == "cred_test"
    assert session.security_generation == 1
    assert session.revoked_at is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("credential_id", ""),
        ("security_generation", 0),
        ("security_generation", -1),
    ],
)
def test_session_rejects_missing_credential_or_invalid_generation(field, value):
    with pytest.raises(ValueError, match=field):
        UserSession(**_window(**{field: value}))


def test_session_requires_aware_timestamps():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="时区"):
        UserSession(**_window(issued_at=now.replace(tzinfo=None)))


def test_session_expiry_must_be_after_issue():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="expires_at"):
        UserSession(**_window(issued_at=now, expires_at=now))
