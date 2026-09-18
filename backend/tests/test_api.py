"""HTTP 接入层的端到端测试。

重点不在"接口能返回 200"，而在**边界是否在正确的位置生效**：
未知 node 被拒且不留预算、A2 写入需要确认、审计链可校验。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import build_platform, create_app

TENANT = "tenant_demo"
PROJECT = "proj_demo"


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(platform=build_platform(var_dir=tmp_path)))


def _ingest(client) -> None:
    response = client.post(
        f"/projects/{PROJECT}/sources",
        json={
            "tenant_id": TENANT,
            "learning_project_id": PROJECT,
            "source_id": "src_1",
            "chunks": ["Agent harness 负责编排工具调用与循环退出条件", "异步与并发决定吞吐上限"],
        },
    )
    assert response.status_code == 200, response.text


def _interact(client, node_id: str, **extra) -> dict:
    payload = {
        "tenant_id": TENANT,
        "principal_id": "user_demo",
        "learning_project_id": PROJECT,
        "node_id": node_id,
        "user_input": "Agent harness 怎么学",
        "params": {"targets": ["agent.harness"]},
    }
    payload.update(extra)
    response = client.post(f"/projects/{PROJECT}/interactions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_healthz_declares_which_components_are_stubs(client):
    """健康检查必须如实说明哪些组件是开发适配器，不能让人误以为生产可用。"""
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["components"]["policy_gateway"] == "available"
    assert body["components"]["sandbox"] == "not_implemented"
    assert body["components"]["persistence"] == "in_memory_adapter"


def test_registry_view_exposes_tool_boundaries(client):
    """工具边界应当是可读的：意图、去向、权限、幂等、网络范围。"""
    body = client.get("/registry").json()
    tools = {item["tool_id"]: item for item in body["tools"]}
    assert tools["append_project_evidence"]["min_authority"] == "A2"
    assert tools["fetch_external_url"]["min_authority"] == "A1c"
    assert tools["fetch_external_url"]["network_domains"] == [
        "docs.python.org",
        "fastapi.tiangolo.com",
    ]


def test_retrieval_returns_citations_and_evidence_state(client):
    _ingest(client)
    body = _interact(client, "retrieve_material")
    assert body["status"] == "ok"
    assert body["output"]["evidence_state"] == "supported"
    assert body["citations"], "有命中时必须返回引用"
    assert body["citations"][0]["content_hash"].startswith("sha256:")


def test_unknown_node_is_denied(client):
    """不变量 #5：未注册 node 被拒，且不产生任何副作用。"""
    body = _interact(client, "nonexistent_node")
    assert body["status"] == "denied"
    assert body["error"]["code"] == "NODE_NOT_REGISTERED"


def test_high_impact_write_requires_confirmation(client):
    """03 号规格 §7：A2 写入未确认时策略拒绝。"""
    params = {
        "artifact": {"a": 1},
        "required_keys": ["a"],
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
    denied = _interact(client, "validate_and_record", params=params)
    assert denied["status"] == "denied"
    assert denied["error"]["code"] == "POLICY_DENIED"

    allowed = _interact(
        client, "validate_and_record", params=params, confirmed_tools=["append_project_evidence"]
    )
    assert allowed["status"] == "ok"
    assert allowed["output"]["evidence_event_id"].startswith("ev_")


def test_mastery_projection_is_read_only_and_rebuildable(client):
    _ingest(client)
    params = {
        "artifact": {"a": 1},
        "required_keys": ["a"],
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
    _interact(client, "validate_and_record", params=params, confirmed_tools=["append_project_evidence"])

    first = client.get(f"/projects/{PROJECT}/mastery?tenant_id={TENANT}").json()
    second = client.get(f"/projects/{PROJECT}/mastery?tenant_id={TENANT}").json()
    assert first == second, "读接口不应改变状态，重复读取结果必须一致"
    assert first["components"][0]["component_id"] == "llm.tool_calling"
    assert first["components"][0]["confidence"] in {"insufficient", "low", "medium", "high"}


def test_audit_chain_stays_valid_after_requests(client):
    _ingest(client)
    _interact(client, "diagnose_prerequisites")
    body = client.get(f"/projects/{PROJECT}/audit").json()
    assert body["records"] > 0
    assert body["chain_valid"] is True


def test_budget_leaks_no_open_reservations(client):
    """交互结束后不应留下未结的**调用**预留。"""
    _ingest(client)
    _interact(client, "diagnose_prerequisites")
    body = client.get(f"/projects/{PROJECT}/budget").json()
    assert body["open_reservations"] == []
    assert body["needs_reconciliation"] == []
