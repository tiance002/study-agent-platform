"""HTTP 接入层的端到端测试。

重点不在"接口能返回 200"，而在**边界在正确的位置生效**：

- 身份只能来自令牌（无令牌 401，伪造令牌 401）；
- 项目归属由服务端成员关系判定；
- A2 写入必须携带**服务端确认记录**，且确认一次一用、绑定参数；
- 审计链可校验、预算不泄漏。
"""

from __future__ import annotations

PROJECT = "proj_demo"

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


def _ingest(client, headers, *, project: str = PROJECT) -> dict:
    response = client.post(
        f"/projects/{project}/sources",
        json={
            "source_id": "src_1",
            "chunks": ["Agent harness 负责编排工具调用与循环退出条件", "异步与并发决定吞吐上限"],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _interact(client, headers, node_id: str, *, project: str = PROJECT, **extra) -> dict:
    payload = {
        "node_id": node_id,
        "user_input": "Agent harness 怎么学",
        "params": {"targets": ["agent.harness"]},
    }
    payload.update(extra)
    response = client.post(f"/projects/{project}/interactions", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _create_confirmation(client, headers, tool_id: str, params: dict, *, project: str = PROJECT):
    return client.post(
        f"/projects/{project}/confirmations",
        json={"tool_id": tool_id, "params": params},
        headers=headers,
    )


# ------------------------------------------------------------------ 认证


def test_healthz_declares_stub_components(client):
    """健康检查必须如实说明哪些组件是开发适配器。"""
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["components"]["auth"] == "dev_bearer_session"
    assert body["components"]["rls"] == "not_implemented"
    assert body["components"]["sandbox"] == "not_implemented"


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


def test_forged_token_is_rejected(client, auth_headers, platform):
    """篡改令牌（换租户）必须失败 —— 这是「身份不可自报」的核心证明。"""
    headers = auth_headers(tenant_id="tenant_demo", principal_id="user_demo")
    raw = headers["Authorization"].split(" ", 1)[1]

    # 手工构造一个租户不同的令牌（用错误密钥签名）
    from app.identity.session import SessionIssuer

    attacker = SessionIssuer(secret="dev-only-attacker-secret")
    forged = attacker.issue(
        principal_id="user_demo",
        tenant_id="tenant_other",
        issued_at=platform.clock.now(),
    )
    forged_raw = attacker.serialize(forged)
    assert forged_raw != raw

    response = client.get("/me", headers={"Authorization": f"Bearer {forged_raw}"})
    assert response.status_code == 401


def test_identity_comes_from_token_not_request(client, auth_headers):
    """/me 返回的身份来自令牌；请求体里根本没有可填的身份字段。"""
    body = client.get("/me", headers=auth_headers(principal_id="user_demo")).json()
    assert body["principal_id"] == "user_demo"
    assert body["tenant_id"] == "tenant_demo"


def test_body_has_no_tenant_or_principal_fields(client, auth_headers):
    """请求模型里不存在 tenant_id / principal_id —— 传了也会被忽略。"""
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
    assert response.status_code == 200, response.text
    # 身份仍是令牌里的那个：交互正常完成，且未因自报字段改变租户
    assert response.json()["status"] == "ok"


# ------------------------------------------------------------------ 项目归属


def test_project_membership_is_required(client, auth_headers, platform):
    """令牌合法但项目不属于该主体 → 拒绝（对外 404，不暴露存在性）。"""
    platform.membership.create_project("proj_someone_else", tenant_id="tenant_demo")
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
    headers = auth_headers()
    _ingest(client, headers)
    body = _interact(client, headers, "retrieve_material")
    assert body["status"] == "ok"
    assert body["output"]["evidence_state"] == "supported"
    assert body["citations"], "有命中时必须返回引用"
    assert body["citations"][0]["content_hash"].startswith("sha256:")


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


def test_confirmation_cannot_be_transferred_to_another_principal(client, auth_headers, platform):
    """确认不能转让：别人拿不走你的确认。"""
    headers = auth_headers()
    created = _create_confirmation(client, headers, "append_project_evidence", WRITE_PARAMS).json()

    platform.membership.grant_project("tenant_demo", "user_other", PROJECT)
    other_headers = auth_headers(principal_id="user_other")
    result = _interact(
        client,
        other_headers,
        "validate_and_record",
        params=WRITE_PARAMS,
        confirmation_id=created["confirmation_id"],
    )
    assert result["status"] == "denied"


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
