"""知识检索：两个**层次不同**的检索件住在这里，名字必须让人一眼分得清。

| 件 | 回答的问题 | 谁在用 |
|---|---|---|
| `ChunkIndex`（第 1 轮开发适配器） | "交互运行时手里那几个片段里有没有词命中" | `workflow` 的 `retrieve_material` |
| `rank_chunks()` / `ScoredChunk`（第 4 轮基线） | "项目里**已入库**的片段，按确定性关键词分数取前 N" | `knowledge/store.py` |

⚠️ 不要把两者互相替换。`ChunkIndex` 是**进程内**的、靠 `tenancy.current()` 取作用域；
`rank_chunks` 是**纯函数**，作用域由仓储在 SQL / RLS 内收窄后才交给它。
把排序搬进 `ChunkIndex` 会让"作用域从哪来"这件事重新变得模糊。

## 第 4 轮基线的"确定性"从哪来

确定性不是"没有随机数"这么轻的要求。同一条查询在同一份数据上必须给出**逐位相同**
的顺序，否则评测集里的指标每次跑都不一样，而"回归"就无从判定。这里靠四条：

1. **归一化只有一步**：NFKC + 小写。全角/半角、兼容字符先折叠成同一形式 ——
   用户从 PDF 粘来的文本里全角字符很常见，不折叠的话 `Ｐｏｓｔｇｒｅｓ` 与
   `postgres` 是两个词，检索会"莫名"漏掉。
2. **分数是整数加法**。不用浮点：浮点求和不满足结合律，"几乎相等"的两条候选
   在不同求和顺序下会交换位置 —— 而这种漂移在测试里表现为"偶发失败"。
3. **命中的每一项都进 `matched_terms`**，排序键不依赖字典遍历顺序。
4. **tie-break 用 `(source_id, chunk_index)`**，不用 `chunk_id`：
   `chunk_id` 是随机生成的，拿它当次序等于"同分时随机排"。

## 这一版**不做**什么（诚实标注）

没有中文分词器、没有 `tsvector`、没有向量、没有 rerank、没有同义词与停用词。
因此它只能回答"命中了没有"，**不能**回答"哪条更相关" —— 下面
`RELEVANCE_FLOOR` 的取值就受这个能力边界限制，注释里写清楚了。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.core.artifacts import ArtifactRef, DisplayPolicy
from app.knowledge.models import StoredChunk
from app.policy.taint import TaintedValue, TaintSource, mark_tainted
from app.tenancy.context import current

# ------------------------------------------------------------------ 版本与权重

#: 排序算法的版本。**改动分词或打分规则必须同时改它。**
#: `backend/tests/fixtures/retrieval_v1.json` 记着冻结的基线指标：
#: 版本没变而指标变了，就说明有人在没意识到的情况下改了召回 ——
#: 而"没意识到"正是本版本号要消灭的状态。
RANKING_VERSION = "keyword/v1"

#: 固定整数权重。含义与理由见模块 docstring 第 2 条。
#: 整句命中给到 100，是为了让"用户原样粘贴的那句话"稳定压过
#: "恰好凑齐了所有 2-gram 的长段落" —— 后者分数会随篇幅线性增长。
WEIGHT_EXACT_PHRASE = 100
#: 命中落在标题路径上。标题是作者亲手写的层级概括，
#: 同一次命中落在标题上比落在正文里更值得排前。
WEIGHT_HEADING_TERM = 10
#: 命中落在正文里。
WEIGHT_CONTENT_TERM = 3
#: 查询的**每一项**都命中（标题或正文）。加分而非乘法：整数加法保确定性。
WEIGHT_ALL_TERMS_COVERED = 15

#: 相关度下限。
#:
#: ⚠️ 取 1 是**受能力边界限制**的结果，不是随便选的：这一版只有整数关键词分数，
#: 除了"有没有命中"之外没有第二个可判的量。取更大的值就等于编一个
#: "低于 X 分就算不相关"的门槛，而那个 X 没有任何证据支持。
#: 后果是 `LOW_RELEVANCE` 在本轮的检索路径上**不会触发**（`rank_chunks`
#: 已经保证每个候选的分数 > 0）—— 与 `evidence_state.py` 里 `SCOPE_BLOCKED`
#: 的标注同一性质：判定已就绪，但当前路径产生不了它。等有了 embedding / rerank
#: 的连续分数，这个下限才有实际含义。
RELEVANCE_FLOOR = 1

#: 相邻汉字组成的 2-gram 的原料。中文没有词边界：逐字匹配过宽
#: （单字"回"能命中"回家"），整句匹配过窄（用户很少整句照抄）。
#: 2-gram 是这两者之间最省事又不失真太多的折中。
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+|[^\u4e00-\u9fff]+")
#: 空白与标点/符号的切分。CJK 是 `\w` 的一部分，所以汉字段不会被切开。
_RUN_SPLIT_RE = re.compile(r"[\s\W_]+")


# ------------------------------------------------------------------ 归一化与分词


def normalize_text(text: str) -> str:
    """NFKC 归一 + 小写。**归一化的唯一出口** —— 索引与查询必须走同一段代码。

    分两处各写一次的话，"同一个词"在查询侧与索引侧会得到两个不同的形式，
    表现是**偶发的漏召回**：只有含全角字符的资料会搜不到，而它看起来
    像"这份资料没入库"。
    """
    return unicodedata.normalize("NFKC", text).lower()


def query_terms(query: str) -> tuple[str, ...]:
    """把查询拆成**加法项**：标点/空白切出的段，加上汉字段的 2-gram。

    顺序稳定且去重（按首次出现）：同一查询永远得到同一个元组。
    元组而不是集合，正是因为集合的遍历顺序不可依赖 ——
    而下面的加分虽然与顺序无关，`matched_terms` 会进响应，
    顺序抖动会让"同一查询两次响应不同"这种假故障反复出现。
    """
    normalized = normalize_text(query)
    terms: list[str] = []
    for run in _RUN_SPLIT_RE.split(normalized):
        if not run:
            continue
        # 进一步按「汉字 / 非汉字」分段：混排的 `postgres回滚` 不该产生
        # `s回` 这种跨字符集的大 gram，那是纯噪音。
        for segment in _CJK_RUN_RE.findall(run):
            terms.append(segment)
            if len(segment) > 2 and _is_cjk(segment):
                terms.extend(segment[i : i + 2] for i in range(len(segment) - 1))
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            unique.append(term)
    return tuple(unique)


def _is_cjk(segment: str) -> bool:
    return _CJK_RUN_RE.fullmatch(segment) is not None and not segment.isascii()


# ------------------------------------------------------------------ 打分


@dataclass(frozen=True)
class ScoredChunk:
    """一个候选片段及其确定性分数。

    `matched_terms` 不是给用户看的"解释"，它是排障用的**证据**：
    "为什么这条排在前面"必须能由数据回答，而不是靠把代码跑一遍再看。
    """

    chunk: StoredChunk
    score: int
    matched_terms: tuple[str, ...] = ()

    @property
    def citation(self) -> ArtifactRef:
        """谱系引用。**内容本体不进响应也不需要**：拿到
        `source_id + document_id + span + content_hash + parser_version`
        就能把**那一版**的原文取回来。

        `document_id` 是必需的：同一来源的两版片段可能落在同一个 span 上，
        少了它，回读可能返回另一版（见 `ArtifactRef` 的 docstring）。
        """
        return self.chunk.as_artifact_ref()

    def as_tainted(self) -> TaintedValue:
        """检索结果天然是外部内容，一律带 taint（不变量 #10）。"""
        return mark_tainted(self.chunk.chunk_id, self.chunk.content, TaintSource.UPLOADED_SOURCE)

    def to_dict(self) -> dict:
        return {
            **self.chunk.to_dict(),
            "score": self.score,
            "matched_terms": list(self.matched_terms),
            "citation": self.citation.to_dict(),
        }


