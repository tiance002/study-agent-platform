"""教学运行的领域模型与 provider 请求/结果契约。

## 两条不可动摇的边界

1. **ProviderResult 是 provider 侧的事实，客户端（以及我们的 HTTP 层）
   不允许构造它。** 答案文本、引用、token 用量都只能从 provider 适配器
   流向校验层；校验层拒绝掉的引用不会因为"再试一次"而复活。
2. **`ProviderRequest` 是不可变快照。** attempt_id、模型、prompt 版本、
   资料片段在派发后绝不改变 —— 重试复用同一个 attempt 标识，
   复放必须得到同一个请求。frozen dataclass 在类型层保证这一点。

## 为什么 TIMEOUT 与 DISPATCH_FAILED 是两个状态

两者的**费用语义相反**：

- `DISPATCH_FAILED`：请求可证明**没有送达** provider（连接被拒、DNS 失败）。
  没有调用就没有费用 —— 预算可以安全释放，运行可以安全重试。
- `TIMEOUT`：请求**已经发出**，截止时间内没等到响应。我们**不知道**
  provider 是否计算并计费 —— 结果未知，费用敞口必须保留，
  运行转 `reconciliation_required`，绝不能自动再调一次
  （那才是无声重复扣费的来源）。

把两者合并成一个"失败"状态，等于把"没花钱"和"可能花了钱"当成一件事处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.core.contracts import (
    require_aware,
    require_non_negative,
    require_positive,
    require_text,
)


class ProviderStatus(StrEnum):
    """provider 调用的结果分类。闭集 —— 未知结果必须显式建模，不能归并。"""

    #: 正常返回完整答案（答案仍需经过引用校验，不代表可提交）。
    COMPLETED = "completed"
    #: provider 明确拒绝（安全策略/内容政策）。调用已发生，但无答案产出。
    REFUSED = "refused"
    #: 请求**可证明未送达**（连接被拒等）。没有调用就没有费用。
    DISPATCH_FAILED = "dispatch_failed"
    #: 请求已发出、截止时间内未等到响应。**结果未知** —— 费用敞口保留。
    TIMEOUT = "timeout"
    #: 响应到达但不是合法的答案契约（畸形 JSON、缺必需键）。
    MALFORMED = "malformed"
    #: 响应到达但被截断（finish_reason / 长度特征表明不完整）。
    TRUNCATED = "truncated"


#: 「已派发但结果未知」的状态集合。落到这些状态时：
#: 费用敞口保留、禁止自动重新 generate、运行转 reconciliation_required。
UNKNOWN_OUTCOMES = frozenset({ProviderStatus.TIMEOUT})

#: 「provider 调用确定已发生」的状态（无论答案是否可用）。
#: 这些状态下费用必须按已发生的调用结算，不能释放成"没花钱"。
DISPATCHED_OUTCOMES = frozenset(
    {
        ProviderStatus.COMPLETED,
        ProviderStatus.REFUSED,
        ProviderStatus.TIMEOUT,
        ProviderStatus.MALFORMED,
        ProviderStatus.TRUNCATED,
    }
)


class PromptRole(StrEnum):
    """provider 消息角色。教学请求里只允许这三种，客户端无权注入其它 role。"""

    SYSTEM = "system"
    USER = "user"


@dataclass(frozen=True)
class PromptMessage:
    """发给 provider 的一条消息。`role` 是闭集，内容原样（不做模板渲染）。"""

    role: PromptRole
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, PromptRole):
            raise ValueError(f"role 必须是 PromptRole，收到 {self.role!r}")
        require_text(self.content, "content")


@dataclass(frozen=True)
class MaterialSnippet:
    """进入 provider 上下文的一段已授权资料。

    字段与 `ArtifactRef` 的不可变标识一一对应：派发后凭这组字段可以
    把**那一版**原文取回来核对。`content` 是该片段的精确原文
    （切片相等在写入侧已核验过，见 `knowledge.models.assert_chunks_match_document`）。

    ⚠️ 这是**带 taint 的外部数据**：它会进 provider 上下文，但里面的
    "忽略规则""调用工具"等文本不改变任何权限 —— 权限只由服务端
    system 指令与请求形状决定（`prompt.py` 固定生成，不由资料内容影响）。
    """

    source_id: str
    document_id: str
    chunk_id: str
    span_start: int
    span_end: int
    content: str
    content_hash: str
    parser_version: str
    display_policy: str = "full"

    def __post_init__(self) -> None:
        require_text(self.source_id, "source_id")
        require_text(self.document_id, "document_id")
        require_text(self.chunk_id, "chunk_id")
        require_non_negative(self.span_start, "span_start")
        if self.span_end < self.span_start:
            raise ValueError("span_end 必须不小于 span_start")
        require_text(self.content, "content", allow_empty=True)
        require_text(self.content_hash, "content_hash")
        require_text(self.parser_version, "parser_version")
        require_text(self.display_policy, "display_policy")

    def as_citation_dict(self) -> dict:
        """转成引用形状（进入 prompt 的资料标注 / 校验层的比对基准）。"""
        return {
            "source_id": self.source_id,
            "document_id": self.document_id,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ProviderRequest:
    """发给 provider 的一次请求（不可变快照）。

    `attempt_id` 是**durable attempt 标识**：它在派发前就已持久化，
    重试只允许复用同一个 attempt —— 这让"供应商侧的幂等/去重"有据可依，
    也让我们能在账本上把调用与 attempt 一一对应。
    """

    attempt_id: str
    model: str
    prompt_version: str
    messages: tuple[PromptMessage, ...]
    artifacts: tuple[MaterialSnippet, ...]
    max_output_tokens: int
    deadline: datetime

    def __post_init__(self) -> None:
        require_text(self.attempt_id, "attempt_id")
        require_text(self.model, "model")
        require_text(self.prompt_version, "prompt_version")
        if not self.messages:
            raise ValueError("messages 不能为空：provider 请求至少要有一条消息")
        require_positive(self.max_output_tokens, "max_output_tokens")
        require_aware(self.deadline, "deadline")


@dataclass(frozen=True)
class TokenUsage:
    """provider 报告的 token 用量。**缺失时必须显式建模为 `None`**，
    绝不允许"缺了就当 0"—— 那是无声的零成本记账。"""

    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        require_non_negative(self.input_tokens, "input_tokens")
        require_non_negative(self.output_tokens, "output_tokens")


@dataclass(frozen=True)
class RawCitation:
    """provider 返回的**未校验**引用。

    ⚠️ 这是不可信输入：模型可能编造 source_id、拼错 hash、给出越界 span。
    构造时只做类型检查（适配器层的形状保证）；"它是否指向本次已授权
    snapshot 里的那一版原文"由校验层判定（`teaching.validation`）。
    在这里提前拒绝"看起来不可能"的值反而有害 —— 那会把两层校验合并，
    让人误以为 provider 返回的东西已经是可信的。
    """

    source_id: str
    document_id: str
    span_start: int
    span_end: int
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str):
            raise TypeError("source_id 必须是 str")
        if not isinstance(self.document_id, str):
            raise TypeError("document_id 必须是 str")
        if not isinstance(self.span_start, int) or isinstance(self.span_start, bool):
            raise TypeError("span_start 必须是 int")
        if not isinstance(self.span_end, int) or isinstance(self.span_end, bool):
            raise TypeError("span_end 必须是 int")
        if not isinstance(self.content_hash, str):
            raise TypeError("content_hash 必须是 str")


@dataclass(frozen=True)
class ProviderResult:
    """provider 调用的结果。**只有适配器构造它** —— 客户端与 HTTP 层
    不允许捏造这份事实（答案、引用、用量都从这里单向流出）。"""

    attempt_id: str
    status: ProviderStatus
    #: provider 侧的请求标识（对账时与供应商侧比对）。拿不到就留空。
    provider_request_id: str = ""
    #: 答案正文。只有 COMPLETED 才可能有值；其余状态必须为空 ——
    #: "失败状态下带着半截答案"会诱人把未校验的文本当结论展示。
    answer_text: str = ""
    citations: tuple[RawCitation, ...] = field(default=())
    #: provider 报告的用量。`None` = 未报告（费用敞口保留，禁止按零结算）。
    usage: TokenUsage | None = None
    #: 面向运维的**安全**简述（绝不放原始异常文本 / SDK 错误体）。
    detail: str = ""

    def __post_init__(self) -> None:
        require_text(self.attempt_id, "attempt_id")
        if not isinstance(self.status, ProviderStatus):
            raise ValueError(f"status 必须是 ProviderStatus，收到 {self.status!r}")
        if self.status is not ProviderStatus.COMPLETED and self.answer_text:
            raise ValueError(
                f"只有 completed 的结果可以携带答案文本，当前状态是 {self.status}"
            )
        if self.status in UNKNOWN_OUTCOMES and self.usage is not None:
            # 结果未知时不可能同时拿到权威用量 —— 两者同时出现说明
            # 适配器在凭空捏造事实。
            raise ValueError("timeout 的结果不能携带 usage：结果未知时没有权威用量")
        require_text(self.provider_request_id, "provider_request_id", allow_empty=True)
        require_text(self.detail, "detail", allow_empty=True)
