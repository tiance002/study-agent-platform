from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from app.teaching.local_router import LocalQueryRewriter, LocalRouterError
from app.teaching.routing import RoutingDecision


def test_routing_decision_is_closed_and_round_trips_without_user_content() -> None:
    decision = RoutingDecision(
        query_rewrite_status="applied",
        reason_code="local_rewrite_accepted",
    )

    assert RoutingDecision.from_dict(decision.to_dict()) == decision
    assert decision.to_dict() == {
        "policy_version": "teaching-route/v1",
        "answer_route": "cloud",
        "query_rewrite_status": "applied",
        "reason_code": "local_rewrite_accepted",
    }
    assert "question" not in decision.to_dict()


def test_local_query_rewriter_uses_one_bounded_closed_schema_request() -> None:
    calls: list[tuple[str, dict, float]] = []

    def transport(endpoint: str, payload: dict, timeout: float) -> tuple[int, bytes]:
        calls.append((endpoint, payload, timeout))
        return (
            200,
            json.dumps({"choices": [{"message": {"content": '{"query":"数据库事务隔离"}'}}]}).encode(),
        )

    rewriter = LocalQueryRewriter(
        endpoint_url="http://127.0.0.1:11434/v1/chat/completions",
        model="local-instruct",
        timeout_seconds=1.5,
        transport=transport,
    )

    assert rewriter.rewrite("事务为什么需要隔离？") == "数据库事务隔离"
    assert len(calls) == 1
    endpoint, payload, timeout = calls[0]
    assert endpoint == "http://127.0.0.1:11434/v1/chat/completions"
    assert timeout == 1.5
    assert payload["model"] == "local-instruct"
    assert payload["max_tokens"] <= 128
    assert payload["stream"] is False
    assert "事务为什么需要隔离？" in payload["messages"][-1]["content"]


def test_local_query_rewriter_uses_only_the_controlled_loopback_http_fixture() -> None:
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(
                {"choices": [{"message": {"content": '{"query":"本地夹具"}'}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        rewriter = LocalQueryRewriter(
            endpoint_url=endpoint, model="fixture-model", timeout_seconds=1
        )
        assert rewriter.rewrite("原始问题") == "本地夹具"
        assert received[0]["messages"][-1]["content"] == "原始问题"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        '{"query":"改写","extra":"unexpected"}',
        '{"query":""}',
        '{"query":' + json.dumps("太长" * 1200, ensure_ascii=False) + "}",
    ],
)
def test_local_query_rewriter_rejects_unbounded_or_open_schema_output(content: str) -> None:
    rewriter = LocalQueryRewriter(
        endpoint_url="http://127.0.0.1:11434/v1/chat/completions",
        model="local-instruct",
        timeout_seconds=1.5,
        transport=lambda *_args: (
            200,
            json.dumps({"choices": [{"message": {"content": content}}]}).encode(),
        ),
    )

    with pytest.raises(LocalRouterError):
        rewriter.rewrite("事务隔离")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:11434/v1/chat/completions",
        "http://192.168.1.2:11434/v1/chat/completions",
        "http://localhost:11434/v1/chat/completions",
        "http://127.0.0.1:11434/v1/chat/completions?next=https://example.com",
    ],
)
def test_local_query_rewriter_rejects_nonliteral_loopback_endpoint(endpoint: str) -> None:
    with pytest.raises(ValueError):
        LocalQueryRewriter(
            endpoint_url=endpoint,
            model="local-instruct",
            timeout_seconds=1.5,
            transport=lambda *_args: (200, b"{}"),
        )
