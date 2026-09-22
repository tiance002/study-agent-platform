"""学习闭环跨聚合命令的内存实现。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import ErrorCode, deny
from app.identity.models import Principal
from app.learning.evidence import EvidenceEvent
from app.learning.memory_store import InMemoryEvidenceRepository
from app.learning.ports import Diagnosis, TaskAssessment, TaskSubmission
from app.learning.rules import (
    self_report_verdict,
    validate_assessments,
    validate_submission_content,
)
from app.product.memory_store import InMemoryProductRepository
from app.product.models import PlanBundle, TaskStatus


@dataclass
class InMemoryLearningLoopRepository:
    products: InMemoryProductRepository
    evidence: InMemoryEvidenceRepository
    _assessments: dict[str, TaskAssessment] = field(default_factory=dict)
    _submissions: dict[str, list[TaskSubmission]] = field(default_factory=dict)
    _diagnoses: dict[str, list[Diagnosis]] = field(default_factory=dict)

    def install_generated_plan(
        self,
        actor: Principal,
        project_id: str,
        bundle: PlanBundle,
        assessments: tuple[TaskAssessment, ...],
    ) -> PlanBundle:
        self.products.membership.get(actor, project_id)
        validate_assessments(bundle, assessments)
        with self.products._lock:
            if any(item.task_id in self._assessments for item in assessments):
                raise deny(ErrorCode.EVIDENCE_IMMUTABLE, "任务评估契约已经冻结")
            saved = self.products._replace_plan_locked(project_id, bundle)
            self._assessments.update({item.task_id: item for item in assessments})
            return saved

    def record_diagnosis(
        self,
        actor: Principal,
        project_id: str,
        *,
        diagnosis_id: str,
        answers: dict,
        summary: str,
    ) -> Diagnosis:
        self.products.membership.get(actor, project_id)
        row = Diagnosis(
            diagnosis_id=diagnosis_id,
            project_id=project_id,
            principal_id=actor.principal_id,
            answers=dict(answers),
            summary=summary,
            created_at=self.products.clock.now(),
        )
        with self.products._lock:
            self._diagnoses.setdefault(project_id, []).append(row)
        return row

    def latest_diagnosis(
        self, actor: Principal, project_id: str
    ) -> Diagnosis | None:
        self.products.membership.get(actor, project_id)
        with self.products._lock:
            rows = [
                row for row in self._diagnoses.get(project_id, ())
                if row.principal_id == actor.principal_id
            ]
        return rows[-1] if rows else None

    def submit_self_report(
        self,
        actor: Principal,
        project_id: str,
        task_id: str,
        *,
        submission_id: str,
        content: str,
    ) -> tuple[TaskSubmission, EvidenceEvent]:
        validate_submission_content(content)
        self.products.membership.get(actor, project_id)
        with self.products._lock:
            found = self.products._find_task(task_id)
            if found is None or found[2].project_id != project_id:
                raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目")
            if found[2].status is not TaskStatus.IN_PROGRESS:
                raise deny(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    "只有进行中的任务可以提交练习",
                )
            assessment = self._assessments.get(task_id)
            if assessment is None:
                raise deny(ErrorCode.EVIDENCE_UNMAPPED, "任务没有事前冻结的评估契约")
            submission = TaskSubmission(
                submission_id=submission_id,
                project_id=project_id,
                task_id=task_id,
                principal_id=actor.principal_id,
                mode="self_report",
                content=content,
                created_at=self.products.clock.now(),
            )
            verdict = self_report_verdict(assessment.component_id, submission_id)
            event = self.evidence.append_learning(
                actor,
                project_id=project_id,
                task_id=task_id,
                contract_id=assessment.contract_id,
                mapping_version=assessment.mapping_version,
                verdicts=(verdict,),
            )
            self._submissions.setdefault(task_id, []).append(submission)
            return submission, event

    def submissions_for_task(
        self, actor: Principal, project_id: str, task_id: str
    ) -> tuple[TaskSubmission, ...]:
        self.products.get_task(actor, project_id, task_id)
        with self.products._lock:
            return tuple(
                row for row in self._submissions.get(task_id, ())
                if row.principal_id == actor.principal_id
            )
