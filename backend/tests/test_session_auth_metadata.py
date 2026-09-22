from datetime import datetime, timedelta, timezone

import pytest
from app.product.models import AuthMethod, UserSession


def _window(**kwargs):
    now = datetime.now(timezone.utc)
    values = {
        "session_id": "sess_test",
        "tenant_id": "tenant_test",
        "principal_id": "principal_test",
        "issued_at": now,
        "expires_at": now + timedelta(hours=1),
    }
    values.update(kwargs)
    return values


def test_invitation_session_cannot_reference_password_credential():
    with pytest.raises(ValueError):
        UserSession(
            **_window(
                auth_method=AuthMethod.INVITATION,
                credential_id="cred_test",
                security_generation=1,
            )
        )


def test_password_session_requires_matching_credential_and_generation():
    session = UserSession(
        **_window(
            auth_method=AuthMethod.PASSWORD,
            credential_id="cred_test",
            security_generation=1,
        )
    )
    assert session.auth_method is AuthMethod.PASSWORD
    assert session.credential_id == "cred_test"
    assert session.security_generation == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"auth_method": AuthMethod.PASSWORD},
        {"auth_method": AuthMethod.PASSWORD, "credential_id": "cred_test"},
    ],
)
def test_session_auth_metadata_rejects_incomplete_combinations(kwargs):
    with pytest.raises(ValueError):
        UserSession(**_window(**kwargs))


def test_legacy_constructor_defaults_to_invitation_session():
    session = UserSession(**_window())
    assert session.auth_method is AuthMethod.INVITATION
    assert session.credential_id is None
    assert session.security_generation == 1
