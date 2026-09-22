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
        "/auth/register", json={"username": "张三", "password": "LongPassword12"}, headers=headers
    )
    assert registered.status_code == 201
    assert registered.headers["cache-control"] == "no-store"
    assert registered.json()["default_project_name"] == "我的学习项目"

    already = client.post(
        "/auth/login", json={"username": "张三", "password": "LongPassword12"}, headers=headers
    )
    assert already.status_code == 409
    assert already.json()["code"] == "already_authenticated"
    assert already.headers["cache-control"] == "no-store"

    client.post("/auth/logout", headers={"Origin": "http://testserver"})
    logged_in = client.post(
        "/auth/login", json={"username": "张三", "password": "LongPassword12"}, headers=headers
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
        "/auth/register", json={"username": "Ａ", "password": "LongPassword12"}, headers=headers
    ).status_code == 201
    client.post("/auth/logout", headers={"Origin": "http://testserver"})
    collision = client.post(
        "/auth/register", json={"username": "a", "password": "LongPassword12"}, headers=headers
    )
    assert collision.status_code == 409
    client.post("/auth/logout", headers={"Origin": "http://testserver"})
    wrong = client.post(
        "/auth/login", json={"username": "Ａ", "password": "wrong-password"}, headers=headers
    )
    assert wrong.status_code == 401


def test_missing_flags_default_closed(tmp_path):
    platform = build_platform(var_dir=tmp_path)
    client = TestClient(create_app(platform=platform))
    response = client.post(
        "/auth/register", json={"username": "张三", "password": "LongPassword12"}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "REGISTRATION_DISABLED"


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
