"""第六轮先行反例：每条测试必须在旧实现上先失败。"""

from __future__ import annotations

from app.core.hashing import content_hash
from app.teaching.models import ProviderResult, ProviderStatus, RawCitation
from app.teaching.provider import ScriptedProvider, completed_result
from app.teaching.runs import RunStatus
from app.workers.teaching import run_once
from test_teaching_recovery import _start_run, _world


def test_expired_dispatched_attempt_becomes_reconciliation_without_redispatch():
    world = _world(provider_script=[])
    run = _start_run(world)
    claim = world.teaching.claim_run(worker_id="crashed", lease_seconds=1)
    assert claim is not None
    world.teaching.mark_dispatched(
        claim,
        attempt_id="att_crashed",
        estimated_input_tokens=100,
        estimated_output_tokens=2000,
    )
    world.clock.advance(seconds=2)

    outcome = run_once(world.platform, worker_id="recovery")

    assert outcome == "reconciliation_required"
    assert world.provider.call_count == 0
    assert world.teaching.get_run(world.actor, world.project_id, run.run_id).status is RunStatus.RECONCILIATION_REQUIRED


def test_dispatched_failure_without_authoritative_usage_becomes_reconciliation():
    world = _world(
        provider_script=[
            ProviderResult(
                attempt_id="att_x",
                status=ProviderStatus.REFUSED,
                usage=None,
                detail="provider refused",
            )
        ]
    )
    run = _start_run(world)

    outcome = run_once(world.platform, worker_id="worker")

    assert outcome == "reconciliation_required"
    assert world.teaching.get_run(world.actor, world.project_id, run.run_id).status is RunStatus.RECONCILIATION_REQUIRED


def test_full_context_budget_is_checked_before_provider_call():
    world = _world(
        provider_script=[completed_result(attempt_id="att_x", answer="should not run")]
    )
    run = _start_run(world, budget_total_micro=4010)

    outcome = run_once(world.platform, worker_id="worker")

    assert outcome == "failed"
    assert world.provider.call_count == 0
    fresh = world.teaching.get_run(world.actor, world.project_id, run.run_id)
    assert fresh.status is RunStatus.FAILED
    assert world.teaching.budget_snapshot(world.actor, world.project_id)["project"]["available_micro"] >= 0


def test_future_messages_are_excluded_from_earlier_run_context():
    world = _world(provider_script=[completed_result(attempt_id="att_x", answer="answer")])
    first = _start_run(world, question="first question")
    world.clock.advance(seconds=1)
    _start_run(world, question="FUTURE_QUESTION")

    assert run_once(world.platform, worker_id="worker") == "succeeded"
    request = world.provider.calls[0]
    assert all("FUTURE_QUESTION" not in message.content for message in request.messages)
    assert request.model == first.model_id


def test_verified_citations_are_exposed_on_completed_run():
    world = _world(provider_script=[])
    chunk = world.chunk
    citation = RawCitation(
        source_id=chunk.source_id,
        document_id=chunk.document_id,
        span_start=chunk.span_start,
        span_end=chunk.span_end,
        content_hash=content_hash(chunk.content),
    )
    world.platform.teaching_provider = ScriptedProvider(
        [completed_result(attempt_id="att_x", answer="answer", citations=[citation])]
    )
    run = _start_run(world)

    assert run_once(world.platform, worker_id="worker") == "succeeded"
    result = world.teaching.get_run(world.actor, world.project_id, run.run_id).to_dict()
    assert result["citations"] == [
        {
            "source_id": citation.source_id,
            "document_id": citation.document_id,
            "span_start": citation.span_start,
            "span_end": citation.span_end,
            "content_hash": citation.content_hash,
        }
    ]


def test_provider_configuration_is_frozen_on_run_not_worker_process():
    world = _world(provider_script=[completed_result(attempt_id="att_x", answer="answer")])
    run = _start_run(world)
    world.platform.settings = type("Settings", (), {"teaching_model": "model-new"})()

    assert run_once(world.platform, worker_id="worker") == "succeeded"
    assert world.provider.calls[0].model == run.model_id
