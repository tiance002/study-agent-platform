"""确定性结构切块器。

设计依据：02 号规格 §3（分块）、本轮计划的「不改写证据原文」不变量。

## 一条不可动摇的性质

> 每个片段的内容**就是原文的一个精确切片**：`text[span] == chunk.content`。

这条性质由本模块保证（片段内容一律取自 `text[start:end]`），并且**在写入前
再核验一次**（`assert_chunks_match_document`，对着**持久化原文**）。

⚠️ `StoredChunk` 的类型约束（`span_end - span_start == len(content)`）**只**
保证长度对口，不能替代上面那句话 —— `content_hash` 也是对片段自身取哈希。
两者都自洽而与原文无关：实测把片段内容换成等长短文本，构造层过得去（R4-05）。

## 媒体类型决定解析路径

`text/plain` 与 `text/markdown` 走**两条**解析路径，而不是"一套 Markdown 规则
容忍纯文本"：

| 类型 | 标题 | 围栏 | `# 开头的一行` |
|---|---|---|---|
| `text/markdown` | 识别 ATX 标题 | 识别代码围栏 | 标题 |
| `text/plain` | 不识别（路径恒为空） | 不识别 | **普通文字** |

原因是实测的后果不对称：`# plain heading` 是一份**完全合法**的纯文本，
按 Markdown 规则却成了"只有标题、没有正文"的节 → 整篇被丢弃、产出**零片段**。
用户看到的是"上传成功但搜不到"，而库里确实什么都没有。

纯文本那边因此有一条更强的性质：**非空内容必然产出至少一个片段**。
（Markdown 允许"只有标题"的文档产出零片段 —— 它确实没有可检索的正文，
这是刻意的语义，见 `_is_heading_only`。）

## 只增加结构元数据，不改写原文

标题、围栏、列表、表格都**原样留在片段内容里**，切块器只是记下它们的边界。
把标题从正文里剥掉（"规范化"）会让片段内容不再是原文的切片，
引用立刻失去意义 —— 而它看起来更整洁，所以特别容易发生。

## 切分的两层

1. **块（block）**：标题行、空行分隔的段落（列表与表格天然落在其中）、
   整个围栏代码块。块是**结构上不可再分的最小单位**。
2. **片段（chunk）**：同一标题路径下的若干连续块。

只有"超过 `max_chars` 的片段"才继续切：优先在**段落边界**切，
段落本身超长时退到**行边界**，单行仍超长时才硬切。
重叠（`overlap_chars`）只在切开的相邻片段之间出现，且永不跨越标题路径。

## 围栏代码块按整块处理

代码块的中间一行往往看起来像标题（`# 这是注释`）。逐行扫描会把它们当成
真标题，于是后续所有片段的 `heading_path` 全错 —— 而错的是"路径"，
检索加权与展示缩进都会跟着错，但内容看起来完全正常。
因此围栏状态必须在扫描前建立，而且**开围栏的长度必须存下来**：
四反引号里包三反引号是合法的 Markdown 示例写法，只比字符不比长度会让
内部那三个反引号"提前闭合"外层围栏，代码里的 `#` 随即变成标题（R4-07）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.core.clock import Clock, SystemClock
from app.core.ids import new_id
from app.knowledge.models import CHUNK_PARSER_VERSION, SourceDocument, StoredChunk

#: 默认片段上限（字符）。超过它的片段才会被切开。
DEFAULT_MAX_CHARS = 4000

#: 默认重叠上限（字符）。只在被切开的相邻片段之间出现。
DEFAULT_OVERLAP_CHARS = 200

#: ATX 标题：1-6 个 `#`，后接空白（或行尾）。7 个 `#` 不是标题。
_ATX_RE = re.compile(r"^(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")

#: 标题末尾的闭合井号序列（CommonMark 允许 `## 标题 ##`）。
_ATX_CLOSING_RE = re.compile(r"[ \t]+#+[ \t]*$")

#: 围栏起始：**任意缩进**，然后是三个及以上的反引号或波浪号。
#:
#: ⚠️ 这里刻意比 CommonMark 宽松（它只允许最多三个前导空格，四个空格算缩进
#: 代码块）。取舍写清楚：放宽的收益是"缩进代码里的 `#` 不会被当成标题"
#: （这正是本模块最怕的错——`heading_path` 是排序权重输入，错了看不出来），
#: 代价是把某些缩进代码块整块当成围栏（于是它们不被检索加权）。
#: 两边的错不对称：前者静默污染后续所有片段，后者只是少算一点权重。
#:
#: 用 `line.text` 而不是 `line.stripped` 匹配：前者保留"这一行到底长什么样"，
#: 后续若要收紧规则（比如恢复 CommonMark 的三格上限）不必先改匹配口径。
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")

#: 围栏结束：同样任意缩进、同种字符、**长度不短于开围栏**，其后只有空白。
_FENCE_CLOSE_RE = re.compile(r"^\s*([`~]{3,})[ \t]*$")


@dataclass(frozen=True)
class _Fence:
    """一个代码围栏的开头。**长度必须存下来**（R4-07）。

    CommonMark 规定：闭围栏的字符数不得少于开围栏。只比字符不比长度时，
    四反引号围栏会被内部的三反引号提前闭合 —— 而"四反引号里包三反引号"
    正是**展示 Markdown 语法示例的标准写法**，于是示例里代码的 `#`
    会被当成真标题，污染其后所有片段的 `heading_path`（也就是排序权重输入）。
    """

    char: str
    length: int


@dataclass(frozen=True)
class _Line:
    """一行原文的偏移与内容（不含行尾换行符）。"""

    start: int
    end: int
    text: str

    @property
    def is_blank(self) -> bool:
        return self.text.strip() == ""

    @property
    def stripped(self) -> str:
        return self.text.strip()


@dataclass(frozen=True)
class _Unit:
    """一个结构块。多个连续块构成一个片段，除非它超长。"""

    start: int
    end: int
    heading_path: tuple[str, ...]
    #: 这一块是不是标题行本身。只有标题、没有正文的节不产出片段，
    #: 判据必须是"这块是标题"，而不是"这块很短"（短段落一样有内容）。
    is_heading: bool = False


def _split_lines(text: str) -> list[_Line]:
    """按行切分并保留**原文偏移**。

    用 `splitlines(keepends=True)` 累加宽度，而不是按 `\\n` 手工切：
    后者遇到 `\\r\\n` 会把回车算进内容长度，于是所有 span 整体偏移一个字符 ——
    而偏移错了的片段照样能构造（长度对得上），只是引的是错的地方。
    """
    lines: list[_Line] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        # 只剥行尾换行符，**不剥尾随空格**：尾随空格是原文的一部分。
        content = raw.rstrip("\r\n")
        lines.append(_Line(start=offset, end=offset + len(content), text=content))
        offset += len(raw)
    return lines


def _fence_open(line: _Line) -> _Fence | None:
    """这一行是不是围栏开头。

    反引号围栏的 info string **不允许含反引号**（CommonMark）：`` ```a`b ``
    不是围栏开头。少了这条，`` ``` `` 后面跟一段含反引号的行会被当成围栏，
    于是接下来的正文全被当成代码。
    """
    match = _FENCE_OPEN_RE.match(line.text)
    if match is None:
        return None
    marker, info = match.group(1), match.group(2)
    if marker[0] == "`" and "`" in info:
        return None
    return _Fence(char=marker[0], length=len(marker))


def _closes_fence(line: _Line, fence: _Fence) -> bool:
    """这一行能不能闭合 `fence`。

    三个条件缺一不可：**同种字符**、**长度不短于开围栏**、其后只有空白。
    波浪号围栏不会被反引号闭合（反之亦然）—— 只比"有没有三个同字符"
    会让两种围栏互相闭合，而混用是真实文本里常见的写法。
    """
    match = _FENCE_CLOSE_RE.match(line.text)
    if match is None:
        return False
    marker = match.group(1)
    return marker[0] == fence.char and len(marker) >= fence.length


def _heading(line: _Line) -> tuple[int, str] | None:
    """识别 ATX 标题，返回 (层级, 标题文本)。

    标题文本为空（单独一行 `#`）时**不当作标题**：片段契约不允许空标题，
    而"给它编一个名字"会让 `heading_path` 里出现原文里没有的字。
    这一行于是退回普通段落，原样进入片段内容。
    """
    match = _ATX_RE.match(line.stripped)
    if match is None:
        return None
    title = _ATX_CLOSING_RE.sub("", match.group(2) or "").strip()
    if not title:
        return None
    return len(match.group(1)), title


def _normalize_units(lines: list[_Line]) -> list[_Unit]:
    """把行序列归并成结构块（标题行 / 段落 / 整块围栏代码）—— **Markdown 路径**。"""
    units: list[_Unit] = []
    stack: list[tuple[int, str]] = []
    pending: list[_Line] = []
    fence: _Fence | None = None
    fenced: list[_Line] = []

    def flush_pending() -> None:
        if pending:
            path = tuple(title for _level, title in stack)
            units.append(_Unit(pending[0].start, pending[-1].end, path))
            pending.clear()

    for line in lines:
        if fence is not None:
            fenced.append(line)
            if _closes_fence(line, fence):
                path = tuple(title for _level, title in stack)
                units.append(_Unit(fenced[0].start, fenced[-1].end, path))
                fenced.clear()
                fence = None
            continue

        opening = _fence_open(line)
        if opening is not None:
            flush_pending()
            fence = opening
            fenced = [line]
            continue

        if line.is_blank:
            flush_pending()
            continue

        heading = _heading(line)
        if heading is not None:
            flush_pending()
            level, title = heading
            # 标题栈：弹出所有同级或更深层的标题，再压入当前标题。
            # 层级跳级（`# A` 之后直接 `### C`）不补空节点 —— 路径只记录
            # 真实存在的标题，`heading_level` 因此等于路径深度。
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            units.append(
                _Unit(
                    line.start,
                    line.end,
                    tuple(t for _l, t in stack),
                    is_heading=True,
                )
            )
            continue

        pending.append(line)

    # 文件在围栏内结束：未闭合的围栏整块算作一个代码块，
    # 而不是把里面的内容当成标题/段落 —— 未闭合只是语法错误，不是语义翻转。
    if fence is not None and fenced:
        path = tuple(title for _level, title in stack)
        units.append(_Unit(fenced[0].start, fenced[-1].end, path))
    flush_pending()
    return units


def _normalize_plain_units(lines: list[_Line]) -> list[_Unit]:
    """纯文本的行序列 → 结构块。**只有段落一种块**（R4-06）。

    刻意不复用 `_normalize_units`，也不给它加开关：那条路径里"标题/围栏"
    是语法，纯文本里同样的字符只是文字。用一个布尔参数分叉会让两条路径
    共享一段读起来像"同一个算法"的代码，而它们的**语义完全不同** ——
    将来任何一处改动都要重新论证"对另一条路径还成立吗"。

    纯文本没有层级可言，因此 `heading_path` 恒为空元组（与
    `test_plain_text_has_empty_heading_path` 的既有断言一致）。
    段落之间由空行分隔：空行是唯一的结构信号，而它只影响"在哪里切"，
    不影响内容（块边界之后仍按行偏移取切片）。
    """
    units: list[_Unit] = []
    pending: list[_Line] = []

    def flush_pending() -> None:
        if pending:
            units.append(_Unit(pending[0].start, pending[-1].end, ()))
            pending.clear()

    for line in lines:
        if line.is_blank:
            flush_pending()
            continue
        pending.append(line)
    flush_pending()
    return units


def _groups(units: list[_Unit]) -> list[list[_Unit]]:
    """按标题路径分组：同一路径的连续块属于同一节。

    首个标题之前的内容也自成一节（路径为空）。
    """
    groups: list[list[_Unit]] = []
    for unit in units:
        if not groups or groups[-1][0].heading_path != unit.heading_path:
            groups.append([unit])
        else:
            groups[-1].append(unit)
    return groups


def _is_heading_only(group: list[_Unit]) -> bool:
    """只有标题、没有正文的节不产出片段。

    它没有任何可检索的内容 —— 硬要产出一个只含 `## 回滚` 的片段，
    检索结果里就会出现"命中但什么也没说"的项，看起来像系统坏了。
    """
    return len(group) == 1 and group[0].is_heading


def _largest_at_most(candidates: list[int], pos: int, limit: int) -> int | None:
    """在 `(pos, limit]` 里取最大的候选偏移。二分避免 O(n²)。"""
    low, high = 0, len(candidates) - 1
    best: int | None = None
    while low <= high:
        mid = (low + high) // 2
        value = candidates[mid]
        if value <= pos:
            low = mid + 1
        elif value <= limit:
            best = value
            low = mid + 1
        else:
            high = mid - 1
    return best


class DocumentProcessor:
    """把不可变原文切成不可变片段。

    构造参数全部可注入：`clock` 与 `id_factory` 让测试能拿到确定的结果，
    也让"片段 id 由谁生成"这件事保持在服务层（端口约定：写入方法接收已生成的 id）。
    """

    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
        clock: Clock | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_chars <= 0:
            raise ValueError(f"max_chars 必须为正整数，收到 {max_chars}")
        if overlap_chars < 0:
            raise ValueError(f"overlap_chars 不能为负，收到 {overlap_chars}")
        if overlap_chars >= max_chars:
            # 重叠不小于上限时，相邻片段会互相包含，切分永远收敛不了。
            raise ValueError(
                f"overlap_chars（{overlap_chars}）必须小于 max_chars（{max_chars}）"
            )
        self._max_chars = max_chars
        self._overlap_chars = overlap_chars
        self._clock = clock or SystemClock()
        self._new_id = id_factory or (lambda: new_id("chk"))

    @property
    def parser_version(self) -> str:
        """片段契约里记录的版本。切块规则一变就必须改它并重跑检索夹具。"""
        return CHUNK_PARSER_VERSION

    def parse(self, document: SourceDocument) -> tuple[StoredChunk, ...]:
        """切片。

        输入是**已验证的** `SourceDocument`：媒体类型、语言、UTF-8 合法性
        都在上游边界判定（`knowledge.models` 与 HTTP 请求校验）。
        切块器不猜媒体类型、不猜语言、不做字符集探测 —— 那些都会写出
        原文里没有的事实。**未知媒体类型直接拒绝**，而不是当成纯文本放过去。

        媒体类型决定走哪条解析路径（见模块 docstring）：`text/plain` 不识别
        标题与围栏，`#` 只是普通文字。
        """
        text = document.content
        lines = _split_lines(text)
        if document.media_type == "text/markdown":
            units = _normalize_units(lines)
        elif document.media_type == "text/plain":
            units = _normalize_plain_units(lines)
        else:  # pragma: no cover - `SourceDocument` 已经拒绝过它
            raise ValueError(
                f"未知的 media_type：{document.media_type!r}；"
                "切块器不猜语义，未知类型一律拒绝"
            )
        line_ends = [line.end for line in lines if line.end > line.start]

        created_at = self._clock.now()
        pieces: list[tuple[int, int, tuple[str, ...]]] = []
        for group in _groups(units):
            if _is_heading_only(group):
                continue
            span = (group[0].start, group[-1].end)
            boundaries = [unit.end for unit in group]
            for start, end in self._split_span(span, boundaries, line_ends):
                if end > start:
                    pieces.append((start, end, group[0].heading_path))

        return tuple(
            StoredChunk(
                chunk_id=self._new_id(),
                tenant_id=document.tenant_id,
                project_id=document.project_id,
                source_id=document.source_id,
                document_id=document.document_id,
                chunk_index=index,
                heading_path=heading_path,
                heading_level=len(heading_path),
                span_start=start,
                span_end=end,
                content=text[start:end],
                parser_version=self.parser_version,
                created_at=created_at,
            )
            for index, (start, end, heading_path) in enumerate(pieces)
        )

    def _split_span(
        self, span: tuple[int, int], boundaries: list[int], line_ends: list[int]
    ) -> list[tuple[int, int]]:
        """把一个节切成若干片段。

        只在**超长**时切（`end - start > max_chars`）：短文按原样整段保留，
        这样"最小可引用单元"才与作者写的段落一致，而不是被人为切碎。
        """
        section_start, section_end = span
        pieces: list[tuple[int, int]] = []
        pos = section_start
        while section_end - pos > self._max_chars:
            limit = pos + self._max_chars
            # 优先级：段落边界 > 行边界 > 硬切。
            cut = _largest_at_most(boundaries, pos, limit)
            if cut is None:
                cut = _largest_at_most(line_ends, pos, limit)
            if cut is None:
                # 单行本身就超长 —— 只能硬切。这不是"降级"，是唯一可行的选择，
                # 而它至少保证"每个片段不超过上限"这条可验证的性质。
                cut = limit
            pieces.append((pos, cut))
            # 重叠不越过本节起点：跨节的"上下文"是另一节的内容，
            # 带进本片段会让引用指向别的标题下。
            pos = max(cut - self._overlap_chars, pos + 1)
        if section_end > pos:
            pieces.append((pos, section_end))
        return pieces
