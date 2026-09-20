"""跨层共享的谱系引用契约。

**为什么放在 `core` 而不是某个 L4 模块里**：`knowledge` 需要产出引用，
`execution` 需要校验引用。如果其中一个模块定义、另一个导入，就形成了
L4 模块之间的横向依赖 —— 那会同时出现两个策略/审计注入点，等于多开一条绕过路径
（06 号规格 §2.1 明确禁止）。把契约下沉到 `core` 是唯一的干净解法。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DisplayPolicy(StrEnum):
    """来源许可决定内容的展示方式。检索展示必须遵守它。"""

    FULL = "full"
    SUMMARY = "summary"
    CITATION_ONLY = "citation_only"


@dataclass(frozen=True)
class ArtifactRef:
    """证据引用。内容本体不进上下文，只带指针与指纹。

    这是「上下文可以丢，谱系不能丢」的具体载体：
    信封里没有原文，但任何人拿到这些字段都能把**那一版**原文取回来。

    ## `document_id` 为什么是必需的，而且排在最前面

    同一份资料（`source_id`）可以有多个版本，而两版的片段**可能落在同一个
    span 上**（改写过的段落常常长度相近甚至相同）。此时只按
    `source_id + span` 回读，返回哪一版取决于存储顺序 —— 实测命中
    `doc_v2 / 'delta!'`，回读拿到 `doc_v1 / 'bravo!'`，而引用看起来完全正常。

    所以"不可变标识"必须是引用的一部分，而不是回读时的一个可选参数：
    放在第 2 个位置（紧跟 `source_id`）是为了让**每一个构造点都必须写出来** ——
    漏掉它就构造不出对象，而不是静默产生一个指向"某一版"的引用。

    ⚠️ 进程内的 `ChunkIndex`（第 1 轮开发适配器）没有持久化文档概念，
    它传空串：那条路径上"精确回读"由进程内的 `chunk_id` 承担，
    与这里要解决的"多版本同跨度"不是同一个问题。
    """

    source_id: str
    document_id: str
    span: tuple[int, int]
    content_hash: str
    parser_version: str
    display_policy: DisplayPolicy = DisplayPolicy.FULL

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "document_id": self.document_id,
            "span": list(self.span),
            "content_hash": self.content_hash,
            "parser_version": self.parser_version,
            "display_policy": str(self.display_policy),
        }

    def is_verifiable(self) -> bool:
        """指纹必须是可校验的 sha256，否则谱系无从追溯。"""
        return self.content_hash.startswith("sha256:") and len(self.content_hash) > 10
