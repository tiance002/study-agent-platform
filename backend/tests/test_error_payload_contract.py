"""对外错误响应体的形状契约。

这一组守的是一条**自查发现的缺陷**：错误 payload 曾经有四个生成点 ——
`PlatformError.to_payload()`、API 层两处手写 dict（401/404 的脱敏替换）、
runtime 的拒绝分支。于是新增 `next_action` 时它只落到了其中一部分：
`/interactions` 在"拒绝"路径返回 3 个字段、在"失败"路径返回 5 个。

形状不一致不是小瑕疵：客户端只能按「有就取、没有就跳过」来写，
最终等于该字段不存在。所以这里不只测"某个出口对不对"，
而是**把全部出口收集起来比对键集**——新增出口却忘了统一时，这组测试会失败。
"""

from __future__ import annotations

import pytest
from app.api.routes import error_response
from app.core.errors import (
    ERROR_PAYLOAD_KEYS,
    ErrorCode,
    PlatformError,
    deny,
    public_error_payload,
)
from app.workflow.runtime import InteractionRequest

CANONICAL = set(ERROR_PAYLOAD_KEYS)


# --------------------------------------------------------------- 唯一定义点


@pytest.mark.invariant
def test_builder_emits_exactly_the_canonical_keys():
    payload = public_error_payload("X", "y")
    assert set(payload) == CANONICAL


@pytest.mark.invariant
def test_platform_error_payload_uses_canonical_shape():
    """`to_payload()` 必须是唯一形状，含 `request_id` 与 `next_action`。"""
    error = deny(
        ErrorCode.POLICY_DENIED,
        "策略拒绝",
        request_id="r1",
    )
    payload = error.to_payload()
    assert set(payload) == CANONICAL
    assert payload["request_id"] == "r1"
    assert payload["next_action"] == ""


@pytest.mark.invariant
def test_all_error_payload_producers_agree_on_shape(platform, tenant_ctx):
    """**全部出口的键集必须一致。**

    这是这条缺陷的机械守卫：任何一个出口被改动而其他出口没跟上，此测试失败。
    出口清单：`to_payload()`、`public_error_payload()`、runtime 的 `_deny` 与 `_fail`、
    API 层的 `error_response`（含 401/404/409 三条替换分支）。
    """
    request = InteractionRequest(
        request_id="r_shape",
        tenant_id=tenant_ctx.tenant_id,
        principal_id=tenant_ctx.principal_id,
        learning_project_id=tenant_ctx.project_id,
        node_id="retrieve_material",
        user_input="x",
    )

    denied = platform.runtime._deny(  # noqa: SLF001 — 契约测试需要覆盖每个出口
        request, None, "retrieve_material", "L0", "NODE_NOT_REGISTERED", "未注册"
    )
    failed = platform.runtime._fail(  # noqa: SLF001
        request, deny(ErrorCode.POLICY_DENIED, "策略拒绝")
    )
    reconciliating = platform.runtime._fail(  # noqa: SLF001
        request, deny(ErrorCode.RECONCILIATION_REQUIRED, "结果未知")
    )

    producers = {
        "public_error_payload": public_error_payload("X", "y"),
        "to_payload": deny(ErrorCode.POLICY_DENIED, "x").to_payload(),
        "runtime._deny": denied.error,
        "runtime._fail": failed.error,
        "runtime._fail(reconcile)": reconciliating.error,
        "error_response(401)": error_response(deny(ErrorCode.AUTH_REQUIRED, "x")).body.decode(),
        "error_response(404)": error_response(
            deny(ErrorCode.CROSS_PROJECT_DENIED, "x")
        ).body.decode(),
        "error_response(409)": error_response(
            deny(ErrorCode.RECONCILIATION_REQUIRED, "x")
        ).body.decode(),
    }

    import json

    for name, payload in producers.items():
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert set(payload) == CANONICAL, f"{name} 的错误体形状与规范不一致：{sorted(payload)}"


@pytest.mark.invariant
def test_denied_and_failed_paths_have_identical_shape(platform, tenant_ctx):
    """`/interactions` 的两条失败路径必须返回同一种形状。

    这是缺陷的原发场景：`_deny` 返回 3 个字段、`_fail` 返回 5 个，
    同一个端点自己就不一致。
    """
    request = InteractionRequest(
        request_id="r_shape2",
        tenant_id=tenant_ctx.tenant_id,
        principal_id=tenant_ctx.principal_id,
        learning_project_id=tenant_ctx.project_id,
        node_id="retrieve_material",
        user_input="x",
    )
    denied = platform.runtime._deny(  # noqa: SLF001
        request, None, "retrieve_material", "L0", "NODE_NOT_REGISTERED", "未注册"
    )
    failed = platform.runtime._fail(  # noqa: SLF001
        request, deny(ErrorCode.POLICY_DENIED, "拒绝")
    )
    assert denied.error is not None and failed.error is not None
    assert set(denied.error) == set(failed.error)
    assert denied.error["request_id"] == "r_shape2"


