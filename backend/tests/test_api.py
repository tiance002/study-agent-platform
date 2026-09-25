"""HTTP 接入层的端到端测试。

重点不在"接口能返回 200"，而在**边界在正确的位置生效**：

- 身份只能来自令牌（无令牌 401，伪造令牌 401）；
- 项目归属由服务端成员关系判定；
- A2 写入必须携带**服务端确认记录**，且确认一次一用、绑定参数；
- 审计链可校验、预算不泄漏。
"""

from __future__ import annotations

import sys
import uuid

import pytest
from app.deployment import DeploymentSettings
from app.identity.ports import SystemContext
from app.main import create_app
from fastapi.testclient import TestClient

#: 认证身份已改为**真实注册**：这些端到端用例要操作的必须是该身份能访问的项目。
#: `_registered_project` 把它替换成注册账号的默认项目（`demo["project"]`）。
PROJECT = "proj_demo"


@pytest.fixture(autouse=True)
def _registered_project(monkeypatch, demo):
    monkeypatch.setattr(sys.modules[__name__], "PROJECT", demo["project"])


# 每次调用生成唯一幂等键：变更接口必须携带 Idempotency-Key（审查修复），
# 且不同调用不得共用键 —— 同键同内容会命中重放缓存，测不到真实执行。


def _idem_key() -> str:
    return "key-" + uuid.uuid4().hex


def test_production_surface_does_not_mount_legacy_development_routes(platform):
    platform.settings = DeploymentSettings.load({"STUDY_PLATFORM_ENV": "production"})
    client = TestClient(create_app(platform=platform))

    assert client.get("/healthz").status_code == 200
    assert client.get("/me").status_code == 401
    assert client.get("/projects/proj_missing/mastery").status_code == 401

    for method, path in (
        ("get", "/registry"),
        ("post", "/projects/proj_missing/retrieval/chunks"),
        ("post", "/projects/proj_missing/confirmations"),
        ("post", "/projects/proj_missing/interactions"),
        ("get", "/projects/proj_missing/audit"),
        ("get", "/projects/proj_missing/budget"),
    ):
        assert getattr(client, method)(path).status_code == 404

# 执行 validate_and_record 时提交的参数。
# **创建确认时提交同一份参数** —— 确认绑定的是「你提交的那次请求的参数」，
# 所以客户端能精确预测，不需要猜服务端怎么构造。
WRITE_PARAMS = {
    "artifact": {"a": 1},
    "required_keys": ["a"],
    "task_id": "assessment_1",
    "contract_id": "ctr_1",
    "mapping_version": "map_1",
    "verdicts": [
        {
            "component_id": "llm.tool_calling",
            "validity": "valid",
            "observation_strength": 4,
            "independence_level": "practiced",
            "direction": "positive",
            "independence_group": "g1",
        }
    ],
}


