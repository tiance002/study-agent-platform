"""教学运行 HTTP 接口的定向测试（第五轮任务 5）。

覆盖：幂等创建、provider 显式开关、状态查询的 404 同形、
SSE 的 Last-Event-ID 续传与批次模型、断线重连不重复生成。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from app.core.hashing import content_hash
from app.identity.models import Principal
from app.knowledge.models import CHUNK_PARSER_VERSION, StoredChunk
from app.main import PlatformState
from app.teaching.provider import ScriptedProvider
from app.workers.teaching import run_once
from fastapi.testclient import TestClient

CONTENT = "# 事务\n\nACID 是事务的四个性质。\n"
QUESTION = "什么是 ACID？"


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _actor_of(client: TestClient) -> Principal:
    """从会话回读当前身份（供直接调用仓储时构造 `Principal`，不绕过认证）。"""
    me = client.get("/me").json()
    return Principal(principal_id=me["principal_id"], tenant_id=me["tenant_id"])


def _seed_material(platform: PlatformState, project_id: str, actor: Principal) -> StoredChunk:
    """给定主体在项目里铺一份已切块的资料（检索命中用）。"""
    source_id = _unique("src")
    platform.products.register_source(
        actor,
        project_id,
        source_id=source_id,
        display_name="讲义.md",
        media_type="text/markdown",
        identity_hash=f"sha256:{uuid.uuid4().hex}",
        acquisition={"kind": "upload"},
    )
    document, job = platform.ingestion.enqueue(
        actor,
        project_id,
        source_id,
        document_id=_unique("doc"),
        job_id=_unique("job"),
        title="讲义",
        content=CONTENT,
        media_type="text/markdown",
        language="zh",
    )
    claimed = platform.ingestion.claim_next(worker_id="seed", lease_seconds=300)
    assert claimed is not None
    chunk = StoredChunk(
        chunk_id=_unique("chk"),
        tenant_id=document.tenant_id,
        project_id=document.project_id,
        source_id=document.source_id,
        document_id=document.document_id,
        chunk_index=0,
        heading_path=(),
        heading_level=0,
        span_start=0,
        span_end=len(CONTENT),
        content=CONTENT,
        parser_version=CHUNK_PARSER_VERSION,
        created_at=datetime.now(timezone.utc),
    )
    platform.ingestion.complete(claimed, (chunk,))
    return chunk


def _cited_answer_script(chunk: StoredChunk, answer: str) -> str:
    return json.dumps(
        {
            "answer_markdown": answer,
            "citations": [
                {
                    "source_id": chunk.source_id,
                    "document_id": chunk.document_id,
                    "span_start": chunk.span_start,
                    "span_end": chunk.span_end,
                    "content_hash": content_hash(chunk.content),
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.fixture()
def teaching_env(platform, client, cookie_project):
    """cookie 登录 + 项目 + 会话 + 已切块资料 + scripted provider。"""
    client_, project_id = cookie_project
    conversation = client_.post(
        f"/projects/{project_id}/conversations",
        json={"title": "教学会话"},
        headers={"Origin": "http://testserver", "Idempotency-Key": _unique("idem")},
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["conversation_id"]
    chunk = _seed_material(platform, project_id, _actor_of(client_))
    return client_, platform, project_id, conversation_id, chunk


def _create_run(client: TestClient, project_id: str, conversation_id: str, *, key: str | None = "k1"):
    headers = {"Origin": "http://testserver"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/projects/{project_id}/conversations/{conversation_id}/teaching-runs",
        json={"question": QUESTION},
        headers=headers,
    )


# ------------------------------------------------------------------ 创建


def test_create_requires_idempotency_key(teaching_env):
    client, _, project_id, conversation_id, _ = teaching_env
    response = _create_run(client, project_id, conversation_id, key=None)
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_create_with_disabled_provider_is_explicit(platform, client, cookie_project):
    """provider 未启用：明确 503 + 稳定错误码，绝不静默降级到模拟器。"""
    client_, project_id = cookie_project
    conversation = client_.post(
        f"/projects/{project_id}/conversations",
        json={"title": "x"},
        headers={"Origin": "http://testserver", "Idempotency-Key": _unique("idem")},
    )
    conversation_id = conversation.json()["conversation_id"]
    response = _create_run(client_, project_id, conversation_id)
    assert response.status_code == 503
    assert response.json()["code"] == "TEACHING_PROVIDER_DISABLED"


def test_create_returns_202_and_writes_user_message(teaching_env):
    client, platform, project_id, conversation_id, _ = teaching_env
    platform.teaching_provider = ScriptedProvider([])
    response = _create_run(client, project_id, conversation_id)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    messages = client.get(
        f"/projects/{project_id}/conversations/{conversation_id}/messages"
    ).json()["messages"]
    assert [m["role"] for m in messages] == ["user"]
    assert messages[0]["content"] == QUESTION


def test_same_key_same_body_replays_same_run(teaching_env):
    client, platform, project_id, conversation_id, _ = teaching_env
    platform.teaching_provider = ScriptedProvider([])
    first = _create_run(client, project_id, conversation_id, key="same-key")
    assert first.status_code == 202
    second = _create_run(client, project_id, conversation_id, key="same-key")
    assert second.status_code == 202
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert second.json()["run_id"] == first.json()["run_id"]


def test_same_key_different_body_is_rejected(teaching_env):
    client, platform, project_id, conversation_id, _ = teaching_env
    platform.teaching_provider = ScriptedProvider([])
    first = _create_run(client, project_id, conversation_id, key="k-conflict")
    assert first.status_code == 202
    conflict = client.post(
        f"/projects/{project_id}/conversations/{conversation_id}/teaching-runs",
        json={"question": "另一个问题"},
        headers={"Origin": "http://testserver", "Idempotency-Key": "k-conflict"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_VIOLATION"


def test_body_rejects_unknown_fields(teaching_env):
    """客户端只允许 question：role/provider/usage 等字段一律拒绝。"""
    client, platform, project_id, conversation_id, _ = teaching_env
    platform.teaching_provider = ScriptedProvider([])
    response = client.post(
        f"/projects/{project_id}/conversations/{conversation_id}/teaching-runs",
        json={"question": QUESTION, "role": "system", "model": "gpt-x"},
        headers={"Origin": "http://testserver", "Idempotency-Key": _unique("idem")},
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ 执行


def test_full_flow_status_messages_and_events(teaching_env):
    client, platform, project_id, conversation_id, chunk = teaching_env
    platform.teaching_provider = ScriptedProvider(
        [_cited_answer_script(chunk, "ACID 指原子性等四个性质。")]
    )
    created = _create_run(client, project_id, conversation_id)
    run_id = created.json()["run_id"]

    assert run_once(platform, worker_id="w-http") == "succeeded"

    status = client.get(f"/projects/{project_id}/teaching-runs/{run_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "succeeded"
    assert body["grounding"] == "sourced"
    assert body["citations"] == [
        {
            "source_id": chunk.source_id,
            "document_id": chunk.document_id,
            "span_start": chunk.span_start,
            "span_end": chunk.span_end,
            "content_hash": content_hash(chunk.content),
        }
    ]
    assert body["next_action"] == "read_answer"
    assert body["answer_message_id"]

    messages = client.get(
        f"/projects/{project_id}/conversations/{conversation_id}/messages"
    ).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["message_id"] == body["answer_message_id"]

    events = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events",
        headers={"Origin": "http://testserver"},
    )
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    payload = events.text
    assert "event: run.created" in payload
    assert "event: run.dispatched" in payload
    assert "event: run.succeeded" in payload


# ------------------------------------------------------------------ 隔离


def test_run_of_another_project_is_404(teaching_env, platform):
    client, _, project_id, conversation_id, _ = teaching_env
    platform.teaching_provider = ScriptedProvider([])
    run_id = _create_run(client, project_id, conversation_id).json()["run_id"]

    # 另一个项目（同主体）：同形 404 —— 不能通过状态码差异探测资源存在。
    other_project = client.post(
        "/projects",
        json={"name": "别的项目"},
        headers={"Origin": "http://testserver", "Idempotency-Key": _unique("idem")},
    ).json()["project_id"]
    for path in (
        f"/projects/{other_project}/teaching-runs/{run_id}",
        f"/projects/{other_project}/teaching-runs/{run_id}/events",
        f"/projects/{project_id}/teaching-runs/run_unknown",
    ):
        response = client.get(path, headers={"Origin": "http://testserver"})
        assert response.status_code == 404, path


# ------------------------------------------------------------------ SSE


def test_last_event_id_replays_only_later_events(teaching_env):
    client, platform, project_id, conversation_id, chunk = teaching_env
    platform.teaching_provider = ScriptedProvider(
        [_cited_answer_script(chunk, "带引用的回答。")]
    )
    run_id = _create_run(client, project_id, conversation_id).json()["run_id"]
    assert run_once(platform, worker_id="w-sse") == "succeeded"

    first = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events",
        headers={"Origin": "http://testserver"},
    )
    all_ids = [
        int(line.split(": ", 1)[1])
        for line in first.text.splitlines()
        if line.startswith("id: ")
    ]
    assert all_ids == [1, 2, 3]

    resumed = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events",
        headers={"Origin": "http://testserver", "Last-Event-ID": "1"},
    )
    resumed_ids = [
        int(line.split(": ", 1)[1])
        for line in resumed.text.splitlines()
        if line.startswith("id: ")
    ]
    assert resumed_ids == [2, 3]  # 只回放之后的批次

    bad = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events",
        headers={"Origin": "http://testserver", "Last-Event-ID": "not-a-number"},
    )
    assert bad.status_code == 400


def test_reconnect_after_restart_replays_without_new_generation(teaching_env):
    """断线 → 重新装配平台 → 重连：同一最终消息与费用，不新增 provider 调用。"""
    client, platform, project_id, conversation_id, chunk = teaching_env
    provider = ScriptedProvider([_cited_answer_script(chunk, "持久化的回答。")])
    platform.teaching_provider = provider
    run_id = _create_run(client, project_id, conversation_id).json()["run_id"]
    assert run_once(platform, worker_id="w1") == "succeeded"
    actor = _actor_of(client)
    budget_before = platform.teaching.budget_snapshot(actor, project_id)

    # "重启"：同一平台对象上重连（内存适配器无跨进程；PG 端到端在任务 6）。
    events_after = client.get(
        f"/projects/{project_id}/teaching-runs/{run_id}/events",
        headers={"Origin": "http://testserver", "Last-Event-ID": "1"},
    )
    assert "event: run.succeeded" in events_after.text
    assert provider.call_count == 1  # 重连没有触发 generate

    budget_after = platform.teaching.budget_snapshot(actor, project_id)
    assert budget_after == budget_before  # 费用不变
