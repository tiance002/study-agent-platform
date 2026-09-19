"""本轮审查修复的回归测试：确认的原子性与预算绑定、畸形令牌、字段拒绝。

这一组测试的共同点：**它们证明的是「机制真的在拦」，而不只是「接口能返回」。**

- 确认的单次消费必须是**原子**的 —— 判断与占用分成两步就会被并发穿透；
- 用户确认的预算上界必须**真的在执行时被校验** —— 否则它只是界面上的装饰；
- 确认不能在策略/预算之前被消费 —— 否则一次被拒的策略判定会白白作废用户的确认；
- 畸形令牌必须是 401 而不是 500 —— 让攻击者能触发 500 这件事本身就是缺陷。
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from app.budget.ledger import Dimension
from app.core.errors import ErrorCode, PlatformError
from app.execution.confirmation import BudgetCeiling, ConfirmationStore

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
CURRENCY = str(Dimension.CURRENCY_MICROS)
TOOL = "append_project_evidence"
PARAMS = {"a": 1}

# `racy_scheduling` 已移到 `conftest.py` —— 它现在是多处复用的基建。


def _new_store(ceiling_amount: int = 100) -> tuple[ConfirmationStore, str]:
    store = ConfirmationStore()
    record = store.create(
        tenant_id="t1",
        project_id="p1",
        principal_id="u1",
        tool_id=TOOL,
        params=PARAMS,
        budget_ceiling=BudgetCeiling(dimension=CURRENCY, amount=ceiling_amount),
        issued_at=NOW,
    )
    return store, record.confirmation_id


def _consume(store: ConfirmationStore, confirmation_id: str, reserved: dict | None = None):
    return store.consume(
        confirmation_id,
        tenant_id="t1",
        project_id="p1",
        principal_id="u1",
        tool_id=TOOL,
        params=PARAMS,
        now=NOW,
        reserved_budget=reserved,
    )


# --------------------------------------------------------------- 确认：原子性


@pytest.mark.invariant
def test_confirmation_is_consumed_exactly_once_under_concurrency(racy_scheduling):
    """并发下同一确认只能被消费一次。

    这是被复现过的缺陷：原实现是「读 → 判断 → 写」三步，
    FastAPI 的同步路由运行在线程池里，所以两个请求真的会同时通过判断。

    ⚠️ 这个测试**必须**配合 `racy_scheduling`。默认的 5ms 切换间隔下，
    连无锁实现都能 100 轮全过 —— 那样这个测试就是在自我安慰。
    加了 fixture 之后，无锁实现在同样条件下 100 轮里穿透 92 轮。
    """
    store, confirmation_id = _new_store()
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            _consume(store, confirmation_id)
            result = "consumed"
        except PlatformError:
            result = "rejected"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("consumed") == 1, f"应当恰好成功一次，实际：{outcomes}"
    assert outcomes.count("rejected") == workers - 1


# --------------------------------------------------------------- 确认：时机


@pytest.mark.invariant
def test_peek_validates_without_consuming():
    """`peek` 只校验、不消费。

    顺序上的意义：策略判定需要知道「是否已确认」，但确认不能在策略之前作废 ——
    否则策略拒绝时用户什么都没执行，却要重新确认一次。
    """
    store, confirmation_id = _new_store()

    record = store.peek(
        confirmation_id,
        tenant_id="t1",
        project_id="p1",
        principal_id="u1",
        tool_id=TOOL,
        params=PARAMS,
        now=NOW,
    )
    assert record.consumed is False, "peek 不得改变状态"
    assert store.get(confirmation_id).consumed is False, "peek 之后确认必须仍然可用"

    _consume(store, confirmation_id)
    assert store.get(confirmation_id).consumed is True


@pytest.mark.invariant
def test_rejected_peek_leaves_confirmation_usable():
    """peek 校验失败（参数不符）也不得影响记录本身。"""
    store, confirmation_id = _new_store()

    with pytest.raises(PlatformError):
        store.peek(
            confirmation_id,
            tenant_id="t1",
            project_id="p1",
            principal_id="u1",
            tool_id=TOOL,
            params={"a": 999},  # 与确认时固化的参数不同
            now=NOW,
        )

    assert store.get(confirmation_id).consumed is False


# --------------------------------------------------------------- 确认：预算绑定


@pytest.mark.invariant
def test_reservation_above_confirmed_ceiling_is_rejected():
    """实际预留超出用户确认的上界 → 拒绝，且**不消耗**那条确认。

    如果没有这道校验，确认界面上写的金额就只是一句装饰性说明：
    服务端可以在用户同意 100 的情况下执行 1000。
    """
    store, confirmation_id = _new_store(ceiling_amount=100)

    with pytest.raises(PlatformError) as exc:
        _consume(store, confirmation_id, reserved={CURRENCY: 101})

    assert exc.value.code is ErrorCode.BUDGET_EXCEEDED
    assert store.get(confirmation_id).consumed is False, (
        "超出上界被拒时不能把确认消耗掉 —— 否则用户要为一个没执行的动作重新确认"
    )


@pytest.mark.invariant
def test_reservation_equal_to_ceiling_is_accepted():
    """等于上界是允许的：确认的意思是「不超过这个数」。"""
    store, confirmation_id = _new_store(ceiling_amount=100)

    record = _consume(store, confirmation_id, reserved={CURRENCY: 100})
    assert record.consumed is True


@pytest.mark.invariant
def test_budget_ceiling_must_be_a_real_number():
    """预算上界必须是具体数值，占位说明不再被接受。"""
    with pytest.raises(PlatformError):
        BudgetCeiling(dimension="", amount=1)

    with pytest.raises(PlatformError):
        BudgetCeiling(dimension=CURRENCY, amount=-1)


# --------------------------------------------------------------- 令牌：畸形输入

_COMPLETE_PAYLOAD = {
    "token_id": "sess_1",
    "principal_id": "u1",
    "tenant_id": "t1",
    "display_name": "",
    "roles": [],
    "issued_at": "2026-01-01T00:00:00+00:00",
    "expires_at": "2027-01-01T00:00:00+00:00",
}


def _encode(payload: object) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"{body.rstrip('=')}.sig"


MALFORMED_TOKENS: list[tuple[str, str]] = [
    ("空字符串", ""),
    ("没有分隔符", "not-a-token"),
    ("载荷不是 JSON", "!!!.sig"),
    # 这一条正是被报告的缺陷：载荷是 `{}` 时字段读取抛 KeyError，对外变成 500。
    ("载荷是空对象", _encode({})),
    ("载荷是数组", _encode([1, 2])),
    ("缺 token_id", _encode({k: v for k, v in _COMPLETE_PAYLOAD.items() if k != "token_id"})),
    ("缺 tenant_id", _encode({k: v for k, v in _COMPLETE_PAYLOAD.items() if k != "tenant_id"})),
    ("token_id 类型错误", _encode({**_COMPLETE_PAYLOAD, "token_id": 123})),
    ("roles 不是字符串序列", _encode({**_COMPLETE_PAYLOAD, "roles": [1, 2]})),
    ("时间没有时区", _encode({**_COMPLETE_PAYLOAD, "issued_at": "2026-01-01T00:00:00"})),
    ("时间无法解析", _encode({**_COMPLETE_PAYLOAD, "expires_at": "not-a-date"})),
    ("有效期早于签发时间", _encode({**_COMPLETE_PAYLOAD, "expires_at": "2025-01-01T00:00:00+00:00"})),
    ("超出长度上限", "a" * 9000),
]


@pytest.mark.invariant
@pytest.mark.parametrize(
    ("label", "raw"),
    MALFORMED_TOKENS,
    ids=[label for label, _ in MALFORMED_TOKENS],
)
def test_malformed_token_maps_to_auth_required(platform, label, raw):
    """任何畸形令牌都必须是 `AUTH_REQUIRED`，不能漏出别的异常类型。

    漏出去的后果不只是"少写了个 catch"：攻击者只要构造畸形令牌就能把
    「认证失败」变成「服务器错误」，既污染错误指标，也绕开了错误码契约。
    """
    with pytest.raises(PlatformError) as exc:
        platform.sessions.parse(raw)
    assert exc.value.code is ErrorCode.AUTH_REQUIRED, f"{label} 漏出了其他错误码"


@pytest.mark.invariant
@pytest.mark.parametrize(
    ("label", "raw"),
    MALFORMED_TOKENS,
    ids=[label for label, _ in MALFORMED_TOKENS],
)
def test_malformed_token_at_api_is_401_not_500(client, label, raw):
    """接入层同样必须是 401 —— 500 会告诉攻击者"我碰到东西了"。"""
    response = client.get("/me", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 401, f"{label} 返回了 {response.status_code}"


# --------------------------------------------------------------- 令牌：正常路径


@pytest.mark.invariant
def test_well_formed_token_still_round_trips(platform):
    """加严校验不能把正常令牌一起拦掉 —— 这是最容易误伤的地方。"""
    token = platform.sessions.issue(
        principal_id="u1",
        tenant_id="t1",
        display_name="张三",
        roles=("tenant_admin",),
        issued_at=platform.clock.now(),
        ttl=timedelta(minutes=30),
    )
    parsed = platform.sessions.parse(platform.sessions.serialize(token))

    assert parsed.principal_id == "u1"
    assert parsed.tenant_id == "t1"
    assert parsed.display_name == "张三"
    assert parsed.roles == ("tenant_admin",)
    assert parsed.issued_at.tzinfo is not None
