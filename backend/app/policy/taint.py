"""Taint 与 sink-specific endorsement。

设计依据：
- 不变量 #10 —— Taint 默认只增；去污点只能生成面向特定 sink、schema、主体和操作的
  有时限 endorsement；**模型摘要不能自动去污点**。
- 03 号规格 §3 —— SQL 参数的 endorsement 不得用于 Shell 或 URL；跨 endorsement
  拼接会生成新的 tainted value，不能沿用旧授权。

三条性质在代码层面强制：
1. 值一旦带来源即 tainted，且只能增加来源，不能清除；
2. 派生（拼接、摘要、子 agent 处理）产出**新值**，旧 endorsement 一律失效；
3. endorsement 绑定 (主体, 操作, sink, schema, 值哈希, 期限)，且**单次使用**，
   已使用过的 endorsement 不能再次授权。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash
from app.core.ids import new_id


class TaintSource(StrEnum):
    """不可信或受限来源。按 00 号规格 §1 的信任边界列举。"""

    USER_INPUT = "user_input"
    UPLOADED_SOURCE = "uploaded_source"
    WEB = "web"
    REPO = "repo"
    MODEL_OUTPUT = "model_output"
    MCP = "mcp"
    SANDBOX = "sandbox"
    THIRD_PARTY_TOOL = "third_party_tool"


class EndorsementSink(StrEnum):
    """endorsement 只能面向一个具体 sink。"""

    MODEL_CONTEXT = "model_context"
    SQL = "sql"
    SHELL = "shell"
    URL = "url"
    FILESYSTEM = "filesystem"


@dataclass(frozen=True)
class TaintedValue:
    """内容引用 + 来源标记。值本身不在此处保存，只保存稳定引用与哈希。"""

    value_ref: str
    content_hash: str
    sources: tuple[TaintSource, ...] = ()
    derived_from: tuple[str, ...] = ()

    @property
    def tainted(self) -> bool:
        return bool(self.sources)

    def to_dict(self) -> dict:
        return {
            "value_ref": self.value_ref,
            "content_hash": self.content_hash,
            "sources": [str(s) for s in self.sources],
            "derived_from": list(self.derived_from),
            "tainted": self.tainted,
        }


def mark_tainted(value_ref: str, payload, source: TaintSource) -> TaintedValue:
    """把一段内容标记为带某个来源。"""
    return TaintedValue(
        value_ref=value_ref,
        content_hash=content_hash(payload),
        sources=(source,),
    )


def derive(
    parents: list[TaintedValue],
    new_value_ref: str,
    payload,
    *,
    new_sources: tuple[TaintSource, ...] = (),
) -> TaintedValue:
    """派生新值：来源取并集，且旧 endorsement 不再适用。

    这是「模型摘要不能自动去污点」的实现点：摘要是一个派生动作，
    结果值的来源集合必然包含原值的全部来源。

    ## `new_sources` 为什么必须显式传，且默认不增加

    派生有两类，taint 语义**不同**（03 号规格 §3）：

    | 派生类型 | 来源变化 |
    |---|---|
    | 确定性派生（解析、拼接、规范化） | 只继承父来源，**不新增** |
    | 模型边界产生新内容 | **必须**显式增加 `MODEL_OUTPUT` |

    早期实现只有「合并父来源」一种行为，于是模型生成的内容永远不会被标上
    `MODEL_OUTPUT` —— 只要父值里没有这个来源，模型输出就会被当成普通派生值，
    混进证据链。这是真实的漏标缺口，不是文档措辞问题。

    默认值取空元组是刻意的：**默认安全**。模型边界必须由调用方显式声明
    （推荐用 `derive_model_output()`，它把这件事变成一个可搜索的调用点），
    而确定性派什么都不用做，也就不会误标。
    """
    if not parents:
        raise ValueError("derive 至少需要一个父值")

    seen: list[TaintSource] = []
    for parent in parents:
        for source in parent.sources:
            if source not in seen:
                seen.append(source)
    for source in new_sources:
        if source not in seen:
            seen.append(source)

    return TaintedValue(
        value_ref=new_value_ref,
        content_hash=content_hash(payload),
        sources=tuple(seen),
        derived_from=tuple(p.value_ref for p in parents),
    )


def derive_model_output(
    parents: list[TaintedValue], new_value_ref: str, payload
) -> TaintedValue:
    """**模型边界**产生新内容时的派生：显式增加 `MODEL_OUTPUT` 并继承全部父来源。

    单独提供一个入口，而不是让每个调用点自己记得传
    `new_sources=(TaintSource.MODEL_OUTPUT,)`。理由是可审计性：
    「哪些地方产生了模型内容」应该是一个可以直接搜索的函数名，
    而不是散落各处的参数写法 —— 后者只要有人漏写一次，
    模型输出就会被静默当成普通派生值。
    """
    return derive(
        parents,
        new_value_ref,
        payload,
        new_sources=(TaintSource.MODEL_OUTPUT,),
    )


@dataclass(frozen=True)
class Endorsement:
    """有时限、面向单一 sink 的使用授权。它不是「把原值变干净」。"""

    endorsement_id: str
    value_hash: str
    subject_id: str
    operation: str
    sink: EndorsementSink
    schema_id: str
    policy_decision_id: str
    issued_at: datetime
    expires_at: datetime
    used: bool = False


class EndorsementRegistry:
    """endorsement 的签发与核验。全部状态在内存中，可丢失且不影响正确性。

    可丢失是刻意的：endorsement 是短期凭证，不像 Evidence 那样是事实。
    """

    def __init__(self) -> None:
        self._issued: dict[str, Endorsement] = {}

    def issue(
        self,
        *,
        value: TaintedValue,
        subject_id: str,
        operation: str,
        sink: EndorsementSink,
        schema_id: str,
        policy_decision_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> Endorsement:
        """签发 endorsement。三件事必须同时绑定：主体、操作、sink。"""
        if not value.tainted:
            raise deny(
                ErrorCode.TAINT_REQUIRES_ENDORSEMENT,
                "该值不带 taint，无需 endorsement；不接受无意义的签发请求",
            )
        if expires_at <= issued_at:
            raise deny(
                ErrorCode.ENDORSEMENT_EXPIRED,
                "endorsement 的过期时间必须晚于签发时间",
            )
        endorsement = Endorsement(
            endorsement_id=new_id("end"),
            value_hash=value.content_hash,
            subject_id=subject_id,
            operation=operation,
            sink=sink,
            schema_id=schema_id,
            policy_decision_id=policy_decision_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        self._issued[endorsement.endorsement_id] = endorsement
        return endorsement

    def authorize(
        self,
        endorsement: Endorsement,
        *,
        value: TaintedValue,
        subject_id: str,
        operation: str,
        sink: EndorsementSink,
        schema_id: str,
        now: datetime,
    ) -> None:
        """核验使用授权。任一项不匹配即拒绝，不做任何"部分放行"。"""
        if endorsement.used:
            raise deny(
                ErrorCode.ENDORSEMENT_REPLAY_DENIED,
                "该 endorsement 已被使用；历史记录可审计，但不能作为未来执行的授权",
                endorsement_id=endorsement.endorsement_id,
            )
        if now >= endorsement.expires_at:
            raise deny(
                ErrorCode.ENDORSEMENT_EXPIRED,
                f"endorsement 已于 {endorsement.expires_at.isoformat()} 过期",
                endorsement_id=endorsement.endorsement_id,
            )
        if endorsement.value_hash != value.content_hash:
            raise deny(
                ErrorCode.TAINT_REQUIRES_ENDORSEMENT,
                "endorsement 绑定的值哈希与当前值不一致；派生或拼接会生成新值，旧授权失效",
                expected=endorsement.value_hash,
                actual=value.content_hash,
            )
        if (
            endorsement.subject_id != subject_id
            or endorsement.operation != operation
            or endorsement.sink is not sink
            or endorsement.schema_id != schema_id
        ):
            raise deny(
                ErrorCode.ENDORSEMENT_SINK_MISMATCH,
                f"endorsement 的绑定范围不匹配：签发为 "
                f"({endorsement.subject_id}, {endorsement.operation}, {endorsement.sink}, "
                f"{endorsement.schema_id})，本次请求为 ({subject_id}, {operation}, {sink}, {schema_id})",
                endorsement_id=endorsement.endorsement_id,
            )

    def consume(self, endorsement: Endorsement) -> Endorsement:
        """标记为已使用。核验通过后必须立刻调用，避免同一授权被复用两次。"""
        consumed = Endorsement(
            endorsement_id=endorsement.endorsement_id,
            value_hash=endorsement.value_hash,
            subject_id=endorsement.subject_id,
            operation=endorsement.operation,
            sink=endorsement.sink,
            schema_id=endorsement.schema_id,
            policy_decision_id=endorsement.policy_decision_id,
            issued_at=endorsement.issued_at,
            expires_at=endorsement.expires_at,
            used=True,
        )
        self._issued[consumed.endorsement_id] = consumed
        return consumed
