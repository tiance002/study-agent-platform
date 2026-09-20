"""最小、无隐藏重试的 OpenAI Responses API 适配器。

只把供应商传输/响应形状转换成 ``TeachingProvider`` 领域结果；引用和答案
是否能用于项目仍由 service 的校验层决定。请求体不包含工具、历史会话 id
或供应商自动存储开关，避免把平台状态交给外部隐式保存。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.teaching.models import ProviderRequest, ProviderResult, ProviderStatus, TokenUsage
from app.teaching.provider import parse_answer_payload, truncated_result


class OpenAIProviderError(RuntimeError):
    """只用于把未知网络/供应商故障交给编排层对账。"""


class OpenAIResponsesProvider:
    """调用单次 Responses 请求；适配器不自动重试。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI provider 需要 API key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def generate(self, request: ProviderRequest) -> ProviderResult:
        remaining = max(
            0.1,
            min(self._timeout_seconds, (request.deadline - datetime.now(timezone.utc)).total_seconds()),
        )
        payload = {
            "model": request.model,
            "input": [
                {"role": str(message.role), "content": message.content}
                for message in request.messages
            ],
            "max_output_tokens": request.max_output_tokens,
            "store": False,
            "text": {"format": {"type": "json_object"}},
        }
        http_request = Request(
            f"{self._base_url}/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Client-Attempt-Id": request.attempt_id,
            },
            method="POST",
        )
        try:
            with urlopen(http_request, timeout=remaining) as response:  # noqa: S310 - URL is deployment config
                raw = response.read(4 * 1024 * 1024)
                headers = response.headers
        except HTTPError as exc:
            # Do not include response body or URL in the public error; the request
            # may have reached the provider, so the caller must reconcile.
            if exc.code in {408, 429} or exc.code >= 500:
                raise OpenAIProviderError("provider transport returned an uncertain error") from exc
            return ProviderResult(
                attempt_id=request.attempt_id,
                status=ProviderStatus.REFUSED,
                provider_request_id=exc.headers.get("x-request-id", ""),
                detail="provider 拒绝了请求",
            )
        except (TimeoutError, URLError, OSError) as exc:
            raise OpenAIProviderError("provider transport outcome is unknown") from exc

        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise OpenAIProviderError("provider returned an unreadable response") from exc
        if not isinstance(document, dict):
            return ProviderResult(
                attempt_id=request.attempt_id,
                status=ProviderStatus.MALFORMED,
                detail="provider 响应不是对象",
            )

        provider_request_id = str(
            document.get("id") or headers.get("x-request-id") or ""
        )
        usage = _usage(document.get("usage"))
        if document.get("status") == "incomplete":
            return replace(
                truncated_result(attempt_id=request.attempt_id),
                provider_request_id=provider_request_id,
                usage=usage,
            )
        text = document.get("output_text") or _output_text(document.get("output"))
        if not isinstance(text, str) or not text.strip():
            return ProviderResult(
                attempt_id=request.attempt_id,
                status=ProviderStatus.MALFORMED,
                provider_request_id=provider_request_id,
                usage=usage,
                detail="provider 没有返回文本答案",
            )
        parsed = parse_answer_payload(request.attempt_id, text)
        return replace(parsed, provider_request_id=provider_request_id, usage=usage)


def _usage(raw: object) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    try:
        return TokenUsage(
            input_tokens=int(raw["input_tokens"]),
            output_tokens=int(raw["output_tokens"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _output_text(raw: object) -> str:
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)
