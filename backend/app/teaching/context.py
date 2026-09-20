"""教学上下文的构建：检索快照、历史窗口与预算裁剪。

## 快照为什么是"冻结"的

`RetrievalSnapshot` 在 run 的执行期只构建**一次**并整体进入
`ProviderRequest`（不可变）。重启 / 重放 / 重试都复用同一份 ——
"检索快照冻结 ranking_version、chunk 集合与内容"意味着：同一个 run
无论谁、在何时重新校验引用，比对的基准都是同一批片段。
悄悄换成"当前检索结果"会让校验变成移动的球门。

## 外发白名单在这里收口

`display_policy` 是来源许可。**只有 `full` 的片段进入 provider 上下文**：
`citation_only` 的正文从不出服务端（用户不该看到，模型也不该看到）；
`summary` 的正文同样不外发 —— "摘要展示"是给用户的呈现约束，
把它当成"可以发全文给模型"的许可，是对同一字段的双重解释（铁律 5）。

## 裁剪是显式策略，不是"截断就好"

超限时的裁剪顺序（保留优先级从高到低）：
1. **问题本身**：超长直接拒绝（`PARAMS_INVALID`）—— 问题的完整性
   不允许牺牲，而且上限内装不下说明请求本身不对；
2. **检索片段**：按排名从高到低保留到条数上限；
3. **历史**：从最新往旧保留到字符预算。
租户/权限约束**从不**参与裁剪 —— 能进快照的片段已经过了
`ingestion.stored_chunks` 的作用域收窄，裁剪只删内容，不删边界。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ErrorCode, PlatformError
from app.identity.models import Principal
from app.knowledge.retrieval import ScoredChunk
from app.knowledge.store import KnowledgeRepository
from app.product.models import Message
from app.teaching.models import MaterialSnippet, PromptMessage
from app.teaching.prompts import build_messages
from app.teaching.runs import TeachingRun

#: 检索快照的默认条数上限。
DEFAULT_MAX_MATERIALS = 5

#: 历史窗口的默认字符预算（含角色标记）。
DEFAULT_HISTORY_BUDGET_CHARS = 4000

#: 问题长度上限（字符）。超限拒绝而不是裁剪。
MAX_QUESTION_CHARS = 4000


@dataclass(frozen=True)
class RetrievalSnapshot:
    """一次教学运行的冻结检索快照。"""

    ranking_version: str
    items: tuple[MaterialSnippet, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.ranking_version, str) or not self.ranking_version:
            raise ValueError("ranking_version 不能为空")

    def to_dicts(self) -> tuple[dict, ...]:
        return tuple(item.as_citation_dict() for item in self.items)


@dataclass(frozen=True)
class TeachingContext:
    """派发前的完整上下文：消息序列 + 快照（校验与重放都用它）。"""

    run: TeachingRun
    snapshot: RetrievalSnapshot
    messages: tuple[PromptMessage, ...]


def build_snapshot(
    actor: Principal,
    project_id: str,
    *,
    knowledge: KnowledgeRepository,
    hits: tuple[ScoredChunk, ...],
    ranking_version: str,
    max_items: int = DEFAULT_MAX_MATERIALS,
) -> RetrievalSnapshot:
    """从检索命中构建冻结快照。

    - **只收 `full`**：`summary` / `citation_only` 的正文不出服务端
      （见模块 docstring 的外发白名单）；
    - 命中已由 `knowledge.search` 按成员关系收窄过 —— 这里**不再**放宽；
    - 按排名保留前 `max_items` 条。
    """
    items: list[MaterialSnippet] = []
    for hit in hits:
        if hit.chunk.display_policy.value != "full":
            continue
        items.append(
            MaterialSnippet(
                source_id=hit.chunk.source_id,
                document_id=hit.chunk.document_id,
                chunk_id=hit.chunk.chunk_id,
                span_start=hit.chunk.span_start,
                span_end=hit.chunk.span_end,
                content=hit.chunk.content,
                content_hash=hit.chunk.content_hash,
                parser_version=hit.chunk.parser_version,
                display_policy=str(hit.chunk.display_policy),
            )
        )
        if len(items) >= max_items:
            break
    return RetrievalSnapshot(ranking_version=ranking_version, items=tuple(items))


def history_window(
    messages: tuple[Message, ...], *, budget_chars: int = DEFAULT_HISTORY_BUDGET_CHARS
) -> tuple[tuple[str, str], ...]:
    """从最新往旧保留对话历史，直到字符预算用尽。

    输入**不含**本次问题（它在 run 行里，单独进 prompt）。
    返回 (role, content) 对，顺序为时间正序。
    """
    picked: list[tuple[str, str]] = []
    used = 0
    for message in reversed(messages):
        piece = len(message.content) + 8
        if used + piece > budget_chars:
            break
        picked.append((str(message.role), message.content))
        used += piece
    picked.reverse()
    return tuple(picked)


def build_context(
    run: TeachingRun,
    actor: Principal,
    project_id: str,
    *,
    knowledge: KnowledgeRepository,
    history: tuple[Message, ...],
) -> TeachingContext:
    """组装派发上下文。检索与历史预算在这里收口（服务层不再自己裁）。"""
    if len(run.question) > MAX_QUESTION_CHARS:
        raise PlatformError(
            ErrorCode.PARAMS_INVALID,
            f"问题超过 {MAX_QUESTION_CHARS} 字符上限；请拆分后再问",
        )
    hits = knowledge.search(actor, project_id, run.question, limit=DEFAULT_MAX_MATERIALS)
    snapshot = build_snapshot(
        actor,
        project_id,
        knowledge=knowledge,
        hits=hits,
        ranking_version=run.ranking_version,
    )
    messages = build_messages(
        question=run.question,
        history=history_window(history),
        materials=snapshot.items,
    )
    return TeachingContext(run=run, snapshot=snapshot, messages=messages)
