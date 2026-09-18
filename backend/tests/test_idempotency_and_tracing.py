"""幂等键与追踪 id 是两件事。

审查复现过的缺陷：运行时用 `request_id` 做幂等去重，而 `request_id` 是服务端
**每请求重新生成**的追踪 id —— 客户端重试必然拿到新值，缓存永远命不中。
那种"幂等"从不报错，只是静默地什么都没做：比没有更糟，因为它看起来已经实现。

这一组守两件事：
1. `idempotency_key` 由客户端提供，且必须绑定**主体、项目与请求内容**；
2. 追踪 id 在响应头、响应体、错误体与审计里必须是同一个值。
"""

from __future__ import annotations

import pytest
from app.core.errors import ErrorCode
from app.workflow.runtime import InteractionRequest


def _request(demo, **overrides) -> InteractionRequest:
    payload = {
        "request_id": "req_trace_1",
        "tenant_id": demo["tenant"],
        "principal_id": demo["principal"],
        "learning_project_id": demo["project"],
        "node_id": "diagnose_prerequisites",
        "user_input": "从哪开始",
        "params": {"targets": ["agent.harness"]},
    }
    payload.update(overrides)
    return InteractionRequest(**payload)


# --------------------------------------------------------------- 幂等语义


@pytest.mark.invariant
def test_same_key_with_same_content_is_not_executed_twice(platform, demo):
    """同一幂等键 + 同一内容 = 不重复执行。"""
    audit_before = len(platform.audit.read_all())

    first = platform.runtime.run(_request(demo, idempotency_key="k1"))
    after_first = len(platform.audit.read_all())

    second = platform.runtime.run(
        _request(demo, request_id="req_trace_2", idempotency_key="k1")
    )
    after_second = len(platform.audit.read_all())

    assert first.status == "ok", first.error
    assert after_first > audit_before, "第一次必须真的执行了（有审计事件）"
    assert after_second == after_first, "第二次重放不得再产生任何审计事件"
    assert second.output.get("idempotent_replay") is True, (
        "重放必须是可观测的：否则客户端分不清『重试没生效』和『命中了缓存』"
    )


@pytest.mark.invariant
def test_replay_carries_the_current_tracing_id(platform, demo):
    """重放返回的追踪 id 必须是**本次**请求的。

    原样返回缓存结果会让响应头（本次）与响应体（上一次）指向两个不同 id，
    排障时又是一次"对不上"。
    """
    platform.runtime.run(_request(demo, idempotency_key="k1"))
    replay = platform.runtime.run(
        _request(demo, request_id="req_trace_2", idempotency_key="k1")
    )
    assert replay.request_id == "req_trace_2"


@pytest.mark.invariant
def test_same_key_with_different_content_is_rejected(platform, demo):
    """同一把钥匙不能开两扇门 —— 必须拒绝，而不是"以先到者为准"。"""
    platform.runtime.run(_request(demo, idempotency_key="k1"))
    conflicting = platform.runtime.run(
        _request(demo, user_input="换了内容", idempotency_key="k1")
    )
    assert conflicting.status == "denied"
    assert conflicting.error is not None
    assert conflicting.error["code"] == str(ErrorCode.IDEMPOTENCY_VIOLATION)


@pytest.mark.invariant
def test_same_key_from_another_principal_is_rejected(platform, demo):
    """幂等键必须绑定主体。

    否则猜到（或复用）别人的 key 就能读到别人的结果 ——
    那等于把幂等缓存变成跨用户读取通道。
    """
    platform.runtime.run(_request(demo, idempotency_key="shared"))
    other = platform.runtime.run(
        _request(demo, principal_id="someone_else", idempotency_key="shared")
    )
    assert other.status == "denied"
    assert other.error is not None
    assert other.error["code"] == str(ErrorCode.IDEMPOTENCY_VIOLATION)


@pytest.mark.invariant
def test_without_key_each_request_executes(platform, demo):
    """不提供幂等键时不做去重 —— 不能悄悄把两次真实请求合并成一次。"""
    first = platform.runtime.run(_request(demo, request_id="r1"))
    second = platform.runtime.run(_request(demo, request_id="r2"))
    assert first.output.get("idempotent_replay") is not True
    assert second.output.get("idempotent_replay") is not True


@pytest.mark.invariant
def test_tracing_id_alone_never_dedupes(platform, demo):
    """**同一追踪 id 也不得被当成幂等键。**

    这是修复前的实际行为：用 `request_id` 去重。它恰好在"同一个请求对象重放"
    这种测试场景下能命中，所以看起来可用；但真实客户端每次重试都会拿到新的
    追踪 id，于是永远命不中。这条测试钉住"追踪 id 不参与幂等"。
    """
    first = platform.runtime.run(_request(demo, request_id="req_same"))
    second = platform.runtime.run(_request(demo, request_id="req_same"))
    assert second.output.get("idempotent_replay") is not True, (
        "追踪 id 不能充当幂等键 —— 它每次请求都会变"
    )
    assert first is not second


