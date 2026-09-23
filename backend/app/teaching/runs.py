"""教学运行的领域模型（区别于 provider 侧的 `teaching.models`）。

状态机在**迁移 0010 的 CHECK** 与本模块的 `transition` 双侧声明 ——
与摄取任务同样的取舍：卡死的运行与"成功但没有答案消息"不可修复，
数据库必须能拒收。Python 侧是唯一的状态迁移判定出口
（`RUN_TRANSITIONS`），两个适配器共用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.core.contracts import (
    require_aware,
    require_non_negative,
    require_positive,
    require_text,
)
from app.teaching.routing import RoutingDecision


class RunStatus(StrEnum):
    """运行状态（与 0010 的 CHECK 逐字对应）。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: 已派发但结果或费用未知。**不是失败**：费用敞口保留，禁止自动重试。
    RECONCILIATION_REQUIRED = "reconciliation_required"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.FAILED)


#: 状态迁移的唯一判定出口。queued 可被接管（租约到期后）；
#: 派发后的 running 不可回到 queued（provider 可能已经在算钱）。
RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.RUNNING: {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.RECONCILIATION_REQUIRED},
    RunStatus.SUCCEEDED: set(),
    RunStatus.FAILED: set(),
    RunStatus.RECONCILIATION_REQUIRED: set(),
}


class Grounding(StrEnum):
    """回答的依据状态。**由校验层判定**，模型无权宣称。"""

    #: 至少一条引用通过了"指向本次已授权快照的那一版原文"的核验。
    SOURCED = "sourced"
    #: 没有任何引用通过核验 —— 回答是一般性说明（或资料不足）。
    INFERENCE_ONLY = "inference_only"


@dataclass(frozen=True)
class TeachingRun:
    """一次教学运行的持久化事实。"""

    run_id: str
    tenant_id: str
    project_id: str
    conversation_id: str
    user_message_id: str
    #: 提问者。worker 落定答案消息时按它做归属判定（内存适配器）；
    #: 也是"这条回答属于谁"的 provenance 事实。
    principal_id: str
    answer_message_id: str | None
    question: str
    status: RunStatus
    attempt_count: int
    model_id: str
    prompt_version: str
    ranking_version: str
    grounding: Grounding | None
    error_code: str
    error_detail: str
    created_at: datetime
    updated_at: datetime
    #: 创建运行时冻结的 token 上限。worker 不能用部署热更新后的值替换它。
    max_input_tokens: int = 8000
    max_output_tokens: int = 2000
    #: 最终回答中通过机械核验的引用；它们是用户可回读的证据坐标。
    citations: tuple[dict, ...] = ()
    #: 被拒绝的 provider 引用及稳定原因，不把原始 provider payload 暴露给用户。
    citation_rejections: tuple[dict, ...] = ()
    routing_decision: RoutingDecision | None = None

    def __post_init__(self) -> None:
        require_text(self.run_id, "run_id")
        require_text(self.tenant_id, "tenant_id")
        require_text(self.project_id, "project_id")
        require_text(self.conversation_id, "conversation_id")
        require_text(self.user_message_id, "user_message_id")
        require_text(self.principal_id, "principal_id")
        require_text(self.question, "question")
        if not isinstance(self.status, RunStatus):
            raise ValueError(f"status 必须是 RunStatus，收到 {self.status!r}")
        require_non_negative(self.attempt_count, "attempt_count")
        require_text(self.model_id, "model_id")
        require_text(self.prompt_version, "prompt_version")
        require_text(self.ranking_version, "ranking_version")
        if self.grounding is not None and not isinstance(self.grounding, Grounding):
            raise ValueError(f"grounding 必须是 Grounding 或 None，收到 {self.grounding!r}")
        require_text(self.error_code, "error_code", allow_empty=True)
        require_text(self.error_detail, "error_detail", allow_empty=True)
        require_positive(self.max_input_tokens, "max_input_tokens")
        require_positive(self.max_output_tokens, "max_output_tokens")
        if not isinstance(self.citations, tuple) or not all(
            isinstance(item, dict) for item in self.citations
        ):
            raise ValueError("citations 必须是 dict 元组")
        if not isinstance(self.citation_rejections, tuple) or not all(
            isinstance(item, dict) for item in self.citation_rejections
        ):
            raise ValueError("citation_rejections 必须是 dict 元组")
        if self.routing_decision is not None and not isinstance(self.routing_decision, RoutingDecision):
            raise ValueError("routing_decision 必须是 RoutingDecision 或 None")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        # 状态 ↔ 附属事实的一致性（与 0010 的 CHECK 同一条规则）。
        if self.status is RunStatus.SUCCEEDED:
            if self.answer_message_id is None or self.grounding is None:
                raise ValueError("succeeded 的运行必须有答案消息与 grounding")
        elif self.answer_message_id is not None or self.grounding is not None:
            raise ValueError(f"只有 succeeded 的运行携带答案消息，当前状态是 {self.status}")
        if self.status in (RunStatus.FAILED, RunStatus.RECONCILIATION_REQUIRED):
            if not self.error_code:
                raise ValueError(f"{self.status} 的运行必须有错误码")

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "user_message_id": self.user_message_id,
            "answer_message_id": self.answer_message_id,
            "status": str(self.status),
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "ranking_version": self.ranking_version,
            "grounding": str(self.grounding) if self.grounding else "",
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "citations": [dict(item) for item in self.citations],
            "citation_rejections": [dict(item) for item in self.citation_rejections],
            "routing_decision": (self.routing_decision.to_dict() if self.routing_decision else None),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class RunClaim:
    """一次认领的围栏凭证（与摄取的 claim 同构）。

    `claim_token` 在每次认领时新生成；落定/派发/记录结果都必须携带它，
    条件更新在同一事务里比对 —— 失去租约的旧持有者无法改写新持有者的运行。
    """

    run: TeachingRun
    claim_token: str
    worker_id: str

    def __post_init__(self) -> None:
        require_text(self.claim_token, "claim_token")
        require_text(self.worker_id, "worker_id")
        if self.run.status is not RunStatus.RUNNING:
            raise ValueError("认领凭证只对应 running 状态的运行")


@dataclass(frozen=True)
class TeachingEvent:
    """一条持久化的运行事件（SSE 回放的来源）。

    `seq` 由存储层在**写事件的同一事务**里分配（每运行内单调递增），
    HTTP 层的 `Last-Event-ID` 直接对应它。
    """

    run_id: str
    seq: int
    event_type: str
    payload: dict
    created_at: datetime

    def __post_init__(self) -> None:
        require_text(self.run_id, "run_id")
        require_text(self.event_type, "event_type")
        if not isinstance(self.payload, dict):
            raise ValueError("payload 必须是 dict")
        require_aware(self.created_at, "created_at")

    def sse_lines(self) -> str:
        """渲染成一条 SSE 帧（不含尾部空行 —— 由流包装器补）。"""
        import json

        return (
            f"id: {self.seq}\n"
            f"event: {self.event_type}\n"
            f"data: {json.dumps(self.payload, ensure_ascii=False, sort_keys=True)}"
        )
