"""真实 HTTP provider 传输契约：只使用本地服务器，不产生云端费用。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from app.teaching.models import ProviderStatus, TokenUsage
from app.teaching.openai_provider import OpenAIProviderError, OpenAIResponsesProvider
from test_teaching_provider import a_request


class _ResponsesHandler(BaseHTTPRequestHandler):
    response_body: dict = {}
    response_status = 200
    seen_headers: dict[str, str] = {}

    def do_POST(self):  # noqa: N802
        type(self).seen_headers = {key.lower(): value for key, value in self.headers.items()}
        self.rfile.read(int(self.headers.get("content-length", "0")))
        body = json.dumps(type(self).response_body).encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture()
def response_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ResponsesHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_openai_responses_adapter_parses_answer_and_usage(response_server):
    _ResponsesHandler.response_status = 200
    _ResponsesHandler.response_body = {
        "id": "resp_test",
        "status": "completed",
        "output_text": json.dumps({"answer_markdown": "答案", "citations": []}),
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    provider = OpenAIResponsesProvider(
        api_key="test-secret",
        base_url=f"http://127.0.0.1:{response_server.server_port}/v1",
    )

    result = provider.generate(a_request())

    assert result.status is ProviderStatus.COMPLETED
    assert result.provider_request_id == "resp_test"
    assert result.usage == TokenUsage(input_tokens=12, output_tokens=7)
    assert result.answer_text == "答案"
    assert _ResponsesHandler.seen_headers["authorization"] == "Bearer test-secret"


def test_openai_responses_adapter_allows_plain_text_only_without_materials(response_server):
    _ResponsesHandler.response_status = 200
    _ResponsesHandler.response_body = {
        "id": "resp_plain",
        "status": "completed",
        "output_text": "函数是一段可复用的代码。",
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    provider = OpenAIResponsesProvider(
        api_key="test-secret",
        base_url=f"http://127.0.0.1:{response_server.server_port}/v1",
    )

    result = provider.generate(a_request(artifacts=()))

    assert result.status is ProviderStatus.COMPLETED
    assert result.answer_text == "函数是一段可复用的代码。"
    assert result.citations == ()
    assert result.usage == TokenUsage(input_tokens=12, output_tokens=7)


def test_openai_responses_adapter_keeps_plain_text_malformed_with_materials(response_server):
    _ResponsesHandler.response_status = 200
    _ResponsesHandler.response_body = {
        "id": "resp_plain_grounded",
        "status": "completed",
        "output_text": "函数是一段可复用的代码。",
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    provider = OpenAIResponsesProvider(
        api_key="test-secret",
        base_url=f"http://127.0.0.1:{response_server.server_port}/v1",
    )

    result = provider.generate(a_request())

    assert result.status is ProviderStatus.MALFORMED


def test_openai_responses_adapter_treats_server_error_as_unknown(response_server):
    _ResponsesHandler.response_status = 503
    _ResponsesHandler.response_body = {"error": {"message": "hidden"}}
    provider = OpenAIResponsesProvider(
        api_key="test-secret",
        base_url=f"http://127.0.0.1:{response_server.server_port}/v1",
    )

    with pytest.raises(OpenAIProviderError):
        provider.generate(a_request())