@pytest.mark.invariant
def test_audit_events_carry_the_same_tracing_id(platform, demo):
    """审计事件必须带**本次请求**的追踪 id。

    这条测试的来历值得记：我曾在 README 与提交说明里断言"响应头、响应体、错误体、
    审计同一个值"，而实测审计事件里**根本没有 `request_id`** ——
    成功路径只带 `run_id`（由 request_id 与 node_id 哈希而来，**不可逆**）。
    也就是说"错误响应 → 审计事件"之间没有可用的关联键，
    而全链路追踪正是追踪 id 存在的唯一理由。

    **写下的断言也是断言，必须实测。**
    """
    result = platform.runtime.run(_request(demo, request_id="req_trace_audit"))
    assert result.status == "ok", result.error

    records = platform.audit.read_all()
    assert records, "本次交互应当产生审计事件"
    assert [r["request_id"] for r in records] == ["req_trace_audit"] * len(records)


@pytest.mark.invariant
def test_idempotency_key_binds_confirmation_id(platform, demo):
    """同一幂等键 + 不同确认记录 = 不同请求，必须拒绝。

    这是一次自查发现的缺口：指纹绑了租户、项目、主体、node、输入与参数，
    唯独漏了 `confirmation_id`。漏掉它的后果不是"少一层校验"，而是
    **第二条确认永远不会被消费**，而客户端以为自己的第二次授权生效了。
    """
    platform.runtime.run(
        _request(demo, idempotency_key="k1", confirmation_id="conf_A")
    )
    conflicting = platform.runtime.run(
        _request(demo, request_id="t2", idempotency_key="k1", confirmation_id="conf_B")
    )
    assert conflicting.status == "denied"
    assert conflicting.error is not None
    assert conflicting.error["code"] == str(ErrorCode.IDEMPOTENCY_VIOLATION)


@pytest.mark.invariant
def test_idempotency_key_is_bounded_in_length(client, auth_headers):
    """幂等键必须有长度上限。

    它由客户端提供、会被长期保留 —— 没有上限就是一个内存放大入口。
    同类字段（`user_input`）早有 `MAX_INPUT_CHARS`，这里不该成为例外。
    """
    response = client.post(
        "/projects/proj_demo/interactions",
        json={
            "node_id": "intake_goal",
            "user_input": "x",
            "idempotency_key": "k" * 10_000,
        },
        headers=auth_headers(),
    )
    assert response.status_code == 422, response.text


# --------------------------------------------------------------- 追踪 id 一致


@pytest.mark.invariant
def test_tracing_id_is_identical_across_header_body_and_error(client, auth_headers):
    """响应头、响应体、错误体必须是**同一个**追踪 id，且审计用同一个。"""
    headers = auth_headers()
    response = client.post(
        f"/projects/{'proj_demo'}/interactions",
        json={"node_id": "nonexistent_node", "user_input": "x"},
        headers=headers,
    )
    body = response.json()

    assert response.headers["X-Request-Id"] == body["request_id"]
    assert body["error"] is not None
    assert body["error"]["request_id"] == body["request_id"], (
        "错误体里的追踪 id 与响应体不一致 —— 出问题时串不起来"
    )


@pytest.mark.invariant
def test_http_requests_never_share_a_tracing_id(client, auth_headers):
    """每次 HTTP 请求都是新的追踪 id（因此它做不了幂等键）。"""
    headers = auth_headers()
    payload = {"node_id": "nonexistent_node", "user_input": "x"}
    first = client.post("/projects/proj_demo/interactions", json=payload, headers=headers)
    second = client.post("/projects/proj_demo/interactions", json=payload, headers=headers)
    assert first.json()["request_id"] != second.json()["request_id"]


@pytest.mark.invariant
def test_idempotency_key_is_accepted_through_the_api(client, auth_headers):
    """API 上幂等键是独立字段，且与追踪 id 并存不冲突。"""
    headers = auth_headers()
    payload = {
        "node_id": "intake_goal",
        "user_input": "我想学 Agent 工程",
        "idempotency_key": "client-key-1",
    }
    first = client.post("/projects/proj_demo/interactions", json=payload, headers=headers)
    second = client.post("/projects/proj_demo/interactions", json=payload, headers=headers)

    assert first.status_code == 200, first.text
    assert first.json()["output"].get("idempotent_replay") is not True
    assert second.json()["output"].get("idempotent_replay") is True
    # 追踪 id 仍然各是各的 —— 幂等与追踪互不干扰。
    assert first.json()["request_id"] != second.json()["request_id"]
    assert second.headers["X-Request-Id"] == second.json()["request_id"]
