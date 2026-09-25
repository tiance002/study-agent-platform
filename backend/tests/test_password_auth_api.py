from app.deployment import DeploymentSettings
from app.main import build_platform, create_app
from fastapi.testclient import TestClient


def _client(tmp_path):
    platform = build_platform(var_dir=tmp_path)
    platform.registration_enabled = True
    platform.password_login_enabled = True
    return platform, TestClient(create_app(platform=platform))


def test_register_login_and_existing_cookie_contract(tmp_path):
    platform, client = _client(tmp_path)
    headers = {"Origin": "http://testserver"}
    registered = client.post(
        "/auth/register", json={"username": "张三", "password": "LongPass1234"}, headers=headers
    )
    assert registered.status_code == 201
    assert registered.headers["cache-control"] == "no-store"
    assert registered.json()["default_project_name"] == "我的学习项目"

    already = client.post(
        "/auth/login", json={"username": "张三", "password": "LongPass1234"}, headers=headers
    )
    assert already.status_code == 409
    assert already.json()["code"] == "already_authenticated"
    assert already.headers["cache-control"] == "no-store"

    client.post("/auth/logout", headers={"Origin": "http://testserver"})
    logged_in = client.post(
        "/auth/login", json={"username": "张三", "password": "LongPass1234"}, headers=headers
    )
    assert logged_in.status_code == 200
    success_events = [
        record["event_type"]
        for record in platform.audit.read_all()
        if record["event_type"] in {"account_registered", "password_login_succeeded"}
    ]
    assert success_events == ["account_registered", "password_login_succeeded"]


def test_nfkc_collision_and_wrong_password_are_uniform(tmp_path):
    _, client = _client(tmp_path)
    headers = {"Origin": "http://testserver"}
    assert client.post(
        "/auth/register", json={"username": "Ａ", "password": "LongPass1234"}, headers=headers
    ).status_code == 201
    client.post("/auth/logout", headers={"Origin": "http://testserver"})
    collision = client.post(
        "/auth/register", json={"username": "a", "password": "LongPass1234"}, headers=headers
    )
    assert collision.status_code == 409
    client.post("/auth/logout", headers={"Origin": "http://testserver"})
    wrong = client.post(
        "/auth/login", json={"username": "Ａ", "password": "wrongpass1"}, headers=headers
    )
    assert wrong.status_code == 401


def test_development_flags_default_open_and_explicit_disable_still_works(tmp_path):
    platform = build_platform(var_dir=tmp_path)
    assert platform.registration_enabled is True
    assert platform.password_login_enabled is True

    disabled_settings = DeploymentSettings.load(
        {
            "STUDY_PLATFORM_REGISTRATION_ENABLED": "false",
            "STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED": "false",
        }
    )
    disabled_platform = build_platform(var_dir=tmp_path / "disabled", settings=disabled_settings)
    client = TestClient(create_app(platform=platform))
    response = client.post(
        "/auth/register", json={"username": "张三", "password": "LongPass1234"}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 201

    client.post("/auth/logout", headers={"Origin": "http://testserver"})
    response = client.post(
        "/auth/register", json={"username": "张三2", "password": "abcde"}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "PARAMS_INVALID"

    disabled_response = TestClient(create_app(platform=disabled_platform)).post(
        "/auth/register", json={"username": "disabled", "password": "ValidPass12"}, headers={"Origin": "http://testserver"}
    )
    assert disabled_response.status_code == 403
    assert disabled_response.json()["code"] == "REGISTRATION_DISABLED"


def test_password_policy_is_single_6_to_12_code_points(tmp_path):
    """单一策略 6–12 码点：5 与 13 拒绝，6 与 12 允许（注册与登录同一规则）。"""
    _, client = _client(tmp_path)
    headers = {"Origin": "http://testserver"}

    too_short = client.post(
        "/auth/register", json={"username": "shortp", "password": "abc12"}, headers=headers
    )
    assert too_short.status_code == 422
    assert too_short.json()["code"] == "PARAMS_INVALID"

    six = client.post(
        "/auth/register", json={"username": "sixpass", "password": "abc123"}, headers=headers
    )
    assert six.status_code == 201
    client.post("/auth/logout", headers=headers)
    relogin = client.post(
        "/auth/login", json={"username": "sixpass", "password": "abc123"}, headers=headers
    )
    assert relogin.status_code == 200
    client.post("/auth/logout", headers=headers)

    twelve = client.post(
        "/auth/register", json={"username": "twelvepas", "password": "abc123456789"}, headers=headers
    )
    assert twelve.status_code == 201
    client.post("/auth/logout", headers=headers)

    too_long = client.post(
        "/auth/register", json={"username": "longpass", "password": "abc1234567890"}, headers=headers
    )
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "PARAMS_INVALID"


def test_logout_clears_invalid_cookie_without_claiming_server_revocation(tmp_path):
    platform, client = _client(tmp_path)
    client.cookies.set("study_session", "not-a-valid-cookie")
    response = client.post("/auth/logout", headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert response.json() == {"revoked": False, "cookie_cleared": True}
    assert "study_session=" in response.headers.get("set-cookie", "")


def test_auth_body_limit_rejects_before_parsing_and_keeps_request_id(tmp_path):
    _, client = _client(tmp_path)
    response = client.post(
        "/auth/register",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(64 * 1024 + 1),
            "Origin": "http://testserver",
        },
    )
    assert response.status_code == 413
    assert response.json()["code"] == "PARAMS_INVALID"
    assert response.json()["request_id"] == response.headers["X-Request-Id"]
