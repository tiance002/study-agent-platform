"""provider 适配器。

首版同时带 `ScriptedProvider`（模拟器）和一个离线可契约测试的真实 HTTP
适配器；真实云端是否可用仍由部署凭据与启动配置决定（见
`docs/adr/ADR-015-teaching-provider.md`）。
模拟器存在的意义不只是开发期占位 —— 反例矩阵里的"派发后超时""缺 usage"
"错误引用"等场景，在单测里**必须**由它复现，而不是等真服务偶尔抽风。

## ScriptedProvider 的契约

- **记录每一次调用**（完整请求快照），调用次数是测试断言的一部分，
  不是副产品 —— "越界输入调用次数为 0"这类性质只能靠它证明。
- 按脚本顺序回放 `ProviderResult`；脚本耗尽时**拒绝**而不是返回固定成功
  —— 固定成功会让"多调了一次"永远测不出来。
- 不做任何校验判断（那是 `teaching.validation` 的事）；
  也不做任何重试（那是编排层的显式决策）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from app.teaching.models import (
    ProviderRequest,
    ProviderResult,
    ProviderStatus,
    RawCitation,
    TokenUsage,
)

#: 模拟调用量的默认值。模块级单例：frozen dataclass 可安全共享，
#: 也避免在参数默认值里做函数调用。
SIMULATED_USAGE = TokenUsage(input_tokens=100, output_tokens=50)


def completed_result(
    *,
    attempt_id: str,
    answer: str,
    citations: Iterable[RawCitation] = (),
    usage: TokenUsage | None = SIMULATED_USAGE,
    provider_request_id: str = "",
) -> ProviderResult:
    """构造一个 completed 结果。`usage=None` 即"缺 usage"场景。"""
    return ProviderResult(
        attempt_id=attempt_id,
        status=ProviderStatus.COMPLETED,
        provider_request_id=provider_request_id,
        answer_text=answer,
        citations=tuple(citations),
        usage=usage,
    )


def refused_result(*, attempt_id: str, detail: str = "内容政策拒绝") -> ProviderResult:
    """provider 明确拒绝。调用已发生（可能计费），但没有答案。"""
    return ProviderResult(
        attempt_id=attempt_id,
        status=ProviderStatus.REFUSED,
        detail=detail,
        usage=TokenUsage(input_tokens=100, output_tokens=0),
    )


def timeout_result(*, attempt_id: str) -> ProviderResult:
    """派发后超时：结果未知。**绝不能带 usage**（模型层已强制）。"""
    return ProviderResult(
        attempt_id=attempt_id,
        status=ProviderStatus.TIMEOUT,
        detail="截止时间内未收到响应；provider 是否已处理未知",
    )


def dispatch_failed_result(*, attempt_id: str, detail: str = "连接被拒") -> ProviderResult:
    """可证明未送达：没有调用就没有费用。"""
    return ProviderResult(
        attempt_id=attempt_id,
        status=ProviderStatus.DISPATCH_FAILED,
        detail=detail,
    )


def malformed_result(*, attempt_id: str, detail: str = "响应不是合法的答案 JSON") -> ProviderResult:
    """响应到达但解析不出答案契约。调用已发生。"""
    return ProviderResult(
        attempt_id=attempt_id,
        status=ProviderStatus.MALFORMED,
        detail=detail,
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )


def truncated_result(*, attempt_id: str) -> ProviderResult:
    """响应被截断（max_output_tokens 打满 / finish 长度特征）。调用已发生。"""
    return ProviderResult(
        attempt_id=attempt_id,
        status=ProviderStatus.TRUNCATED,
        detail="响应不完整（输出长度达到上限）",
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )


def parse_answer_payload(attempt_id: str, raw_text: str) -> ProviderResult:
    """把 provider 的原始响应文本解析成领域结果（真实适配器同款路径）。

    模拟器用它从脚本里的 JSON 字符串走**与真实适配器相同**的解析代码，
    这样"畸形 JSON → MALFORMED"测的就不是模拟器自己的 if 分支。
    """
    payload = _load_answer_object(raw_text)
    if payload is None:
        return malformed_result(attempt_id=attempt_id, detail="响应不是合法 JSON")
    answer = payload.get("answer_markdown")
    if not isinstance(answer, str) or not answer.strip():
        return malformed_result(attempt_id=attempt_id, detail="缺少 answer_markdown")
    raw_citations = payload.get("citations", [])
    if not isinstance(raw_citations, list):
        return malformed_result(attempt_id=attempt_id, detail="citations 不是数组")
    citations: list[RawCitation] = []
    for item in raw_citations:
        if not isinstance(item, dict):
            return malformed_result(attempt_id=attempt_id, detail="引用项不是对象")
        try:
            citations.append(
                RawCitation(
                    source_id=item.get("source_id", ""),
                    document_id=item.get("document_id", ""),
                    span_start=item.get("span_start", -1),
                    span_end=item.get("span_end", -1),
                    content_hash=item.get("content_hash", ""),
                )
            )
        except TypeError:
            return malformed_result(attempt_id=attempt_id, detail="引用字段类型错误")
    return completed_result(
        attempt_id=attempt_id,
        answer=answer,
        citations=citations,
    )


def _load_answer_object(raw_text: str) -> dict | None:
    """Load strict JSON, tolerating one Markdown JSON fence from a provider.

    The fence is presentation noise, not a license to recover arbitrary prose:
    the candidate must still decode as a top-level JSON object.  This keeps the
    answer contract strict while handling a common provider formatting quirk.
    """

    candidate = raw_text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"}:
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(candidate)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        return payload

    # Some providers place reasoning or a short preamble before the final
    # structured object.  Recover only a valid object that has the answer key;
    # never treat arbitrary braces or an inner citation object as the answer.
    decoder = json.JSONDecoder()
    candidates: list[dict] = []
    for index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(candidate[index:])
        except ValueError:
            continue
        if isinstance(parsed, dict) and "answer_markdown" in parsed:
            candidates.append(parsed)
    return candidates[-1] if candidates else None


class ScriptedProvider:
    """按脚本回放结果的模拟 provider。满足 `TeachingProvider` 协议。"""

    def __init__(self, script: Iterable[ProviderResult | str | Exception]) -> None:
        """`script` 的元素：

        - `ProviderResult`：原样回放（构造好的域结果）；
        - `str`：当作 provider 的原始响应文本，走 `parse_answer_payload`
          —— 与真实适配器同一条解析路径；
        - `Exception`：原样抛出（模拟"连结果分类都给不出"的故障，
          编排层必须保守处理）。

        脚本耗尽后调用 → 抛 `RuntimeError`：**拒绝**比"永远成功"诚实，
        后者会把"多调了一次"藏起来。
        """
        self._script: Iterator[ProviderResult | str | Exception] = iter(tuple(script))
        #: 每次调用的完整请求快照。测试断言"参数检查而非只检查最终文字"
        #: （反例矩阵第一行）靠它实现。
        self.calls: list[ProviderRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def generate(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request)
        try:
            step = next(self._script)
        except StopIteration:
            raise RuntimeError(
                "ScriptedProvider 脚本已耗尽；这次调用说明编排层多调了一次 provider"
            ) from None
        if isinstance(step, Exception):
            raise step
        if isinstance(step, str):
            return parse_answer_payload(request.attempt_id, step)
        return step