def score_chunk(
    chunk: StoredChunk, terms: tuple[str, ...], phrase: str
) -> tuple[int, tuple[str, ...]]:
    """算一条候选的分数与命中项。**整数加法，无浮点**（见模块 docstring）。

    ⚠️ 命中落在标题上就**不**再在正文里重复计一次（`elif` 的用意）：
    标题文字通常也出现在片段正文里，两处都加会让"标题命中"被算两遍，
    于是分数里混进了一个我们没打算赋予的含义。
    """
    heading = normalize_text(chunk.heading_text)
    content = normalize_text(chunk.content)
    score = 0
    matched: list[str] = []

    if phrase and phrase in content:
        score += WEIGHT_EXACT_PHRASE
        matched.append(phrase)

    heading_hits: tuple[str, ...] = tuple(term for term in terms if term in heading)
    content_hits: tuple[str, ...] = tuple(
        term for term in terms if term not in heading_hits and term in content
    )
    score += WEIGHT_HEADING_TERM * len(heading_hits)
    score += WEIGHT_CONTENT_TERM * len(content_hits)
    matched.extend(heading_hits)
    matched.extend(content_hits)

    if terms and len(heading_hits) + len(content_hits) == len(terms):
        score += WEIGHT_ALL_TERMS_COVERED

    return score, tuple(matched)


def rank_chunks(
    chunks: tuple[StoredChunk, ...], query: str, *, limit: int
) -> tuple[ScoredChunk, ...]:
    """确定性排序，取前 `limit` 条。**纯函数**：不读上下文、不碰存储。

    "零分候选不进结果"是刻意的：分数 0 意味着一个词都没命中。把它们一并返回，
    会让"检索到了东西"这件事失去意义 —— 而证据判定正是按候选数说话的
    （`NO_CANDIDATES`）。**返回空集是一个诚实的答案。**
    """
    if limit <= 0:
        return ()
    phrase = normalize_text(query).strip()
    terms = query_terms(query)
    if not phrase or not terms:
        return ()
    scored = [
        ScoredChunk(chunk=chunk, score=score, matched_terms=matched)
        for chunk in chunks
        for score, matched in (score_chunk(chunk, terms, phrase),)
        if score > 0
    ]
    scored.sort(key=lambda item: (-item.score, item.chunk.source_id, item.chunk.chunk_index))
    return tuple(scored[:limit])


