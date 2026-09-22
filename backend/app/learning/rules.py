"""Pure validation and verdict rules shared by learning-loop adapters."""

from __future__ import annotations

from app.core.errors import ErrorCode, deny
from app.learning.evidence import (
    ComponentVerdict,
    Direction,
    IndependenceLevel,
    ObservationStrength,
    Validity,
)
from app.learning.ports import TaskAssessment
from app.product.models import PlanBundle


def validate_assessments(
    bundle: PlanBundle, assessments: tuple[TaskAssessment, ...]
) -> None:
    task_ids = {task.task_id for task in bundle.tasks}
    mapped_ids = {item.task_id for item in assessments}
    if task_ids != mapped_ids or len(mapped_ids) != len(assessments):
        raise deny(
            ErrorCode.EVIDENCE_UNMAPPED,
            "生成计划中的每个任务必须恰好冻结一份评估契约",
        )
    if any(
        not item.component_id or not item.contract_id or not item.mapping_version
        for item in assessments
    ):
        raise deny(ErrorCode.EVIDENCE_UNMAPPED, "评估契约字段不能为空")


def validate_submission_content(content: str) -> None:
    if not content or len(content) > 20_000:
        raise deny(
            ErrorCode.PARAMS_INVALID,
            "练习提交内容长度必须在 1 到 20000 个字符之间",
        )


def self_report_verdict(component_id: str, submission_id: str) -> ComponentVerdict:
    return ComponentVerdict(
        component_id=component_id,
        assessment_validity=Validity.VALID,
        observation_strength=ObservationStrength.OBS_1,
        source_reliability_ok=True,
        independence_level=IndependenceLevel.INTRODUCED,
        direction=Direction.POSITIVE,
        independence_group=submission_id,
    )
