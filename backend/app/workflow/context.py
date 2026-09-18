"""node 执行上下文。

把 node handler 能接触到的东西收拢在一处。handler **拿不到** registry、gateway、
ledger 或 dispatcher —— 它只能通过 `ToolInvoker` 调用工具，因此无法绕过策略、
预算与审计。这是把「工具边界」做成结构性约束而不是靠自觉的关键一步。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.audit.sink import AuditSink
from app.knowledge.retrieval import ChunkIndex
from app.learning.evidence import EvidenceLog


@dataclass
class NodeContext:
    """node handler 可见的全部依赖。"""

    chunk_index: ChunkIndex
    evidence_log: EvidenceLog
    audit: AuditSink
    # 供 handler 组织输出用，不参与权限判断
    scratch: dict = field(default_factory=dict)