# --------------------------------------------------------------- 状态码映射


@pytest.mark.invariant
def test_reconciliation_maps_to_409_not_403():
    """未对账的动作既不是"你不被允许"（403），也不是服务故障（5xx）。

    403 是误导；5xx 会被客户端当故障重试 —— 而盲目重试正是这条错误要阻止的行为。
    """
    response = error_response(deny(ErrorCode.RECONCILIATION_REQUIRED, "结果未知"))
    assert response.status_code == 409
    import json

    payload = json.loads(response.body.decode())
    assert payload["retryable"] is False
    assert payload["next_action"] == "reconcile"


@pytest.mark.invariant
def test_unauthorized_response_keeps_canonical_shape(client):
    """走真实 HTTP 路径验证 401 的响应体形状（这是可达分支，非仅测试可构造）。"""
    response = client.get("/me")
    assert response.status_code == 401
    payload = response.json()
    assert set(payload) == CANONICAL
    # 对外不复用内部错误码。
    assert payload["code"] == "UNAUTHENTICATED"


@pytest.mark.invariant
def test_cross_project_response_is_404_with_canonical_shape(client, auth_headers):
    """跨项目访问以 404 呈现，且形状与规范一致（不暴露出存在性差异）。"""
    response = client.get("/projects/proj_nonexistent/audit", headers=auth_headers())
    assert response.status_code == 404
    payload = response.json()
    assert set(payload) == CANONICAL
    assert payload["code"] == "NOT_FOUND"


@pytest.mark.invariant
def test_platform_error_cannot_be_retryable_and_ask_for_reconciliation():
    """模型级强制：对账类错误不得被标成可自动重试。"""
    with pytest.raises(ValueError):
        PlatformError(
            code=ErrorCode.RECONCILIATION_REQUIRED, message="x", retryable=True
        )


# --------------------------------------------------------------- 追踪 id 真的有值


@pytest.mark.invariant
def test_deny_does_not_swallow_request_id_into_details():
    """`deny(..., request_id=...)` 必须绑到字段，而不是塞进 `details`。

    这是自查发现的静默失效：`request_id` 曾只能靠 `**details` 传递，
    于是 `deny(code, msg, request_id="r1")` 把 id 放进了一个**不会出现在
    错误响应里**的字典 —— 调用方以为写了追踪 id，响应里却是 null，且不报错。
    """
    error = deny(ErrorCode.POLICY_DENIED, "x", request_id="r1")
    assert error.request_id == "r1"
    assert "request_id" not in error.details
    assert error.to_payload()["request_id"] == "r1"


@pytest.mark.invariant
@pytest.mark.parametrize(
    ("path", "expected_status"),
    [("/me", 401), ("/projects/proj_nope/audit", 404)],
)
def test_every_error_response_carries_a_request_id(
    client, auth_headers, path, expected_status
):
    """对外错误必须带**非空**追踪 id（总设计 §8）。

    自查前的实际状态是：`PlatformError.request_id` 没有任何 raise 点设置过，
    所以每条错误响应的 `request_id` 都是 null —— 字段在、值是空的。
    这不是"小问题"：出问题时拿不到可检索的 id，等于排障只能靠时间戳猜。
    """
    headers = {} if expected_status == 401 else auth_headers()
    response = client.get(path, headers=headers)

    assert response.status_code == expected_status
    payload = response.json()
    assert payload["request_id"], f"{path} 的错误响应没有追踪 id"
    # 锁到我们自己的生成器，避免测试被"随便一个非空字符串"糊弄过去。
    assert payload["request_id"].startswith("req_"), payload["request_id"]
    # 响应头与响应体同源，方便按 id 检索服务端日志。
    assert response.headers["X-Request-Id"] == payload["request_id"]


@pytest.mark.invariant
def test_request_id_is_generated_per_request(client):
    """每次请求都必须是不同的 id。

    单看"非空"是能被常量骗过去的 —— 一个硬编码的固定 id 也能让上面的断言通过，
    而它作为追踪标识毫无用处。这里用两次请求互不相同来排除那种情况。
    """
    first = client.get("/me")
    second = client.get("/me")
    assert first.json()["request_id"] != second.json()["request_id"]


@pytest.mark.invariant
def test_successful_response_also_echoes_request_id(client, auth_headers):
    """成功请求也回写追踪 id —— 否则「这条日志属于哪个请求」依然无从关联。"""
    response = client.get("/me", headers=auth_headers())
    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
