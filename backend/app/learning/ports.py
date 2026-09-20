"""学习闭环的端口：证据仓储协议 + 内置图谱常量。

第 3 轮 MVP 的范围界定（诚实声明）：

- **能力图谱是内置常量**（`COMPONENTS_V1`）：三个组件的拆分是占位设计，
  第 4 轮接入真实模型后会换成可配置图谱 —— 但 `graph_version` 的机制
  从第一天就是真的：证据与投影都携带它，将来换图谱不需要迁移历史数据。
- **corrections 在 PG 侧暂不落地**：`evidence_events` 表没有对应的
  corrections 表（append-only 修正需要单独的迁移）。PG 适配器如实返回
  空元组 —— 比假装支持却静默丢数据诚实。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.identity.models import Principal
from app.learning.evidence import EvidenceCorrection, EvidenceEvent
from app.product.models import PlanBundle

#: 第 3 轮使用的图谱版本。证据事件与投影都会携带，作为将来的分界线。
GRAPH_VERSION_V1 = "graph/v1"

#: 内置能力组件。模板计划生成把每个任务映射到其中一个。
COMPONENTS_V1 = ("concept", "practice", "reflection")


@dataclass(frozen=True)
class TaskAssessment:
    assessment_id: str
    task_id: str
    component_id: str
    contract_id: str
    mapping_version: str


@dataclass(frozen=True)
class TaskSubmission:
    submission_id: str
    project_id: str
    task_id: str
    principal_id: str
    mode: str
    content: str
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "submission_id": self.submission_id,
            "task_id": self.task_id,
            "mode": self.mode,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class Diagnosis:
    diagnosis_id: str
    project_id: str
    principal_id: str
    answers: dict
    summary: str
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "diagnosis_id": self.diagnosis_id,
            "answers": self.answers,
            "summary": self.summary,
            "created_at": self.created_at.isoformat(),
        }


class EvidenceRepository(Protocol):
    """证据仓储。append-only：协议里没有 update / delete。"""

    def append_learning(
        self,
        actor: Principal,
        *,
        project_id: str,
        task_id: str,
        contract_id: str,
        mapping_version: str,
        verdicts: tuple,
    ) -> EvidenceEvent:
        """追加一条学习证据。

        契约参数必须来自**事前冻结**的 `task_assessments` 行 ——
        由调用方（提交端点）负责取用，仓储不负责验证冻结语义，
        但 `EvidenceLog.append` 会校验非空。
        """
        ...

    def events_for(self, actor: Principal, project_id: str) -> tuple[EvidenceEvent, ...]:
        """按项目回放事件（seq 升序）。投影输入必须先经过这里过滤。"""
        ...

    def corrections_for(
        self, actor: Principal, project_id: str, event_ids: set[str]
    ) -> tuple[EvidenceCorrection, ...]:
        """指向给定事件的修正裁决。PG 适配器返回空（见模块 docstring）。"""
        ...


class LearningLoopRepository(Protocol):
    """需要跨产品/证据表原子完成的学习闭环命令。"""

    def install_generated_plan(
        self,
        actor: Principal,
        project_id: str,
        bundle: PlanBundle,
        assessments: tuple[TaskAssessment, ...],
    ) -> PlanBundle: ...

    def record_diagnosis(
        self,
        actor: Principal,
        project_id: str,
        *,
        diagnosis_id: str,
        answers: dict,
        summary: str,
    ) -> Diagnosis: ...

    def latest_diagnosis(
        self, actor: Principal, project_id: str
    ) -> Diagnosis | None: ...

    def submit_self_report(
        self,
        actor: Principal,
        project_id: str,
        task_id: str,
        *,
        submission_id: str,
        content: str,
    ) -> tuple[TaskSubmission, EvidenceEvent]: ...

    def submissions_for_task(
        self, actor: Principal, project_id: str, task_id: str
    ) -> tuple[TaskSubmission, ...]: ...
