"""Bounded web-search provider adapter for source discovery."""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from typing import Callable, Protocol

SEARCH_MAX_RESULTS = 10
SEARCH_MAX_RESPONSE_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SourceSearchProvider(Protocol):
    def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]: ...


Transport = Callable[[dict, str], tuple[int, bytes]]


def _https_transport(payload: dict, api_key: str) -> tuple[int, bytes]:
    connection = http.client.HTTPSConnection("api.tavily.com", timeout=8.0)
    try:
        connection.request(
            "POST",
            "/search",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        body = response.read(SEARCH_MAX_RESPONSE_BYTES + 1)
        if len(body) > SEARCH_MAX_RESPONSE_BYTES:
            raise RuntimeError("搜索服务响应超过大小限制")
        return response.status, body
    finally:
        connection.close()


class TavilySearchProvider:
    """Tavily Search API adapter; excludes answer and full page content."""

    def __init__(self, *, api_key: str, transport: Transport | None = None) -> None:
        if not api_key:
            raise ValueError("搜索 provider 需要 API key")
        self._api_key = api_key
        self._transport = transport or _https_transport

    def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        normalized = query.strip()
        if not normalized or len(normalized) > 2000:
            raise ValueError("搜索关键词长度无效")
        if not 1 <= limit <= SEARCH_MAX_RESULTS:
            raise ValueError("搜索结果数量超出允许范围")
        payload = {
            "query": normalized,
            "search_depth": "basic",
            "max_results": limit,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_favicon": False,
            "include_usage": False,
        }
        try:
            status, raw = self._transport(payload, self._api_key)
        except Exception as exc:
            raise RuntimeError("搜索服务暂时不可用") from exc
        if status != 200:
            raise RuntimeError("搜索服务未能完成请求")
        if len(raw) > SEARCH_MAX_RESPONSE_BYTES:
            raise RuntimeError("搜索服务响应超过大小限制")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("搜索服务响应格式无效") from exc
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise RuntimeError("搜索服务响应格式无效")

        results: list[SearchResult] = []
        for item in data["results"][:limit]:
            if not isinstance(item, dict):
                continue
            title, url, snippet = item.get("title"), item.get("url"), item.get("content")
            if not isinstance(title, str) or not isinstance(url, str) or not isinstance(snippet, str):
                continue
            title, url, snippet = title.strip(), url.strip(), snippet.strip()
            if not title or not url:
                continue
            results.append(SearchResult(title[:300], url[:2048], snippet[:2000]))
        return tuple(results)
