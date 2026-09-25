# -*- coding: utf-8 -*-
"""A01 生成计划的质量不变量：真实分解，而不是回抄目标。

这些断言直接对应审查要求，逐条锁定生成的计划必须满足的硬性质量：
标题不回抄目标、不重复、是"动作 + 具体对象"；任务详情非空；前置只指向
同一计划内的更早任务且不成环；每个任务恰好一份评估契约；总时长不超预算。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from app.identity.models import Principal
from app.learning.plan_builders import (
    ASSESSMENT_CONTRACT_ID,
    MIN_PLAN_MINUTES,
    TASK_MAPPING_VERSION,
    build_generated_bundle,
    normalize_goal,
    select_domain,
)
from app.product.models import TASK_TYPES

ACTOR = Principal(principal_id="u_plan_quality", tenant_id="t_plan_quality")
NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)

#: 覆盖各领域与兜底的代表性目标。
REPRESENTATIVE_GOALS = (
    "我想知道怎么学习从0到1落地一个agent",
    "学 Python 后端开发",
    "掌握数据库索引与查询优化",
    "提高英语口语和听力",
    "备考雅思",
    "培养摄影构图能力",  # 命中不了任何领域 -> 通用兜底
)


def _build(goal: str, weekly_hours: int = 6):
    return build_generated_bundle(
        ACTOR, "proj_plan_quality", goal, weekly_hours=weekly_hours, now=NOW
    )


def _assert_quality(bundle, assessments, *, budget: int) -> None:
    milestones = {milestone.milestone_id for milestone in bundle.milestones}
    task_ids = [task.task_id for task in bundle.tasks]
    assert task_ids, "计划必须包含任务"
    assert len(set(task_ids)) == len(task_ids)

    normalized_goal = normalize_goal(bundle.plan.goal)
    titles = [task.title for task in bundle.tasks]
    assert len(set(titles)) == len(titles), "任务标题不得重复"

    for task in bundle.tasks:
        # 标题必须是"动作 + 具体对象"，且绝不回抄目标。
        assert not task.title.startswith("学习"), f"空泛标题：{task.title}"
        if normalized_goal:
            assert normalized_goal not in normalize_goal(task.title), (
                f"标题回抄了目标：{task.title}"
            )
        # 任务详情必须完整。
        assert task.objective.strip()
        assert task.instruction.strip()
        assert task.deliverable.strip()
        assert len(task.acceptance_criteria) >= 1
        assert all(item.strip() for item in task.acceptance_criteria)
        assert task.estimated_minutes > 0
        assert task.task_type in TASK_TYPES
        assert task.milestone_id in milestones
        # 前置只能引用同一计划内的任务，且必须更早（构造上因此不成环）。
        for prereq in task.prerequisites:
            assert prereq in task_ids
            assert task_ids.index(prereq) < task_ids.index(task.task_id)

    total = sum(task.estimated_minutes for task in bundle.tasks)
    assert total <= budget, f"计划总时长 {total} 超过预算 {budget}"

    # 每个任务恰好一份评估契约，组件取自 task_type 闭集，版本正确。
    mapped = {item.task_id: item for item in assessments}
    assert set(mapped) == set(task_ids)
    assert len(mapped) == len(assessments)
    for task in bundle.tasks:
        item = mapped[task.task_id]
        assert item.component_id == task.task_type
        assert item.component_id in {"concept", "practice", "reflection"}
        assert item.contract_id == ASSESSMENT_CONTRACT_ID
        assert item.mapping_version == TASK_MAPPING_VERSION


@pytest.mark.parametrize("goal", REPRESENTATIVE_GOALS)
@pytest.mark.parametrize("weekly_hours", [1, 6])
def test_generated_plan_holds_quality_invariants(goal, weekly_hours):
    bundle, assessments = _build(goal, weekly_hours)
    _assert_quality(
        bundle, assessments, budget=max(weekly_hours * 60, MIN_PLAN_MINUTES)
    )


def test_empty_goal_falls_back_to_generic_template():
    assert select_domain("") == "generic"
    bundle, assessments = _build("")
    _assert_quality(bundle, assessments, budget=max(6 * 60, MIN_PLAN_MINUTES))


def test_agent_goal_decomposes_into_concrete_actions():
    """用户真实例子：不得复述目标，且应落到模型调用/工具调用一类动作。"""
    goal = "我想知道怎么学习从0到1落地一个agent"
    bundle, _ = _build(goal)
    titles = [task.title for task in bundle.tasks]

    assert "agent" not in " ".join(titles).casefold(), "标题不得复述目标里的 agent"
    assert normalize_goal(goal) not in normalize_goal(" ".join(titles))
    joined = " ".join(titles)
    assert "模型调用" in joined or "工具调用" in joined, "应涉及模型调用/工具调用一类动作"


def test_weekly_budget_scales_task_minutes_down():
    """周时长很小时把任务时长等比压进预算，但每个任务仍至少 1 分钟。"""
    bundle, _ = _build("学 Python", weekly_hours=1)
    total = sum(task.estimated_minutes for task in bundle.tasks)
    assert total <= max(60, MIN_PLAN_MINUTES)
    assert all(task.estimated_minutes >= 1 for task in bundle.tasks)
