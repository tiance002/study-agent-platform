"""Integration checks for the platform-wide paid-provider cap."""

from types import SimpleNamespace

from app.budget.platform import PlatformPaidBudget
from app.teaching.provider import timeout_result
from app.workers.teaching import run_once
from test_teaching_recovery import _start_run, _world


def test_paid_provider_worker_reserves_shared_platform_cap_before_dispatch():
    world = _world(
        provider_script=[
            timeout_result(attempt_id="att_x"),
            timeout_result(attempt_id="att_y"),
        ]
    )
    # A scripted provider is used as a deterministic stand-in, but the server
    # configuration identifies this worker path as the paid provider.
    world.platform.settings = SimpleNamespace(
        teaching_provider="openai",
        teaching_model="test-model-v1",
        teaching_max_input_tokens=8000,
        teaching_max_output_tokens=2000,
    )
    world.platform.platform_paid_budget = PlatformPaidBudget(
        monthly_cap_micro=6_000,
        paid_dispatch_enabled=True,
    )
    _start_run(world)
    assert run_once(world.platform, worker_id="w1") == "reconciliation_required"

    _start_run(world, question="第二次请求")
    assert run_once(world.platform, worker_id="w2") == "failed"
    assert world.provider.call_count == 1
