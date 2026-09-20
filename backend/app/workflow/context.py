"""node 执行上下文。

把 node handler 能接触到的东西收拢在一处。handler **拿不到** registry、gateway、
ledger 或 dispatcher —— 它只能通过 `ToolInvoker` 调用工具，因此无法绕过策略、
预算与审计。这是把「工具边界」做成结构性约束而不是靠自觉的关键一步。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.audit.sink import AuditSink
from app.core.clock import Clock
from app.knowledge.retrieval import ChunkIndex
from app.learning.evidence import EvidenceLog


@dataclass
class NodeContext:
    """node handler 可见的全部依赖。

    `clock` 在这里的意义不只是方便取时间：**时间属于执行环境，不属于调用参数**。
    handler 若把时间塞进工具参数，参数哈希每次都会变，
    服务端确认记录（绑定参数哈希）就永远无法匹配。
    """

    chunk_index: ChunkIndex
    evidence_log: EvidenceLog
    audit: AuditSink
    clock: Clock
    # 环境信息：**不进入工具参数**，由工具实现从这里取。
    # 参数里混入任何环境值都会让参数哈希不稳定，进而使服务端确认记录永远匹配不上。
    tenant_id: str
    project_id: str
    principal_id: str
    # 供 handler 组织输出用，不参与权限判断
    scratch: dict = field(default_factory=dict)
