"""证据仓储的内存实现：组合（而非继承）既有的 `EvidenceLog`。

`EvidenceLog` 的 append-only 校验（学习证据必须带契约、必须有裁决）已经
有测试守着 —— 内存适配器直接复用它，校验逻辑不出现第二份。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.clock import Clock, SystemClock
from app.identity.models import Principal
from app.learning.evidence import (
    EvidenceCorrection,
    EvidenceEvent,
    EvidenceKind,
    EvidenceLog,
)
from app.learning.ports import GRAPH_VERSION_V1


@dataclass
class InMemoryEvidenceRepository:
    """内存证据仓储。开发适配器；PG 版与之跑同一套契约测试。"""

    clock: Clock = field(default_factory=SystemClock)
    _log: EvidenceLog = field(default_factory=EvidenceLog)

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
        return self._log.append(
            tenant_id=actor.tenant_id,
            project_id=project_id,
            kind=EvidenceKind.LEARNING,
            task_id=task_id,
            graph_version=GRAPH_VERSION_V1,
            occurred_at=self.clock.now(),
            verdicts=verdicts,
            contract_id=contract_id,
            mapping_version=mapping_version,
        )

    def events_for(self, actor: Principal, project_id: str) -> tuple[EvidenceEvent, ...]:
        return self._log.events_scoped(tenant_id=actor.tenant_id, project_id=project_id)

    def corrections_for(
        self, actor: Principal, project_id: str, event_ids: set[str]
    ) -> tuple[EvidenceCorrection, ...]:
        return self._log.corrections_scoped(event_ids=event_ids)