# ------------------------------------------------- 第 1 轮开发适配器（交互路径）


@dataclass(frozen=True)
class Chunk:
    """一个可检索片段。属于某个项目，来源可追溯。"""

    chunk_id: str
    tenant_id: str
    learning_project_id: str
    source_id: str
    span: tuple[int, int]
    text: str
    parser_version: str = "parser/v1"
    display_policy: DisplayPolicy = DisplayPolicy.FULL
    origin: TaintSource = TaintSource.UPLOADED_SOURCE

    def artifact_ref(self) -> ArtifactRef:
        """转成谱系引用。内容本体不进上下文，只带指针与指纹。

        `document_id` 传空串：本适配器是**进程内**的，没有持久化文档，
        因此不存在"同一来源多版本、跨度相同"的问题 ——
        那条路径上的精确回读由进程内的 `chunk_id` 承担。
        持久化路径（`knowledge/models.py` 的 `StoredChunk`）必须带上真实
        `document_id`。
        """
        value = mark_tainted(self.chunk_id, self.text, self.origin)
        return ArtifactRef(
            source_id=self.source_id,
            document_id="",
            span=self.span,
            content_hash=value.content_hash,
            parser_version=self.parser_version,
            display_policy=self.display_policy,
        )


@dataclass(frozen=True)
class IndexedMatch:
    """`ChunkIndex` 的命中项。

    名字**刻意不叫 `ScoredChunk`**：那会与上面第 4 轮的候选同名。
    两个不同的东西共用一个名字，读代码的人会按其中一种语义去理解另一种 ——
    "都叫候选"最容易掩盖的差别恰恰是作用域从哪来（一个是上下文，
    一个是调用方已经收窄好的入参）。
    """

    chunk: Chunk
    score: int

    def as_tainted(self) -> TaintedValue:
        """检索结果天然是外部内容，一律带 taint。"""
        return mark_tainted(self.chunk.chunk_id, self.chunk.text, self.chunk.origin)


class ChunkIndex:
    """进程内片段索引（第 1 轮开发适配器）。租户与项目过滤固化在查询路径里。

    ⚠️ **降级声明**：本类是进程内关键词匹配，**不是**设计中的混合检索管线
    （中文分词 + `tsvector` + pgvector + 融合 + rerank）。第 4 轮的检索
    （`rank_chunks` + `knowledge/store.py`）走的是另一条路，两者互不替代。

    有两点语义不能因为简化而省略：

    1. 租户与项目过滤在**内部**强制 —— 不能先把跨租户候选拉上来再在应用层筛
       （02 号规格 §7 明确禁止）；
    2. 检索结果一律携带 taint 与来源引用，否则引用装配与展示策略都无从判定。
    """

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []

    def add(self, chunk: Chunk) -> None:
        """写入片段。租户必须与当前上下文一致。"""
        context = current()
        if chunk.tenant_id != context.tenant_id:
            from app.core.errors import ErrorCode, deny

            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "摄取片段所属租户与当前上下文不一致",
                expected=context.tenant_id,
                actual=chunk.tenant_id,
            )
        self._chunks.append(chunk)

    def search(self, query: str, *, limit: int = 5) -> list[IndexedMatch]:
        """按词命中数打分。

        过滤在**内部**完成：先按租户、再按项目，之后才打分排序。
        """
        context = current()
        project_id = context.require_project()
        terms = [t for t in _tokenize(query) if t]
        scored: list[IndexedMatch] = []
        for chunk in self._chunks:
            if chunk.tenant_id != context.tenant_id:
                continue
            if chunk.learning_project_id != project_id:
                continue
            text = chunk.text.lower()
            score = sum(1 for term in terms if term in text)
            if score > 0:
                scored.append(IndexedMatch(chunk=chunk, score=score))
        scored.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
        return scored[:limit]

    def read_span(self, source_id: str, span: tuple[int, int]) -> Chunk | None:
        """按 `source_id` + `span` 精确回读。

        **租户与项目过滤在这里强制**，调用方不需要、也不应该自己判断作用域。
        把这段逻辑留给调用方，等于把隔离交给"记得写"——迟早会漏。
        """
        context = current()
        project_id = context.require_project()
        for chunk in self._chunks:
            if chunk.tenant_id != context.tenant_id:
                continue
            if chunk.learning_project_id != project_id:
                continue
            if chunk.source_id == source_id and tuple(chunk.span) == tuple(span):
                return chunk
        return None


def _tokenize(query: str) -> list[str]:
    """极简分词：按空白与非字母数字切分。

    真实实现需要中文分词器并在自有语料上做基准（02 号规格 §3），此处从简。
    """
    return [t for t in _RUN_SPLIT_RE.split(query.lower()) if t]
