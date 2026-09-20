"""学习闭环跨表命令的 PostgreSQL 原子实现。"""

from __future__ import annotations

import json

from app.core.errors import ErrorCode, deny
from app.db.evidence_store import PostgresEvidenceRepository
from app.db.product_store import PostgresProductRepository
from app.db.session import full_transaction
from app.identity.models import Principal
from app.learning.loop_store import _self_report_verdict, _validate_submission_content
from app.learning.ports import Diagnosis, TaskAssessment, TaskSubmission
from app.product.models import PlanBundle, TaskStatus


class PostgresLearningLoopRepository:
    def __init__(
        self,
        *,
        products: PostgresProductRepository,
        evidence: PostgresEvidenceRepository,
        dsn: str | None = None,
    ) -> None:
        self.products = products
        self.evidence = evidence
        self._dsn = dsn

    def install_generated_plan(
        self,
        actor: Principal,
        project_id: str,
        bundle: PlanBundle,
        assessments: tuple[TaskAssessment, ...],
    ) -> PlanBundle:
        from app.learning.loop_store import InMemoryLearningLoopRepository

        self.products.membership.get(actor, project_id)
        InMemoryLearningLoopRepository._validate_assessments(bundle, assessments)
        with full_transaction(
            tenant_id=actor.tenant_id,
            project_id=project_id,
            principal_id=actor.principal_id,
            dsn=self._dsn,
        ) as conn:
            saved = self.products._replace_plan_in_transaction(
                conn, actor, project_id, bundle
            )
            for item in assessments:
                conn.execute(
                    "INSERT INTO task_assessments"
                    " (assessment_id, tenant_id, project_id, task_id, component_id,"
                    " contract_id, mapping_version) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        item.assessment_id, actor.tenant_id, project_id, item.task_id,
                        item.component_id, item.contract_id, item.mapping_version,
                    ),
                )
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
        with full_transaction(
            tenant_id=actor.tenant_id, project_id=project_id,
            principal_id=actor.principal_id, dsn=self._dsn,
        ) as conn:
            row = conn.execute(
                "INSERT INTO diagnoses"
                " (diagnosis_id, tenant_id, project_id, principal_id, answers, summary)"
                " VALUES (%s, %s, %s, %s, %s::jsonb, %s)"
                " RETURNING created_at",
                (
                    diagnosis_id, actor.tenant_id, project_id, actor.principal_id,
                    json.dumps(answers, ensure_ascii=False, sort_keys=True), summary,
                ),
            ).fetchone()
        assert row is not None
        return Diagnosis(
            diagnosis_id, project_id, actor.principal_id, dict(answers), summary, row[0]
        )

    def latest_diagnosis(
        self, actor: Principal, project_id: str
    ) -> Diagnosis | None:
        self.products.membership.get(actor, project_id)
        with full_transaction(
            tenant_id=actor.tenant_id, project_id=project_id,
            principal_id=actor.principal_id, dsn=self._dsn,
        ) as conn:
            row = conn.execute(
                "SELECT diagnosis_id, answers, summary, created_at FROM diagnoses"
                " WHERE project_id = %s AND principal_id = %s"
                " ORDER BY created_at DESC, diagnosis_id DESC LIMIT 1",
                (project_id, actor.principal_id),
            ).fetchone()
        if row is None:
            return None
        return Diagnosis(row[0], project_id, actor.principal_id, row[1], row[2], row[3])

    def submit_self_report(
        self,
        actor: Principal,
        project_id: str,
        task_id: str,
        *,
        submission_id: str,
        content: str,
    ):
        _validate_submission_content(content)
        self.products.membership.get(actor, project_id)
        with full_transaction(
            tenant_id=actor.tenant_id, project_id=project_id,
            principal_id=actor.principal_id, dsn=self._dsn,
        ) as conn:
            task = conn.execute(
                "SELECT status FROM learning_tasks WHERE task_id = %s FOR UPDATE",
                (task_id,),
            ).fetchone()
            if task is None:
                raise deny(ErrorCode.CROSS_TENANT_DENIED, "无权访问该项目")
            if task[0] != TaskStatus.IN_PROGRESS.value:
                raise deny(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    "只有进行中的任务可以提交练习",
                )
            assessment = conn.execute(
                "SELECT component_id, contract_id, mapping_version"
                " FROM task_assessments WHERE task_id = %s",
                (task_id,),
            ).fetchone()
            if assessment is None:
                raise deny(ErrorCode.EVIDENCE_UNMAPPED, "任务没有事前冻结的评估契约")
            row = conn.execute(
                "INSERT INTO task_submissions"
                " (submission_id, tenant_id, project_id, task_id, principal_id, mode, content)"
                " VALUES (%s, %s, %s, %s, %s, 'self_report', %s)"
                " RETURNING created_at",
                (
                    submission_id, actor.tenant_id, project_id, task_id,
                    actor.principal_id, content,
                ),
            ).fetchone()
            assert row is not None
            submission = TaskSubmission(
                submission_id, project_id, task_id, actor.principal_id,
                "self_report", content, row[0],
            )
            event = self.evidence.append_learning_in_transaction(
                conn,
                actor,
                project_id=project_id,
                task_id=task_id,
                contract_id=assessment[1],
                mapping_version=assessment[2],
                verdicts=(_self_report_verdict(assessment[0], submission_id),),
            )
        return submission, event

    def submissions_for_task(
        self, actor: Principal, project_id: str, task_id: str
    ) -> tuple[TaskSubmission, ...]:
        self.products.get_task(actor, project_id, task_id)
        with full_transaction(
            tenant_id=actor.tenant_id, project_id=project_id,
            principal_id=actor.principal_id, dsn=self._dsn,
        ) as conn:
            rows = conn.execute(
                "SELECT submission_id, principal_id, mode, content, created_at"
                " FROM task_submissions WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return tuple(
            TaskSubmission(row[0], project_id, task_id, row[1], row[2], row[3], row[4])
            for row in rows
        )
