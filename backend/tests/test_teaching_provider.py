"""教学 provider 边界的定向测试。

覆盖计划任务 1 的每一条验收点：

- 请求契约：越界/残缺的 `ProviderRequest` 在**构造时**就拒绝；
- 模拟器：记录调用、按脚本回放、脚本耗尽即拒绝（不是固定成功）；
- 状态语义：`TIMEOUT`（结果未知）与 `DISPATCH_FAILED`（可证明未送达）
  在模型层就不可能混淆 —— timeout 带 usage 直接构造失败；
- 答案解析：畸形 JSON / 缺键 / 非对象 / 引用形状错误 → `MALFORMED`；
- `ProviderResult` 的不变量：非 completed 不得携带答案、
  timeout 不得携带 usage。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from app.teaching.models import (
    MaterialSnippet,
    PromptMessage,
    PromptRole,
    ProviderRequest,
    ProviderResult,
    ProviderStatus,
    RawCitation,
    TokenUsage,
)
from app.teaching.ports import TeachingProvider
from app.teaching.provider import (
    ScriptedProvider,
    completed_result,
    dispatch_failed_result,
    parse_answer_payload,
    refused_result,
    timeout_result,
    truncated_result,
)

#: 一切测试共用的合法请求基座。
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

MATERIAL = MaterialSnippet(
    source_id="src_1",
    document_id="doc_1",
    chunk_id="chunk_1",
    span_start=0,
    span_end=5,
    content="幂等是同一次操作重复执行与执行一次效果相同。",
    content_hash="sha256:" + "a" * 16,
    parser_version="structure/v1",
)


def a_request(**overrides) -> ProviderRequest:
    """合法请求 + 可选覆盖。测试要改哪个字段一目了然。"""
    fields = dict(
        attempt_id="att_1",
        model="test-model-v1",
        prompt_version="teaching-sys/v1",
        messages=(
            PromptMessage(role=PromptRole.SYSTEM, content="system 指令"),
            PromptMessage(role=PromptRole.USER, content="什么是幂等？"),
        ),
        artifacts=(MATERIAL,),
        max_output_tokens=1000,
        deadline=NOW + timedelta(seconds=30),
    )
    fields.update(overrides)
    return ProviderRequest(**fields)


# ------------------------------------------------------------- 请求契约


def test_request_rejects_non_positive_output_limit():
    with pytest.raises(ValueError, match="max_output_tokens"):
        a_request(max_output_tokens=0)


def test_request_rejects_naive_deadline():
    with pytest.raises(ValueError, match="deadline"):
        a_request(deadline=datetime(2026, 9, 20, 12, 0))  # 无时区


def test_request_rejects_empty_messages_and_model():
    with pytest.raises(ValueError, match="messages"):
        a_request(messages=())
    with pytest.raises(ValueError, match="model"):
        a_request(model="")


def test_request_is_an_immutable_snapshot():
    """冻结契约：派发后改请求 = 重新派发，类型层不允许。"""
    request = a_request()
    with pytest.raises(AttributeError):  # frozen dataclass 写入即报错
        request.model = "other-model"  # type: ignore[misc]


# ------------------------------------------------------------- 结果不变量


def test_non_completed_result_cannot_carry_answer_text():
    """失败状态带半截答案会诱人把未校验文本当结论 —— 构造期直接拒绝。"""
    with pytest.raises(ValueError, match="completed"):
        ProviderResult(
            attempt_id="att_1",
            status=ProviderStatus.TIMEOUT,
            answer_text="看起来像答案的东西",
        )


def test_timeout_result_cannot_carry_usage():
    """结果未知时没有权威用量 —— 带着它就是凭空捏造事实。"""
    with pytest.raises(ValueError, match="usage"):
        timeout_result(attempt_id="att_1").__class__(
            attempt_id="att_1",
            status=ProviderStatus.TIMEOUT,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


def test_missing_usage_is_explicit_none_not_zero():
    """缺 usage 的成功结果必须建模为 None —— "没报"和"没花钱"是两回事。"""
    result = completed_result(attempt_id="att_1", answer="好", usage=None)
    assert result.usage is None


# ------------------------------------------------------------- 模拟器


def test_scripted_provider_records_every_call_with_full_request():
    """调用参数可断言（反例矩阵第一行：检查调用参数而非只看最终文字）。"""
    provider = ScriptedProvider([completed_result(attempt_id="att_1", answer="好")])
    request = a_request()
    provider.generate(request)

    assert provider.call_count == 1
    assert provider.calls[0] is request
    assert provider.calls[0].artifacts[0].source_id == "src_1"


def test_scripted_provider_rejects_when_script_exhausted():
    """脚本耗尽后再调用必须炸 —— 固定成功会让"多调了一次"永远测不出。"""
    provider = ScriptedProvider([completed_result(attempt_id="att_1", answer="好")])
    provider.generate(a_request())
    with pytest.raises(RuntimeError, match="脚本已耗尽"):
        provider.generate(a_request(attempt_id="att_2"))


def test_scripted_provider_replays_distinct_outcomes_in_order():
    provider = ScriptedProvider(
        [
            refused_result(attempt_id="a1"),
            timeout_result(attempt_id="a2"),
            dispatch_failed_result(attempt_id="a3"),
            truncated_result(attempt_id="a4"),
        ]
    )
    statuses = [
        provider.generate(a_request(attempt_id=f"a{n}")).status for n in range(1, 5)
    ]
    assert statuses == [
        ProviderStatus.REFUSED,
        ProviderStatus.TIMEOUT,
        ProviderStatus.DISPATCH_FAILED,
        ProviderStatus.TRUNCATED,
    ]


def test_scripted_provider_can_raise_through_adapter_failures():
    """Exception 直接穿透：编排层必须把"连分类都给不出"当未知处理。"""
    provider = ScriptedProvider([ConnectionError("网络炸了")])
    with pytest.raises(ConnectionError):
        provider.generate(a_request())


# ------------------------------------------------------------- 答案解析


def test_parse_valid_answer_payload():
    raw = (
        '{"answer_markdown": "幂等指重复执行与一次执行效果相同。",'
        ' "citations": [{"source_id": "src_1", "document_id": "doc_1",'
        ' "span_start": 0, "span_end": 5, "content_hash": "sha256:aaaa"}]}'
    )
    result = parse_answer_payload("att_1", raw)
    assert result.status is ProviderStatus.COMPLETED
    assert result.answer_text.startswith("幂等指")
    assert len(result.citations) == 1
    assert result.citations[0].document_id == "doc_1"


@pytest.mark.parametrize(
    "raw",
    [
        "不是 JSON 的文本",
        '{"answer_markdown": 42}',  # 缺答案 / 类型错
        '{"answer_markdown": "x", "citations": "不是数组"}',
        '{"answer_markdown": "x", "citations": [{"span_start": "零"}]}',
    ],
)
def test_parse_malformed_payloads_become_malformed(raw):
    """畸形响应必须归类为 MALFORMED，而不是抛异常让上层猜。"""
    result = parse_answer_payload("att_1", raw)
    assert result.status is ProviderStatus.MALFORMED
    assert result.answer_text == ""
    assert result.usage is not None  # 调用已发生（模拟器给的模拟用量）


def test_parse_malformed_does_not_invent_citations():
    result = parse_answer_payload("att_1", "垃圾")
    assert result.citations == ()


# ------------------------------------------------------------- 端口结构


def test_scripted_provider_satisfies_the_port():
    """结构化类型：ScriptedProvider 不继承任何东西也满足协议。"""
    provider = ScriptedProvider([])
    assert isinstance(provider, TeachingProvider)


def test_raw_citation_is_untrusted_input_kept_as_is():
    """未校验引用保留原样（哪怕看起来不可能）—— 校验是下游的职责，
    在这里提前"修正"会把两层校验合并成一层。"""
    citation = RawCitation(
        source_id="", document_id="doc_x", span_start=-5, span_end=-1, content_hash=""
    )
    assert citation.span_start == -5


def test_material_snippet_requires_exact_fields():
    with pytest.raises(ValueError, match="span_end"):
        MaterialSnippet(
            source_id="s",
            document_id="d",
            chunk_id="c",
            span_start=10,
            span_end=3,
            content="x",
            content_hash="sha256:x",
            parser_version="p",
        )
