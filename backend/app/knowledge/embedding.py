"""可选 embedding 端口与版本化索引键（R9 任务 3）。

## 为什么是"端口"而不是"客户端"

embedding provider 在本轮**没有任何生产实现**：目标 ECS 的 pgvector
只完成了只读复核（未安装扩展，属生产变更，须用户确认），真实云端
embedding 也未接入。因此这里冻结的是**契约**——结构与协议形状，
加上一个确定性的进程内实现供管线测试。谁实现了 `EmbeddingProvider`，
混合检索就能用谁；实现缺席时检索退回 `keyword/v1` 基线（见 `store.py`）。

## 版本化索引键：四个字段共同决定缓存身份

R9 计划要求：content hash、parser/chunker version、embedding model
revision、最终 input hash **共同**决定一条向量缓存的身份。前三个是
"输入是什么"，第四个是"把前三者绑定后的指纹"：

```text
input_hash = sha256(content_hash | parser_version | model_revision)
```

任何一项变化（重新切块、换 embedding 模型、内容更新）都会产生新的
`input_hash` —— 旧向量在键上**对不上号**，而不是"碰巧还挂在索引里"。
把 model revision 烙进键里，是为了杜绝最隐蔽的一类错：换了 embedding
模型后继续读旧模型的向量，相似度比较在两个语义空间之间做，结果看似
正常、实则无意义，而且没有任何报错。

## `HashingEmbeddingProvider` 的诚实声明

这个实现从文本的 SHA-256 派生固定维度向量：**同文本必得同向量，
不同文本几乎必得无关向量**。它没有任何语义能力 —— "近义查询召回
相关文档"这种 embedding 的核心价值它一概没有。它的用途只有两个：

1. 让混合检索管线（版本匹配、索引查询、RRF 融合、降级路径）可以
   **不依赖任何外部服务**地被测试；
2. 作为实现契约的参照物。

它**不得**出现在生产装配里（`platform.py` 不注入它）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

#: 进程内确定性实现自带的 revision。它的取值没有兼容性承诺：
#: 这是一个实现细节的标识，不是对外协议。
HASHING_EMBEDDING_REVISION = "hashing-embedding/v1"

#: 进程内实现的向量维度。取 64 足够让"不同文本的向量"在排序上
#: 互不干扰，又小到不值得讨论存储。
HASHING_EMBEDDING_DIMENSIONS = 64


@dataclass(frozen=True)
class EmbeddingIndexKey:
    """一条向量缓存条目的**完整身份**。

    `input_hash` 是派生属性（构造时由前三个字段算出），因此
    "键与它的组成部分不一致"在类型上不可表达 —— 与
    `SourceDocument.content_hash` 同一条设计理由。
    """

    content_hash: str
    parser_version: str
    model_revision: str
    input_hash: str

    def __post_init__(self) -> None:
        if not self.content_hash or not self.parser_version or not self.model_revision:
            raise ValueError("索引键的三个组成部分都不能为空")
        expected = _input_hash(self.content_hash, self.parser_version, self.model_revision)
        if self.input_hash != expected:
            raise ValueError(
                "input_hash 与 (content_hash, parser_version, model_revision) 不一致；"
                "索引键必须由 embedding_index_key() 构造"
            )


def _input_hash(content_hash: str, parser_version: str, model_revision: str) -> str:
    """把键的三个组成部分绑成一个指纹。分隔符用 `\\x1f`（单元分隔符）：
    出现在正常文本里的概率为零，杜绝 `a|bc` 与 `ab|c` 拼出同一指纹。"""
    joined = "\x1f".join((content_hash, parser_version, model_revision))
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def embedding_index_key(
    *, content_hash: str, parser_version: str, model_revision: str
) -> EmbeddingIndexKey:
    """索引键的**唯一**构造出口。"""
    return EmbeddingIndexKey(
        content_hash=content_hash,
        parser_version=parser_version,
        model_revision=model_revision,
        input_hash=_input_hash(content_hash, parser_version, model_revision),
    )


class EmbeddingProvider(Protocol):
    """把文本变成向量的端口。

    实现必须满足两条契约：

    1. `model_revision` 是**稳定标识**：同一 revision 下同一文本必须
       产生同一向量（否则缓存键失去意义）。revision 变化 = 新的语义
       空间 = 旧向量全部失效，这是调用方降级判断的依据。
    2. `embed_texts` 对**整批**文本一次性调用 —— provider 内部可以做
       批量请求；调用方逐条调用会把"一批"变成 N 次网络往返。
    """

    @property
    def model_revision(self) -> str: ...

    def embed_texts(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class HashingEmbeddingProvider:
    """确定性哈希向量（见模块 docstring 的诚实声明）。

    向量元素从 `(model_revision, text)` 的 SHA-256 摘要循环取 4 字节
    派生，映射到 `[-1, 1)`。没有任何语义；只保证同输入同输出。
    """

    def __init__(self, *, dimensions: int = HASHING_EMBEDDING_DIMENSIONS) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions 必须为正")
        self._dimensions = dimensions

    @property
    def model_revision(self) -> str:
        return HASHING_EMBEDDING_REVISION

    def embed_texts(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            digest = hashlib.sha256(
                (HASHING_EMBEDDING_REVISION + "\x00" + text).encode("utf-8")
            ).digest()
            components: list[float] = []
            for index in range(self._dimensions):
                offset = (index * 4) % len(digest)
                word = int.from_bytes(digest[offset : offset + 4], "big")
                components.append(word / 2**32 * 2 - 1)
            vectors.append(tuple(components))
        return tuple(vectors)


@dataclass(frozen=True)
class VectorMatch:
    """向量索引的一条命中：键 + 余弦相似度。

    相似度是浮点 —— 它只参与**索引内部**的排序（同一份向量在同一
    台机器上计算是可复现的），不进入 RRF 融合（融合只用排名，见
    `fusion.py`）。排名是整数，融合分数是精确有理数，整条链路上
    "跨实现可复现"的性质不依赖浮点相等。
    """

    key: EmbeddingIndexKey
    similarity: float


class VectorIndex(Protocol):
    """向量索引端口。

    与 `EmbeddingProvider` 同一条纪律：本轮只有进程内实现，
    pgvector 适配器在目标 ECS 完成安装与 RLS 形状验证之前**不写**。
    实现必须自带 `model_revision`（索引构建时的 embedding revision），
    调用方靠它判定"换模型后旧索引是否整体失效"。
    """

    @property
    def model_revision(self) -> str: ...

    def upsert(self, key: EmbeddingIndexKey, embedding: tuple[float, ...]) -> None: ...

    def has(self, key: EmbeddingIndexKey) -> bool: ...

    def search(
        self,
        keys: tuple[EmbeddingIndexKey, ...],
        embedding: tuple[float, ...],
        *,
        limit: int,
    ) -> tuple["VectorMatch", ...]: ...


class InMemoryVectorIndex:
    """进程内向量索引（开发 / 测试适配器）。

    它**不做作用域过滤**：`search` 只在调用方传入的键集合里排序。
    候选集的收窄（tenant / project / 最新版本）发生在
    `IngestionRepository.stored_chunks` 的 SQL / RLS 边界内 —— 02 号
    规格 §7 禁止把跨项目候选拉到应用层再筛，索引不能成为绕过那条
    边界的第二条路。
    """

    def __init__(self, *, model_revision: str) -> None:
        if not model_revision:
            raise ValueError("model_revision 不能为空")
        self._model_revision = model_revision
        self._vectors: dict[str, tuple[float, ...]] = {}

    @property
    def model_revision(self) -> str:
        return self._model_revision

    def upsert(self, key: EmbeddingIndexKey, embedding: tuple[float, ...]) -> None:
        """写入（或覆盖）一条向量。同 `input_hash` 幂等覆盖。

        revision 不一致的键直接拒绝：索引是**一个**语义空间的存量，
        混入另一个空间的向量不会报错，只会让相似度比较静默失去意义。
        """
        if key.model_revision != self._model_revision:
            raise ValueError(
                f"索引 revision 是 {self._model_revision!r}，"
                f"拒绝写入 {key.model_revision!r} 的向量"
            )
        if not embedding:
            raise ValueError("向量不能为空")
        self._vectors[key.input_hash] = embedding

    def has(self, key: EmbeddingIndexKey) -> bool:
        return key.input_hash in self._vectors

    def search(
        self,
        keys: tuple[EmbeddingIndexKey, ...],
        embedding: tuple[float, ...],
        *,
        limit: int,
    ) -> tuple[VectorMatch, ...]:
        """在**给定的键集合**内按余弦相似度取前 `limit`。

        只对 `keys` 里已有向量的条目排序 —— 键在集合里但没被索引过，
        意味着"这个片段没有可用向量"，跳过而不是报错（部分缺向量是
        索引构建的常态，整体缺失才触发降级，判定在 `store.py`）。
        """
        if limit <= 0:
            return ()
        query_norm = _norm(embedding)
        if query_norm == 0.0:
            return ()
        scored: list[VectorMatch] = []
        for key in keys:
            stored = self._vectors.get(key.input_hash)
            if stored is None or len(stored) != len(embedding):
                continue
            stored_norm = _norm(stored)
            if stored_norm == 0.0:
                continue
            # 长度已在上面校验相等，strict 让"看似相等实则漂移"直接失败。
            dot = sum(a * b for a, b in zip(embedding, stored, strict=True))
            scored.append(VectorMatch(key=key, similarity=dot / (query_norm * stored_norm)))
        # tie-break 用 `input_hash`：相似度相同的两条命中必须有稳定次序，
        # 否则"同输入不同输出"会以评测指标抖动的形式出现。
        scored.sort(key=lambda match: (-match.similarity, match.key.input_hash))
        return tuple(scored[:limit])


def _norm(vector: tuple[float, ...]) -> float:
    return sum(component * component for component in vector) ** 0.5


def embed_chunk_key(
    *, chunk_content_hash: str, chunk_parser_version: str, model_revision: str
) -> EmbeddingIndexKey:
    """从片段派生索引键的便捷出口（`content_hash` 参数名区分于模块函数）。"""
    return embedding_index_key(
        content_hash=chunk_content_hash,
        parser_version=chunk_parser_version,
        model_revision=model_revision,
    )


__all__ = [
    "HASHING_EMBEDDING_DIMENSIONS",
    "HASHING_EMBEDDING_REVISION",
    "EmbeddingIndexKey",
    "EmbeddingProvider",
    "HashingEmbeddingProvider",
    "InMemoryVectorIndex",
    "VectorIndex",
    "VectorMatch",
    "embedding_index_key",
    "embed_chunk_key",
]
