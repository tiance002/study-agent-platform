"""教学运行的执行服务：认领后的完整流水线。

## 执行顺序（每一步都对应一个"不能省"的理由）

```
认领（worker 已做）
→ 组上下文（快照冻结）
→ mark_dispatched（派发存证 + 预算 held→in_flight）
→ provider.generate（网络调用不持有任何事务/行锁）
→ record_result（结果存证 —— 此后任何失败都从存证重放，绝不重调模型）
→ validate_citations（引用核验）
→ finish_run（消息 + 结算 + 事件 + 终态，同一事务）
```

## 结果分类的处理矩阵（与计划冻结的状态机一一对应）

| provider 结果 | 处理 | 预算 |
|---|---|---|
| `COMPLETED` | 存证 → 校验 → 落定 | 有 usage 结算；缺/越界 → 敞口保留 |
| `REFUSED` / `MALFORMED` / `TRUNCATED` | 调用已发生 → 按失败落定 | 有 usage 结算；缺 → 对账 |
| `DISPATCH_FAILED` | 可证明未送达 → 失败 | **释放**（没花钱） |
| `TIMEOUT` / 无法分类的异常 | 结果未知 → `reconciliation_required` | 敞口保留，禁止自动重试 |

## usage 的上界判定

provider 报告的用量**越过承诺上限**时按异常保守记账：不采纳（费用敞口
保留、进对账），也不静默截断消费值 —— "截断到上界"会伪造出一条
不存在的账目，而敞口是诚实的"我们不知道花了多少"。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, PlatformError
from app.identity.models import Principal
from app.knowledge.store import KnowledgeRepository
from app.product.ports import ProductRepository
from app.teaching.context import RetrievalSnapshot, TeachingContext, build_context
from app.teaching.models import (
    MaterialSnippet,
    ProviderRequest,
    ProviderResult,
    ProviderStatus,
    RawCitation,
    TokenUsage,
)
from app.teaching.ports import TeachingProvider, TeachingRunRepository
from app.teaching.runs import RunClaim
from app.teaching.validation import validate_citations

#: provider 调用的默认截止时间（秒）。单一 provider 单次调用，
#: P99 远小于 60 秒；超时即转对账，不自动重试。
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 60

#: 执行结果的类别。worker 的返回值是它加上 "idle" / "stale"。
Outcome = Literal["succeeded", "failed", "reconciliation_required"]

#: 可以归为"确定性失败"的错误码集合：写终态是安全的，
#: 因为同一个输入跑一百次也是同一个结果。
_DETERMINISTIC_CODES = frozenset({ErrorCode.PARAMS_INVALID})


@dataclass
class TeachingService:
    """认领后的执行逻辑。依赖全部来自装配层，本类不读环境。"""

    teaching: TeachingRunRepository
    knowledge: KnowledgeRepository
    products: ProductRepository
    provider: TeachingProvider | None
    model_id: str
    prompt_version: str
    max_input_tokens: int
    max_output_tokens: int
    clock: Clock = None  # type: ignore[assignment]
    provider_timeout_seconds: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.clock is None:  # pragma: no cover - 装配层保证
            self.clock = SystemClock()

    # ------------------------------------------------------------ 执行

    def execute(self, claim: RunClaim) -> Outcome:
        """执行一次认领。返回结果类别（测试与 worker 日志用）。"""
        run = claim.run
        if self.provider is None:
            # 功能显式关闭：确定性失败（配置问题不因重试而改变）。
            self.teaching.fail_run(
                claim,
                error_code="TEACHING_PROVIDER_DISABLED",
                safe_detail="教学功能未启用；请联系管理员配置 provider",
                dispatch_happened=False,
                usage=None,
            )
            return "failed"

        actor = Principal(principal_id=run.principal_id, tenant_id=run.tenant_id)
        history = self._history(actor, run)
        context = build_context(
            run, actor, run.project_id, knowledge=self.knowledge, history=history
        )
        est_input = self._estimate_input_tokens(context)
        if est_input > self.max_input_tokens:
            self.teaching.fail_run(
                claim,
                error_code="INPUT_BUDGET_EXCEEDED",
                safe_detail="上下文超过输入 token 上限；请精简资料或历史后重试",
                dispatch_happened=False,
                usage=None,
            )
            return "failed"

        attempt_id = f"att_{uuid.uuid4().hex[:12]}"
        self.teaching.mark_dispatched(
            claim,
            attempt_id=attempt_id,
            estimated_input_tokens=est_input,
            estimated_output_tokens=self.max_output_tokens,
        )
        deadline = self.clock.now() + timedelta(seconds=self.provider_timeout_seconds)
        request = ProviderRequest(
            attempt_id=attempt_id,
            model=self.model_id,
            prompt_version=self.prompt_version,
            messages=context.messages,
            artifacts=context.snapshot.items,
            max_output_tokens=self.max_output_tokens,
            deadline=deadline,
        )
        try:
            result = self.provider.generate(request)
        except Exception:
            # 连结果分类都给不出：结果与费用都未知，保守转对账。
            # 绝不能在这里写终态 —— 那会掩盖"provider 可能已经在算钱"。
            self.teaching.require_reconciliation(
                claim,
                error_code="PROVIDER_ERROR",
                safe_detail="provider 调用异常；结果与费用未知，待对账",
            )
            return "reconciliation_required"

        if result.status is ProviderStatus.DISPATCH_FAILED:
            self.teaching.fail_run(
                claim,
                error_code="PROVIDER_DISPATCH_FAILED",
                safe_detail="provider 未收到请求（可证明未派发）；本次未产生费用",
                dispatch_happened=False,
                usage=None,
            )
            return "failed"

        if result.status is ProviderStatus.TIMEOUT:
            self.teaching.require_reconciliation(
                claim,
                error_code="PROVIDER_TIMEOUT",
                safe_detail="截止时间内未收到响应；结果与费用未知，待对账",
            )
            return "reconciliation_required"

        if result.status is ProviderStatus.COMPLETED:
            payload = self._serialize_result(result, context, payload_attempt_id=attempt_id)
            self.teaching.record_result(
                claim,
                attempt_id=attempt_id,
                provider_request_id=result.provider_request_id,
                payload=payload,
            )
            validation = validate_citations(
                actor,
                run.project_id,
                result.citations,
                snapshot=context.snapshot,
                knowledge=self.knowledge,
            )
            self.teaching.finish_run(
                claim,
                attempt_id=attempt_id,
                answer_message_id=f"msg_{uuid.uuid4().hex[:12]}",
                answer_text=result.answer_text,
                grounding=validation.grounding,
                usage=self._settled_usage(result.usage),
            )
            return "succeeded"

        # REFUSED / MALFORMED / TRUNCATED：调用已发生，按失败落定。
        # 用量缺失时 fail_run 自身会拒绝并要求对账（费用敞口不能抹掉）。
        self.teaching.fail_run(
            claim,
            error_code=f"PROVIDER_{result.status.value.upper()}",
            safe_detail=result.detail or "provider 返回了不可用的结果",
            dispatch_happened=True,
            usage=self._settled_usage(result.usage),
        )
        return "failed"

    def replay(self, claim: RunClaim) -> Outcome:
        """从已持久化的结果存证落库（final commit 失败后的恢复路径）。

        **绝不重新调用 provider** —— 存证里的答案、引用、快照就是
        当时的事实；重新生成等于花第二次钱换一个不同的答案。
        """
        payload = self.teaching.find_stored_result(claim)
        if payload is None:
            raise PlatformError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                "该运行没有已存证的结果，不能走重放路径",
            )
        run = claim.run
        actor = Principal(principal_id=run.principal_id, tenant_id=run.tenant_id)
        snapshot = self._deserialize_snapshot(payload["snapshot"])
        citations = tuple(
            RawCitation(
                source_id=item["source_id"],
                document_id=item["document_id"],
                span_start=item["span_start"],
                span_end=item["span_end"],
                content_hash=item["content_hash"],
            )
            for item in payload["citations"]
        )
        validation = validate_citations(
            actor, run.project_id, citations, snapshot=snapshot, knowledge=self.knowledge
        )
        usage = self._settled_usage(self._deserialize_usage(payload.get("usage")))
        self.teaching.finish_run(
            claim,
            attempt_id=payload["attempt_id"],
            answer_message_id=f"msg_{uuid.uuid4().hex[:12]}",
            answer_text=payload["answer_markdown"],
            grounding=validation.grounding,
            usage=usage,
        )
        return "succeeded"

    # ------------------------------------------------------------ 内部

    def _history(self, actor: Principal, run) -> tuple:
        """会话历史（不含本次问题：它在 prompt 的 QUESTION 区）。"""
        messages = self.products.list_messages(
            actor, run.project_id, run.conversation_id
        )
        return tuple(m for m in messages if m.message_id != run.user_message_id)

    def _estimate_input_tokens(self, context: TeachingContext) -> int:
        """输入 token 估计上界：逐字符计数。

        没有验证过的 tokenizer 之前，"1 字符 ≈ 1 token"对 CJK 是
        保守上界（中文常见分词约 0.6–1 token/字符）—— 估计上界宁可
        高估：低估会派发超出预算的请求，高估只是拒绝得早一点。
        """
        return sum(len(message.content) for message in context.messages)

    def _settled_usage(self, usage: TokenUsage | None) -> TokenUsage | None:
        """用量是否可采纳。缺失或越界一律返回 None（敞口保留，进对账）。"""
        if usage is None:
            return None
        if (
            usage.input_tokens > self.max_input_tokens
            or usage.output_tokens > self.max_output_tokens
        ):
            # 越界的"权威用量"本身不可信。不静默截断消费值 ——
            # 截断会伪造一条不存在的账目；敞口是诚实的未知。
            return None
        return usage

    def _serialize_result(
        self, result: ProviderResult, context: TeachingContext, *, payload_attempt_id: str
    ) -> dict:
        """结果存证。**快照一起存**：重放路径校验引用要有同一份基准，
        否则"重启后重新检索"会让校验对着另一批片段（移动的球门）。"""
        return {
            "provider_status": str(result.status),
            "attempt_id": payload_attempt_id,
            "answer_markdown": result.answer_text,
            "citations": [
                {
                    "source_id": c.source_id,
                    "document_id": c.document_id,
                    "span_start": c.span_start,
                    "span_end": c.span_end,
                    "content_hash": c.content_hash,
                }
                for c in result.citations
            ],
            "usage": (
                None
                if result.usage is None
                else {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                }
            ),
            "snapshot": [
                {
                    "source_id": item.source_id,
                    "document_id": item.document_id,
                    "chunk_id": item.chunk_id,
                    "span_start": item.span_start,
                    "span_end": item.span_end,
                    "content": item.content,
                    "content_hash": item.content_hash,
                    "parser_version": item.parser_version,
                    "display_policy": item.display_policy,
                }
                for item in context.snapshot.items
            ],
        }

    def _deserialize_snapshot(self, items: list) -> RetrievalSnapshot:
        from app.teaching.context import RetrievalSnapshot

        return RetrievalSnapshot(
            ranking_version="keyword/v1",
            items=tuple(
                MaterialSnippet(
                    source_id=item["source_id"],
                    document_id=item["document_id"],
                    chunk_id=item["chunk_id"],
                    span_start=item["span_start"],
                    span_end=item["span_end"],
                    content=item["content"],
                    content_hash=item["content_hash"],
                    parser_version=item["parser_version"],
                    display_policy=item.get("display_policy", "full"),
                )
                for item in items
            ),
        )

    def _deserialize_usage(self, usage: dict | None) -> TokenUsage | None:
        if usage is None:
            return None
        return TokenUsage(
            input_tokens=int(usage["input_tokens"]),
            output_tokens=int(usage["output_tokens"]),
        )