def _ingest(client, headers, *, project: str | None = None) -> dict:
    project = project or PROJECT
    response = client.post(
        f"/projects/{project}/retrieval/chunks",
        json={
            "source_id": "src_1",
            "chunks": ["Agent harness 负责编排工具调用与循环退出条件", "异步与并发决定吞吐上限"],
        },
        headers={**headers, "Idempotency-Key": _idem_key()},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _interact(client, headers, node_id: str, *, project: str | None = None, **extra) -> dict:
    project = project or PROJECT
    payload = {
        "node_id": node_id,
        "user_input": "Agent harness 怎么学",
        "params": {"targets": ["agent.harness"]},
    }
    payload.update(extra)
    response = client.post(
        f"/projects/{project}/interactions",
        json=payload,
        headers={**headers, "Idempotency-Key": _idem_key()},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_confirmation(client, headers, tool_id: str, params: dict, *, project: str | None = None):
    project = project or PROJECT
    return client.post(
        f"/projects/{project}/confirmations",
        json={"tool_id": tool_id, "params": params},
        headers={**headers, "Idempotency-Key": _idem_key()},
    )


# ------------------------------------------------------------------ 认证


def test_healthz_declares_stub_components(client):
    """健康检查必须如实说明哪些组件是开发适配器。"""
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["components"]["auth"] == "cookie_session_bearer_compat"
    assert body["components"]["rls"] == "not_implemented"
    assert body["components"]["sandbox"] == "not_implemented"
    assert body["features"] == {
        "registration_enabled": True,
        "password_login_enabled": True,
        "paid_dispatch_config_source": "environment",
    }


def test_requests_without_token_are_rejected(client):
    """不变量 #1：未通过身份校验的请求不得读取任何项目数据。"""
    for method, url in (
        ("get", f"/projects/{PROJECT}/mastery"),
        ("get", f"/projects/{PROJECT}/audit"),
        ("get", f"/projects/{PROJECT}/budget"),
        ("get", "/me"),
    ):
        response = getattr(client, method)(url)
        assert response.status_code == 401, f"{url} 应要求认证"
        assert response.json()["code"] == "UNAUTHENTICATED"


def test_forged_token_is_rejected(client, platform):
    """篡改令牌（换租户 + 错误密钥）必须失败 —— 这是「身份不可自报」的核心证明。"""
    from app.identity.session import SessionIssuer

    attacker = SessionIssuer(secret="dev-only-attacker-secret")
    forged = attacker.issue(
        principal_id="user_demo",
        tenant_id="tenant_other",
        issued_at=platform.clock.now(),
    )
    forged_raw = attacker.serialize(forged)

    response = client.get("/me", headers={"Authorization": f"Bearer {forged_raw}"})
    assert response.status_code == 401


def test_identity_comes_from_token_not_request(client, auth_headers, demo):
    """/me 返回的身份来自会话；请求体里根本没有可填的身份字段。"""
    body = client.get("/me", headers=auth_headers()).json()
    assert body["principal_id"] == demo["principal"]
    assert body["tenant_id"] == demo["tenant"]


def test_body_fields_impersonating_identity_are_rejected(client, auth_headers):
    """请求体里出现身份字段必须**被拒绝**，而不是被静默忽略。

    pydantic 默认 `extra="ignore"`，会把 `tenant_id` 一声不响地丢掉。
    那样"安全"确实安全 —— 身份改不了 —— 但两种本该被看见的情况会一起消失：
    旧客户端以为自己在设置身份，以及有人正拿这个字段做探测。
    所以这里是 422，不是 200：**丢弃不等于拒绝，静默不等于安全。**
    """
    response = client.post(
        f"/projects/{PROJECT}/interactions",
        json={
            "node_id": "intake_goal",
            "user_input": "x",
            "tenant_id": "tenant_other",      # 企图自报
            "principal_id": "someone_else",   # 企图自报
        },
        headers=auth_headers(),
    )
    assert response.status_code == 422, response.text


@pytest.mark.invariant
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/projects/{project}/interactions",
            {"node_id": "intake_goal", "user_input": "x", "tenant_id": "t"},
        ),
        (
            "/projects/{project}/sources",
            {"source_id": "s1", "chunks": ["x"], "learning_project_id": "p"},
        ),
        (
            "/projects/{project}/confirmations",
            {"tool_id": "append_project_evidence", "params": {}, "principal_id": "u"},
        ),
    ],
)
def test_all_request_models_forbid_unknown_fields(client, auth_headers, path, payload):
    """三个请求模型都必须拒绝未知字段 —— 只改一个等于没改。"""
    response = client.post(
        path.format(project=PROJECT),
        json=payload,
        headers=auth_headers(),
    )
    assert response.status_code == 422, response.text


# ------------------------------------------------------------------ 项目归属


def test_project_membership_is_required(client, auth_headers, platform):
    """令牌合法但项目不属于该主体 → 拒绝（对外 404，不暴露存在性）。"""
    platform.membership.create_project(
        SystemContext("tenant_demo"), project_id="proj_someone_else"
    )
    response = client.get(
        "/projects/proj_someone_else/mastery", headers=auth_headers()
    )
    assert response.status_code == 404


def test_registry_view_exposes_tool_boundaries(client, auth_headers):
    body = client.get("/registry", headers=auth_headers()).json()
    tools = {item["tool_id"]: item for item in body["tools"]}
    assert tools["append_project_evidence"]["min_authority"] == "A2"
    assert tools["fetch_external_url"]["min_authority"] == "A1c"


# ------------------------------------------------------------------ 正常流程


def test_retrieval_returns_citations_and_evidence_state(client, auth_headers):
    """检索返回引用，但证据状态**不得**因为"有命中"就升级。

    ⚠️ 这是**对外可见的契约变化**：此前这里断言 `evidence_state == "supported"`，
    与规格不符 —— 有命中只说明检索返回了东西，不说明任何核心结论有充分证据。
    首版没有冻结的核心结论标注集，所以正确状态是 `insufficient` + `MISSING_SUPPORT`，
    同时 `retrieval_health` 如实报告过程是干净的。

    **过程干净 ≠ 结论有据**，这两个问题现在分属两个字段。
    """
    headers = auth_headers()
    _ingest(client, headers)
    body = _interact(client, headers, "retrieve_material")

    assert body["status"] == "ok"
    assert body["citations"], "有命中时必须返回引用"
    assert body["citations"][0]["content_hash"].startswith("sha256:")

    output = body["output"]
    assert output["evidence_state"] == "insufficient"
    assert output["retrieval_health"] == "clean"
    assert [i["code"] for i in output["issues"]] == ["MISSING_SUPPORT"]
    assert "unresolved" not in output


def test_response_never_carries_two_conflicting_evidence_verdicts(client, auth_headers):
    """同一响应里不得同时出现两个互相矛盾的证据判定。

    审查复现过的缺陷：node handler 给出结构化的 `evidence_state=insufficient`，
    运行时外层又按「有没有 citation」另算一个 `evidence_sufficiency=supported`。
    读旧字段的客户端会据此接受缺少关键证据的答案 ——
    **矛盾的判定比没有判定更危险**，因为它看起来像有依据。
    """
    headers = auth_headers()
    _ingest(client, headers)
    body = _interact(client, headers, "retrieve_material")

    assert body["citations"], "本用例必须有命中，才构成「有 citation」的条件"
    forbidden = {"evidence_sufficiency"}
    assert not (forbidden & set(body["output"])), (
        f"响应里又出现了第二套证据判定：{forbidden & set(body['output'])}"
    )


