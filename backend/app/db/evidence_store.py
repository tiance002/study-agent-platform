"""证据仓储的 PostgreSQL 实现。

## 两个关键决定

1. **payload jsonb 是重建的唯一权威**：`evidence_events.payload` 存的是
   `EvidenceEvent.payload()` 的规范化 JSON —— 裁决、契约引用全在里面。
   回放时从 payload 重建事件对象，列只用来过滤与排序（seq/tenant/project）。
   两处真相源（列 vs payload）里，**结构化的那个说了算**。

2. **`INSERT ... RETURNING seq` 在这里是安全的**（铁律 40 的例外）：
   `evidence_events` 的 RLS 谓词是「上下文相等」型（tenant + project），
   新行的 tenant/project 与事务上下文一致 ⇒ 新行必然通过 USING。
   铁律 40 栽的是成员感知策略（新行对创建者不可见）—— 本表没有那种谓词。
   seq 由 identity 列分配，应用层不生成（全局单调，跨项目不回退）。

corrections：`evidence_corrections` 表尚不存在（需要单独迁移），
本适配器如实返回空元组而不是静默丢弃 —— 见 ports.py 的说明。
"""

from __future__ import annotations

import json
from datetime import datetime

from psycopg import errors as pg_errors

from app.core.errors import ErrorCode, deny
from app.db.session import tenant_transaction
from app.identity.models import Principal
from app.learning.evidence import (
    ComponentVerdict,
    Direction,
    EvidenceCorrection,
    EvidenceEvent,
    EvidenceKind,
    IndependenceLevel,
    ObservationStrength,
    Validity,
)
from app.learning.ports import GRAPH_VERSION_V1


def _verdict_from_payload(data: dict) -> ComponentVerdict:
    return ComponentVerdict(
        component_id=data["component_id"],
        assessment_validity=Validity(data["assessment_validity"]),
        observation_strength=ObservationStrength(data["observation_strength"]),
        source_reliability_ok=data["source_reliability_ok"],
        independence_level=IndependenceLevel(data["independence_level"]),
        direction=Direction(data["direction"]),
        independence_group=data["independence_group"],
    )


def _event_from_row(seq: int, payload: dict) -> EvidenceEvent:
    """从 payload（jsonb 反序列化出的 dict）重建事件对象。"""
    return EvidenceEvent(
        event_id=payload["event_id"],
        seq=seq,  # 列是权威：identity 分配的全局单调序号。
        tenant_id=payload["tenant_id"],
        project_id=payload["project_id"],
        kind=EvidenceKind(payload["kind"]),
        task_id=payload["task_id"],
        contract_id=payload["contract_id"],
        mapping_version=payload["mapping_version"],
        graph_version=payload["graph_version"],
        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
        verdicts=tuple(_verdict_from_payload(v) for v in payload["verdicts"]),
    )


class PostgresEvidenceRepository:
    """PG 证据仓储。append-only 由表权限（SELECT/INSERT）兜底。"""

    def __init__(self, clock=None, dsn: str | None = None) -> None:
        from app.core.clock import SystemClock

        self._clock = clock if clock is not None else SystemClock()
        self._dsn = dsn

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
        from app.core.hashing import canonical_json

        # 先在应用层构造完整事件（走 EvidenceLog 的全部校验），
        # 再原样落库 —— 校验逻辑不出现第二份。
        from app.learning.memory_store import InMemoryEvidenceRepository

        staging = InMemoryEvidenceRepository(clock=self._clock)
        event = staging.append_learning(
            actor,
            project_id=project_id,
            task_id=task_id,
            contract_id=contract_id,
            mapping_version=mapping_version,
            verdicts=verdicts,
        )
        try:
            with tenant_transaction(
                tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
            ) as conn:
                row = conn.execute(
                    "INSERT INTO evidence_events"
                    " (event_id, tenant_id, project_id, kind, task_id,"
                    "  contract_id, mapping_version, graph_version, occurred_at,"
                    "  payload)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
                    " RETURNING seq",
                    (
                        event.event_id,
                        actor.tenant_id,
                        project_id,
                        event.kind.value,
                        task_id,
                        contract_id,
                        mapping_version,
                        GRAPH_VERSION_V1,
                        event.occurred_at,
                        canonical_json(event.payload()),
                    ),
                ).fetchone()
        except pg_errors.UniqueViolation as exc:
            raise deny(
                ErrorCode.EVIDENCE_IMMUTABLE,
                "证据事件 ID 冲突；事件是不可变的，请勿重放同一 event_id",
            ) from exc
        assert row is not None, "INSERT 成功却拿不到 RETURNING 行"
        # 用库分配的 seq 替换暂存对象里的内存序号 —— 全局单调以库为准。
        return _event_from_row(int(row[0]), json.loads(canonical_json(event.payload())))

    def events_for(self, actor: Principal, project_id: str) -> tuple[EvidenceEvent, ...]:
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "SELECT seq, payload FROM evidence_events ORDER BY seq"
            ).fetchall()
        return tuple(_event_from_row(int(seq), payload) for seq, payload in rows)

    def corrections_for(
        self, actor: Principal, project_id: str, event_ids: set[str]
    ) -> tuple[EvidenceCorrection, ...]:
        # corrections 表尚不存在；如实返回空（ports.py 有说明）。
        return ()
