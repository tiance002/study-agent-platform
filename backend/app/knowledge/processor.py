"""确定性结构切块器。

设计依据：02 号规格 §3（分块）、本轮计划的「不改写证据原文」不变量。

## 一条不可动摇的性质

> 每个片段的内容**就是原文的一个精确切片**：`text[span] == chunk.content`。

这条性质不是在切块器里"小心算偏移"实现的，而是 `StoredChunk` 的类型约束
（`span_end - span_start == len(content)`）。切块器算错一个字符，第一个片段
构造时就炸 —— 而不是等到用户点开引用、发现引的是别处的文字。

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
因此围栏状态必须在扫描前建立。
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

#: 围栏起始：三个及以上的反引号或波浪号。
_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})")


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


def _fence_marker(line: _Line) -> str | None:
    match = _FENCE_OPEN_RE.match(line.stripped)
    return match.group(1)[0] if match else None


def _closes_fence(line: _Line, marker: str) -> bool:
    return re.fullmatch(re.escape(marker) + r"{3,}", line.stripped) is not None


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
    """把行序列归并成结构块（标题行 / 段落 / 整块围栏代码）。"""
    units: list[_Unit] = []
    stack: list[tuple[int, str]] = []
    pending: list[_Line] = []
    fence: str | None = None
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

        marker = _fence_marker(line)
        if marker is not None:
            flush_pending()
            fence = marker
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
        切块器不猜标题、不猜语言、不做字符集探测 —— 那些都会写出原文里没有的事实。
        """
        text = document.content
        lines = _split_lines(text)
        units = _normalize_units(lines)
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
