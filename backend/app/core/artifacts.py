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
    信封里没有原文，但任何人拿到 `source_id + span + content_hash` 都能把原文取回来。
    """

    source_id: str
    span: tuple[int, int]
    content_hash: str
    parser_version: str
    display_policy: DisplayPolicy = DisplayPolicy.FULL

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "span": list(self.span),
            "content_hash": self.content_hash,
            "parser_version": self.parser_version,
            "display_policy": str(self.display_policy),
        }

    def is_verifiable(self) -> bool:
        """指纹必须是可校验的 sha256，否则谱系无从追溯。"""
        return self.content_hash.startswith("sha256:") and len(self.content_hash) > 10