def test_unknown_node_is_denied(client, auth_headers):
    body = _interact(client, auth_headers(), "nonexistent_node")
    assert body["status"] == "denied"
    assert body["error"]["code"] == "NODE_NOT_REGISTERED"


# ------------------------------------------------------- 服务端确认（核心）


def test_high_impact_write_requires_server_side_confirmation(client, auth_headers):
    """A2 写入：没有服务端确认记录就不能执行；有记录才能执行；且一次一用。"""
    headers = auth_headers()

    denied = _interact(client, headers, "validate_and_record", params=WRITE_PARAMS)
    assert denied["status"] == "denied", "无确认记录时必须拒绝"
    assert denied["error"]["code"] == "POLICY_DENIED"

    created = _create_confirmation(client, headers, "append_project_evidence", WRITE_PARAMS)
    assert created.status_code == 200, created.text
    payload = created.json()
    confirmation_id = payload["confirmation_id"]
    # 确认界面需要展示的内容必须齐全
    assert payload["tool"]["min_authority"] == "A2"
    assert "reversible" in payload["tool"]
    assert payload["expires_at"]

    allowed = _interact(
        client,
        headers,
        "validate_and_record",
        params=WRITE_PARAMS,
        confirmation_id=confirmation_id,
    )
    assert allowed["status"] == "ok", allowed
    assert allowed["output"]["evidence_event_id"].startswith("ev_")

    reused = _interact(
        client,
        headers,
        "validate_and_record",
        params=WRITE_PARAMS,
        confirmation_id=confirmation_id,
    )
    assert reused["status"] == "denied", "同一条确认不能执行两次"


def test_confirmation_is_bound_to_params(client, auth_headers):
    """确认只对当时展示的参数有效：换了参数就失效。"""
    headers = auth_headers()
    created = _create_confirmation(client, headers, "append_project_evidence", WRITE_PARAMS).json()

    tampered = {**WRITE_PARAMS, "task_id": "assessment_OTHER"}
    result = _interact(
        client,
        headers,
        "validate_and_record",
        params=tampered,
        confirmation_id=created["confirmation_id"],
    )
    assert result["status"] == "denied"
    assert result["error"]["code"] == "POLICY_DENIED"


def test_confirmation_cannot_be_issued_for_low_impact_tool(client, auth_headers):
    """低影响动作不需要确认 —— 否则确认会变成随手点掉的仪式。"""
    response = _create_confirmation(
        client, auth_headers(), "retrieve_project_chunks", {"query": "x"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "POLICY_DENIED"


def test_confirmation_cannot_be_transferred_to_another_principal(client, auth_headers):
    """确认不能转让：别人拿不走你的确认（另一个真实账号访问同一份确认被拒）。"""
    headers = auth_headers()
    created = _create_confirmation(client, headers, "append_project_evidence", WRITE_PARAMS).json()

    # 另一个真实注册的账号：对同一项目没有访问权 —— 统一 404，
    # 既不拿走确认，也不暴露确认是否存在。
    other_headers = auth_headers(principal_id="user_other")
    response = client.post(
        f"/projects/{PROJECT}/interactions",
        json={
            "node_id": "validate_and_record",
            "user_input": "x",
            "params": WRITE_PARAMS,
            "confirmation_id": created["confirmation_id"],
        },
        headers={**other_headers, "Idempotency-Key": _idem_key()},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


# ------------------------------------------------------------- 读接口


def test_mastery_projection_is_read_only_and_rebuildable(client, auth_headers):
    headers = auth_headers()
    created = _create_confirmation(client, headers, "append_project_evidence", WRITE_PARAMS).json()
    _interact(
        client,
        headers,
        "validate_and_record",
        params=WRITE_PARAMS,
        confirmation_id=created["confirmation_id"],
    )

    first = client.get(f"/projects/{PROJECT}/mastery", headers=headers).json()
    second = client.get(f"/projects/{PROJECT}/mastery", headers=headers).json()
    assert first == second, "读接口不应改变状态"
    assert first["components"][0]["component_id"] == "llm.tool_calling"
    assert first["components"][0]["confidence"] in {"insufficient", "low", "medium", "high"}


def test_audit_chain_stays_valid_after_requests(client, auth_headers):
    headers = auth_headers()
    _ingest(client, headers)
    _interact(client, headers, "diagnose_prerequisites")
    body = client.get(f"/projects/{PROJECT}/audit", headers=headers).json()
    assert body["records"] > 0
    assert body["chain_valid"] is True
    assert body["chain_scope"] == "global"


def test_budget_leaks_no_open_reservations(client, auth_headers):
    headers = auth_headers()
    _ingest(client, headers)
    _interact(client, headers, "diagnose_prerequisites")
    body = client.get(f"/projects/{PROJECT}/budget", headers=headers).json()
    assert body["open_reservations"] == []
    assert body["needs_reconciliation"] == []
