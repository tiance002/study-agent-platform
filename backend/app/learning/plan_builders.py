"""Pure plan construction shared by manual and generated plan commands."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.core.ids import new_id
from app.identity.models import Principal
from app.learning.ports import COMPONENTS_V1, TaskAssessment
from app.product.models import (
    LearningPlan,
    LearningTask,
    Milestone,
    PlanBundle,
    PlanStatus,
    TaskStatus,
)


def build_manual_bundle(
    actor: Principal,
    project_id: str,
    goal: str,
    milestone_specs: Sequence[tuple[str, str, Sequence[str]]],
    *,
    now: datetime,
) -> PlanBundle:
    """Build a complete plan while leaving version allocation to the repository."""
    plan_id = new_id("plan")
    milestones: list[Milestone] = []
    tasks: list[LearningTask] = []
    for order, (title, description, task_titles) in enumerate(milestone_specs):
        milestone_id = new_id("mile")
        milestones.append(
            Milestone(milestone_id, actor.tenant_id, project_id, plan_id, order, title, description)
        )
        for task_order, task_title in enumerate(task_titles):
            tasks.append(
                LearningTask(
                    new_id("task"), actor.tenant_id, project_id, milestone_id,
                    task_order, task_title, TaskStatus.PENDING,
                )
            )
    return PlanBundle(
        LearningPlan(plan_id, actor.tenant_id, project_id, 1, goal, PlanStatus.ACTIVE, now),
        tuple(milestones),
        tuple(tasks),
    )


def build_generated_bundle(
    actor: Principal, project_id: str, goal: str, summary: str, *, now: datetime
) -> tuple[PlanBundle, tuple[TaskAssessment, ...]]:
    """Build the fixed three-stage starter plan and its assessment mapping."""
    context = f"{goal.strip() or '完成学习目标'}（{summary}）"[:120]
    stage_specs = (
        ("理解核心概念", f"理解并解释：{context}"),
        ("完成实践练习", f"动手完成一个练习：{context}"),
        ("复盘与迁移", f"总结并迁移到新情境：{context}"),
    )
    plan_id = new_id("plan")
    milestones: list[Milestone] = []
    tasks: list[LearningTask] = []
    assessments: list[TaskAssessment] = []
    for order, ((milestone_title, task_title), component) in enumerate(
        zip(stage_specs, COMPONENTS_V1, strict=True)
    ):
        milestone_id = new_id("mile")
        task_id = new_id("task")
        milestones.append(
            Milestone(milestone_id, actor.tenant_id, project_id, plan_id, order, milestone_title, "")
        )
        tasks.append(
            LearningTask(task_id, actor.tenant_id, project_id, milestone_id, 0, task_title, TaskStatus.PENDING)
        )
        assessments.append(
            TaskAssessment(new_id("asm"), task_id, component, "self-report/v1", "graph-v1/task-v1")
        )
    return (
        PlanBundle(
            LearningPlan(plan_id, actor.tenant_id, project_id, 1, goal, PlanStatus.ACTIVE, now),
            tuple(milestones),
            tuple(tasks),
        ),
        tuple(assessments),
    )
