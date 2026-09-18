"""掌握投影：证据事实源 → 可重建读模型。

设计依据：01 号规格 §6、不变量 #14。

四条性质（均有测试）：
1. **顺序无关**：事件以任意顺序给出，投影结果一致；
2. **可复现**：相同 `projection_input_set_hash` 必得相同投影；
3. **禁用时钟**：投影在 `projection_scope()` 内运行，读系统时间直接抛错
   （`freshness` 属于展示层，不进入投影）；
4. **唯一写入者**：本模块是 `MasteryProjection` 的唯一产生处，API 与模型不得直写。

另有一条容易搞错的规则：**产品执行证据不进入掌握投影**。跑通一次代码不等于掌握
（不变量 #14）。只有事前声明 contract 的学习证据才计入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.clock import projection_scope
from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash
from app.learning.evidence import (
    PROJECTION_ALGORITHM_VERSION,
    Confidence,
    Direction,
    EvidenceCorrection,
    EvidenceEvent,
    EvidenceKind,
    EvidenceLog,
    IndependenceLevel,
    Validity,
    level_value,
)


@dataclass(frozen=True)
class ComponentMastery:
    """单个能力组件的掌握状态。拆开独立水平、时效、争议与用户放弃标记。"""

    component_id: str
    independence_level: IndependenceLevel
    confidence: Confidence
    evidence_count: int
    independence_groups: int
    negative_evidence_count: int
    last_valid_evidence_at: datetime | None

    def to_dict(self) -> dict:
        return {
            "component_id": self.component_id,
            "independence_level": str(self.independence_level),
            "confidence": str(self.confidence),
            "evidence_count": self.evidence_count,
            "independence_groups": self.independence_groups,
            "negative_evidence_count": self.negative_evidence_count,
            "last_valid_evidence_at": (
                self.last_valid_evidence_at.isoformat() if self.last_valid_evidence_at else None
            ),
        }


@dataclass(frozen=True)
class MasteryProjection:
    """可重建读模型。不是事实源。"""

    project_id: str
    graph_version: str
    projection_algorithm_version: str
    input_set_hash: str
    event_seq_watermark: int
    components: tuple[ComponentMastery, ...]

    def component(self, component_id: str) -> ComponentMastery | None:
        for item in self.components:
            if item.component_id == component_id:
                return item
        return None

    def level_of(self, component_id: str) -> IndependenceLevel:
        item = self.component(component_id)
        return item.independence_level if item else IndependenceLevel.UNKNOWN

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "graph_version": self.graph_version,
            "projection_algorithm_version": self.projection_algorithm_version,
            "input_set_hash": self.input_set_hash,
            "event_seq_watermark": self.event_seq_watermark,
            "components": [c.to_dict() for c in self.components],
        }


def _confidence_for(groups: int) -> Confidence:
    """由独立组数量决定四档置信度。**不输出概率数值**（01 号规格 §1）。"""
    if groups <= 0:
        return Confidence.INSUFFICIENT
    if groups == 1:
        return Confidence.LOW
    if groups == 2:
        return Confidence.MEDIUM
    return Confidence.HIGH


class Projector:
    """掌握投影的唯一写入者。"""

    def project(self, log: EvidenceLog, *, graph_version: str) -> MasteryProjection:
        return self.project_from(
            events=log.events(),
            corrections=log.corrections(),
            graph_version=graph_version,
        )

    def project_from(
        self,
        *,
        events: tuple[EvidenceEvent, ...] | list[EvidenceEvent],
        corrections: tuple[EvidenceCorrection, ...] | list[EvidenceCorrection],
        graph_version: str,
    ) -> MasteryProjection:
        """全量规范回放。投影过程全程在 projection_scope 内，读时钟即抛错。"""
        with projection_scope():
            return self._project(events, corrections, graph_version)

    # ------------------------------------------------------------------ 核心

    def _project(self, events, corrections, graph_version) -> MasteryProjection:
        # 1) 项目隔离：跨项目事件混入即拒绝，而不是"各自投影"。
        projects = {e.project_id for e in events}
        if len(projects) > 1:
            raise deny(
                ErrorCode.CROSS_PROJECT_DENIED,
                f"投影输入混入了多个项目：{sorted(projects)}",
            )
        project_id = next(iter(projects), "")

        # 2) 固定顺序：先按 seq 再按 event_id，使到达顺序不影响结果。
        ordered_events = sorted(events, key=lambda e: (e.seq, e.event_id))
        ordered_corrections = sorted(corrections, key=lambda c: (c.issued_at, c.correction_id))

        superseded = {
            (c.target_event_id, c.component_id)
            for c in ordered_corrections
            if c.supersedes
        }

        # 3) 只让学习证据参与掌握投影；产品执行证据一律忽略。
        considered = [e for e in ordered_events if e.kind is EvidenceKind.LEARNING]

        buckets: dict[str, dict] = {}
        for event in considered:
            for verdict in event.verdicts:
                if (event.event_id, verdict.component_id) in superseded:
                    continue
                # 筛选层：无效裁决不进入投影（01 号规格 §7）。
                if verdict.assessment_validity is not Validity.VALID:
                    continue
                bucket = buckets.setdefault(
                    verdict.component_id,
                    {
                        "count": 0,
                        "negative": 0,
                        "groups": set(),
                        "best_level": IndependenceLevel.UNKNOWN,
                        "last_at": None,
                    },
                )
                bucket["count"] += 1
                bucket["groups"].add(verdict.independence_group)
                if verdict.direction is Direction.NEGATIVE:
                    bucket["negative"] += 1
                    # 负向证据保留事实，但不提升独立水平。
                    continue
                if verdict.direction is Direction.POSITIVE:
                    if level_value(verdict.independence_level) > level_value(bucket["best_level"]):
                        bucket["best_level"] = verdict.independence_level
                if bucket["last_at"] is None or event.occurred_at > bucket["last_at"]:
                    bucket["last_at"] = event.occurred_at

        components = tuple(
            ComponentMastery(
                component_id=component_id,
                independence_level=data["best_level"],
                confidence=_confidence_for(len(data["groups"])),
                evidence_count=data["count"],
                independence_groups=len(data["groups"]),
                negative_evidence_count=data["negative"],
                last_valid_evidence_at=data["last_at"],
            )
            for component_id, data in sorted(buckets.items())
        )

        return MasteryProjection(
            project_id=project_id,
            graph_version=graph_version,
            projection_algorithm_version=PROJECTION_ALGORITHM_VERSION,
            input_set_hash=self.input_set_hash(ordered_events, ordered_corrections),
            event_seq_watermark=max((e.seq for e in ordered_events), default=0),
            components=components,
        )

    @staticmethod
    def input_set_hash(events, corrections) -> str:
        """投影输入集合的规范化哈希。

        与顺序无关：排序后再哈希。投影若使用了未进入该哈希的配置，
        重放就会出现"同样输入、不同结果"，这是被明确禁止的。
        """
        ordered_events = sorted(events, key=lambda e: (e.seq, e.event_id))
        ordered_corrections = sorted(corrections, key=lambda c: (c.issued_at, c.correction_id))
        return content_hash(
            {
                "projection_algorithm_version": PROJECTION_ALGORITHM_VERSION,
                "events": [e.payload() for e in ordered_events],
                "corrections": [c.payload() for c in ordered_corrections],
            }
        )
