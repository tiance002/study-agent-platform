"""SSE 事件流的补充测试：批次模型、重连提示、至少一次投递。"""

from __future__ import annotations

import json

import pytest
from app.api import teaching_routes
from app.core.hashing import content_hash
from app.teaching.provider import ScriptedProvider
from app.workers.teaching import run_once
from test_teaching_api import (
    _create_run,
    _seed_material,
    _unique,
)

EVENTS_HEADERS = {"Origin": "http://testserver"}


@pytest.fixture()
def finished_run(platform, client, cookie_project):
    """创建并跑完一个带资料引用的运行，返回 (client, platform, project_id, run_id)。"""
    client_, project_id = cookie_project
    conversation = client_.post(
        f"/projects/{project_id}/conversations",
        json={"title": "s"},
        headers={"Origin": "http://testserver", "Idempotency-Key": _unique("idem")},
    )
    conversation_id = conversation.json()["conversation_id"]
    chunk = _seed_material(platform, project_id)
    citation = {
        "source_id": chunk.source_id,
        "document_id": chunk.document_id,
        "span_start": chunk.span_start,
        "span_end": chunk.span_end,
        "content_hash": content_hash(chunk.content),
    }
    platform.teaching_provider = ScriptedProvider(
        [
            json.dumps(
                {"answer_markdown": "答", "citations": [citation]}, ensure_ascii=False
            )
        ]
    )
    run_id = _create_run(client_, project_id, conversation_id).json()["run_id"]
    assert run_once(platform, worker_id="w-events") == "succeeded"
    return client_, platform, project_id, run_id


def _event_frames(text: str) -> list[tuple[str | None, str]]:
    """把 SSE 文本解析成 (id, event) 对。"""
    frames: list[tuple[str | None, str]] = []
    current_id: str | None = None
    current_event: str | None = None
    for line in text.splitlines():
        if line.startswith("id: "):
            current_id = line[4:]
        elif line.startswith("event: "):
            current_event = line[7:]
            frames.append((current_id, current_event))
            current_id, current_event = None, None
    return frames


def test_events_include_persistent_seq_ids_and_retry_hint(finished_run):
    client, _, project_id, run_id = finished_run
    response = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events", headers=EVENTS_HEADERS
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("retry: 3000\n\n")  # 重连提示，非业务事件
    frames = _event_frames(response.text)
    assert [(i, e) for i, e in frames] == [
        ("1", "run.created"),
        ("2", "run.dispatched"),
        ("3", "run.succeeded"),
    ]


def test_batch_limit_is_bounded_and_resumable(finished_run, monkeypatch):
    """批次上限：超过上限的连接只拿到一截 + 显式的"还有更多"标记。"""
    client, _, project_id, run_id = finished_run
    monkeypatch.setattr(teaching_routes, "MAX_EVENTS_PER_BATCH", 1)

    first = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events", headers=EVENTS_HEADERS
    )
    frames = _event_frames(first.text)
    assert frames == [(None, "batch.truncated")] or len(frames) <= 2
    assert "batch.truncated" in first.text  # 显式截断标记，不是静默截断

    # 客户端凭 Last-Event-ID 续传；续传批次同样有界（再来一截）。
    second = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events",
        headers={**EVENTS_HEADERS, "Last-Event-ID": "1"},
    )
    second_frames = _event_frames(second.text)
    assert ("2", "run.dispatched") in second_frames

    third = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events",
        headers={**EVENTS_HEADERS, "Last-Event-ID": "2"},
    )
    assert ("3", "run.succeeded") in _event_frames(third.text)


def test_two_connections_receive_identical_persisted_events(finished_run):
    """至少一次投递：同一批持久化事件对每个连接都可见（客户端按 id 去重）。"""
    client, _, project_id, run_id = finished_run
    texts = {
        client.get(
            f"/projects/{project_id}/teaching-runs/{run_id}/events",
            headers=EVENTS_HEADERS,
        ).text
        for _ in range(2)
    }
    assert len(texts) == 1  # 两次连接拿到逐字相同的事件流（来源是库，不是内存）


def test_worker_processing_does_not_emit_partial_answer_events(finished_run):
    """只发状态与已校验的最终结果：不存在"部分答案"类的事件类型。"""
    client, _, project_id, run_id = finished_run
    text = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events", headers=EVENTS_HEADERS
    ).text
    business_events = {event for _, event in _event_frames(text) if event}
    assert business_events <= {
        "run.created",
        "run.dispatched",
        "run.succeeded",
        "run.failed",
        "run.reconciliation_required",
        "batch.truncated",
    }
    assert not any("token" in e or "delta" in e or "chunk" in e for e in business_events)
