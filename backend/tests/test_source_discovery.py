"""Source search provider contracts; no live provider requests are made."""

from __future__ import annotations

import json

import pytest
from app.knowledge.discovery import SearchResult, TavilySearchProvider


def test_tavily_search_requests_only_bounded_metadata() -> None:
    requests: list[tuple[dict, str]] = []

    def transport(payload: dict, api_key: str) -> tuple[int, bytes]:
        requests.append((payload, api_key))
        return 200, json.dumps(
            {
                "results": [
                    {
                        "title": "Guide",
                        "url": "https://example.com/guide",
                        "content": "A short snippet",
                    }
                ],
                "answer": "must not be requested",
            }
        ).encode()

    provider = TavilySearchProvider(api_key="search-secret-dev-only", transport=transport)
    results = provider.search("事务隔离", limit=10)

    assert results == (
        SearchResult(
            title="Guide",
            url="https://example.com/guide",
            snippet="A short snippet",
        ),
    )
    payload, key = requests[0]
    assert key == "search-secret-dev-only"
    assert payload["query"] == "事务隔离"
    assert payload["max_results"] == 10
    assert payload["include_answer"] is False
    assert payload["include_raw_content"] is False


@pytest.mark.parametrize("status", [401, 429, 500])
def test_tavily_search_returns_closed_error_without_provider_body(status: int) -> None:
    provider = TavilySearchProvider(
        api_key="search-secret-dev-only",
        transport=lambda _payload, _key: (status, b"private provider diagnostic"),
    )

    with pytest.raises(RuntimeError) as caught:
        provider.search("query", limit=5)

    assert "private provider diagnostic" not in str(caught.value)


def test_tavily_search_rejects_malformed_response() -> None:
    provider = TavilySearchProvider(
        api_key="search-secret-dev-only", transport=lambda _payload, _key: (200, b"[]")
    )

    with pytest.raises(RuntimeError, match="格式"):
        provider.search("query", limit=5)
