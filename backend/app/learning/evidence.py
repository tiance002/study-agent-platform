"""学习证据模型：append-only 事实源。

设计依据：01 号规格 §4/§5/§6、不变量 #14。

三条性质（均有测试）：
1. `EvidenceEvent` 与 `EvidenceCorrection` **只能追加**：本模块不提供任何
   update / delete 接口，修正只能以追加裁决的形式表达；
2. 证据按「证据 × 能力组件」**独立裁决四个维度**，不允许用一个总 verdict 代替；
3. 产品执行证据与学习证据分开建模 —— 产品运行成功不自动等于用户掌握。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum

from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash
from app.core.ids import new_id

PROJECTION_ALGORITHM_VERSION = "projection/v1"


class EvidenceKind(StrEnum):
    """两类证据。混用会导致「跑通代码 = 学会知识」这种错误结论。"""

    LEARNING = "learning"   # 事前声明 contract 的 assessment 产生
    PROJECT = "project"     # 产品运行产生的工程证据


class Validity(StrEnum):
    """筛选层结论：这条证据是否有效。"""

    VALID = "valid"
    INCONCLUSIVE = "inconclusive"
    VOIDED = "voided"
    ATTRIBUTION_PENDING = "attribution_pending"


class ObservationStrength(IntEnum):
    """平台能观测到多少。使用 OBS-1..OBS-5，不得与 A 权限、D 影响度、L 档位混用。"""

    OBS_1 = 1
    OBS_2 = 2
    OBS_3 = 3
    OBS_4 = 4
    OBS_5 = 5


class IndependenceLevel(StrEnum):
    UNKNOWN = "unknown"
    INTRODUCED = "introduced"
    PRACTICED = "practiced"
    DEMONSTRATED = "demonstrated"


_LEVEL_ORDER = {
    IndependenceLevel.UNKNOWN: 0,
    IndependenceLevel.INTRODUCED: 1,
    IndependenceLevel.PRACTICED: 2,
    IndependenceLevel.DEMONSTRATED: 3,
}


def level_value(level: IndependenceLevel) -> int:
    return _LEVEL_ORDER[level]


class Confidence(StrEnum):
    """四档置信度。**首版不输出概率数值**（01 号规格 §1）。"""

    INSUFFICIENT = "insufficient"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Direction(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NONE = "none"


@dataclass(frozen=True)
class ComponentVerdict:
    """单个能力组件上的裁决。四维门槛的后果不同，不能合并成一个总分。"""

    component_id: str
    assessment_validity: Validity
    observation_strength: ObservationStrength
    source_reliability_ok: bool
    independence_level: IndependenceLevel
    direction: Direction
    independence_group: str

    def to_dict(self) -> dict:
        return {
            "component_id": self.component_id,
            "assessment_validity": str(self.assessment_validity),
            "observation_strength": int(self.observation_strength),
            "source_reliability_ok": self.source_reliability_ok,
            "independence_level": str(self.independence_level),
            "direction": str(self.direction),
            "independence_group": self.independence_group,
        }


@dataclass(frozen=True)
class EvidenceEvent:
    """append-only 事实。没有 update / delete，也没有"软删除标记"。"""

    event_id: str
    seq: int
    tenant_id: str
    project_id: str
    kind: EvidenceKind
    task_id: str
    contract_id: str | None
    mapping_version: str | None
    graph_version: str
    occurred_at: datetime
    verdicts: tuple[ComponentVerdict, ...]

    def payload(self) -> dict:
        """参与哈希与重放的规范化载荷。"""
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "kind": str(self.kind),
            "task_id": self.task_id,
            "contract_id": self.contract_id,
            "mapping_version": self.mapping_version,
            "graph_version": self.graph_version,
            "occurred_at": self.occurred_at,
            "verdicts": [v.to_dict() for v in self.verdicts],
        }

    def content_hash(self) -> str:
        return content_hash(self.payload())


@dataclass(frozen=True)
class EvidenceCorrection:
    """追加裁决。它**不修改**原始事件，只覆盖受影响组件的结论。"""

    correction_id: str
    target_event_id: str
    component_id: str
    supersedes: bool
    reason: str
    issued_at: datetime

    def payload(self) -> dict:
        return {
            "correction_id": self.correction_id,
            "target_event_id": self.target_event_id,
            "component_id": self.component_id,
            "supersedes": self.supersedes,
            "reason": self.reason,
            "issued_at": self.issued_at,
        }


class EvidenceLog:
    """只追加的证据日志。

    与数据库约束对应：应用角色对 `EvidenceEvent` / `EvidenceCorrection`
    只有 INSERT 权限（sql-schema 契约要求）。
    """

    def __init__(self) -> None:
        self._events: list[EvidenceEvent] = []
        self._corrections: list[EvidenceCorrection] = []
        self._next_seq = 0
        self._by_id: dict[str, EvidenceEvent] = {}

    # ------------------------------------------------------------------ 追加

    def append(
        self,
        *,
        tenant_id: str,
        project_id: str,
        kind: EvidenceKind,
        task_id: str,
        graph_version: str,
        occurred_at: datetime,
        verdicts: tuple[ComponentVerdict, ...],
        contract_id: str | None = None,
        mapping_version: str | None = None,
    ) -> EvidenceEvent:
        """追加一条证据事件。

        学习证据必须引用已冻结的 assessment contract 与有效 mapping，
        否则不能进入掌握投影（01 号规格 §4）。
        """
        if kind is EvidenceKind.LEARNING and not contract_id:
            raise deny(
                ErrorCode.EVIDENCE_UNMAPPED,
                "学习证据必须引用事前冻结的 assessment contract；"
                "普通练习不能事后追认为 assessment",
                task_id=task_id,
            )
        if kind is EvidenceKind.LEARNING and not mapping_version:
            raise deny(
                ErrorCode.EVIDENCE_UNMAPPED,
                "学习证据必须引用有效的 CompetencyTaskMapping；"
                "无有效 mapping 的证据不能进入掌握投影",
                task_id=task_id,
            )
        if not verdicts:
            raise deny(
                ErrorCode.EVIDENCE_UNMAPPED,
                "证据事件必须至少包含一个组件级裁决",
                task_id=task_id,
            )

        self._next_seq += 1
        event = EvidenceEvent(
            event_id=new_id("ev"),
            seq=self._next_seq,
            tenant_id=tenant_id,
            project_id=project_id,
            kind=kind,
            task_id=task_id,
            contract_id=contract_id,
            mapping_version=mapping_version,
            graph_version=graph_version,
            occurred_at=occurred_at,
            verdicts=verdicts,
        )
        self._events.append(event)
        self._by_id[event.event_id] = event
        return event

    def append_correction(
        self,
        *,
        target_event_id: str,
        component_id: str,
        supersedes: bool,
        reason: str,
        issued_at: datetime,
    ) -> EvidenceCorrection:
        """追加一条裁决。原始事件保持不变 —— 失败事实永远保留。"""
        if target_event_id not in self._by_id:
            raise deny(
                ErrorCode.EVIDENCE_UNMAPPED,
                f"correction 指向不存在的事件：{target_event_id}",
            )
        correction = EvidenceCorrection(
            correction_id=new_id("cor"),
            target_event_id=target_event_id,
            component_id=component_id,
            supersedes=supersedes,
            reason=reason,
            issued_at=issued_at,
        )
        self._corrections.append(correction)
        return correction

    # ------------------------------------------------------------------ 查询

    def events(self) -> tuple[EvidenceEvent, ...]:
        return tuple(self._events)

    def corrections(self) -> tuple[EvidenceCorrection, ...]:
        return tuple(self._corrections)

    def watermark(self) -> int:
        """当前事件水位（最大 seq）。"""
        return self._next_seq

    def event(self, event_id: str) -> EvidenceEvent:
        try:
            return self._by_id[event_id]
        except KeyError as exc:
            raise deny(ErrorCode.EVIDENCE_UNMAPPED, f"事件不存在：{event_id}") from exc
