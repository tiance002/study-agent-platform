"""Deterministic, non-rendering HTML to Markdown extraction."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from app.knowledge.models import MAX_DOCUMENT_BYTES, require_document_content

HTML_PARSER_VERSION = "html-to-markdown/v1"
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_IGNORED_TAGS = frozenset(
    {
        "aside",
        "button",
        "footer",
        "form",
        "head",
        "iframe",
        "nav",
        "noscript",
        "object",
        "script",
        "style",
        "svg",
        "template",
    }
)
_HEADING_TAGS = {f"h{level}": level for level in range(1, 7)}
_BLOCK_TAGS = frozenset(
    {"article", "blockquote", "div", "li", "main", "ol", "p", "section", "table", "tr", "ul"}
)
_NOISE_CLASSES = frozenset(
    {"advertisement", "banner", "breadcrumb", "breadcrumbs", "cookie", "menu", "navigation", "sidebar"}
)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, bool]] = []
        self.current: list[str] = []
        self.prefix = ""
        self.in_code = False
        self.ignored: list[str] = []

    def _begin(self, prefix: str = "", *, code: bool = False) -> None:
        self._finish()
        self.prefix = prefix
        self.in_code = code

    def _finish(self) -> None:
        raw = "".join(self.current)
        self.current.clear()
        if self.in_code:
            text = raw.strip("\r\n")
            if text.strip():
                fence = "`" * max(3, max((len(match) for match in re.findall(r"`+", text)), default=0) + 1)
                self.blocks.append((f"{fence}\n{text}\n{fence}", True))
        else:
            lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
            text = "\n".join(line for line in lines if line)
            if text:
                self.blocks.append((self.prefix + text, False))
        self.prefix = ""
        self.in_code = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.ignored:
            if tag not in _VOID_TAGS:
                self.ignored.append(tag)
            return

        attributes = {key.casefold(): (value or "") for key, value in attrs}
        classes = set(attributes.get("class", "").casefold().split())
        hidden = (
            "hidden" in attributes
            or attributes.get("aria-hidden", "").casefold() == "true"
            or bool(classes & _NOISE_CLASSES)
        )
        if tag in _IGNORED_TAGS or hidden:
            if tag not in _VOID_TAGS:
                self.ignored.append(tag)
            return

        if tag in _HEADING_TAGS:
            self._begin("#" * _HEADING_TAGS[tag] + " ")
        elif tag == "pre":
            self._begin(code=True)
        elif tag in _BLOCK_TAGS:
            self._begin("- " if tag == "li" else "> " if tag == "blockquote" else "")
        elif tag == "br":
            self.current.append("\n")
        elif tag in {"td", "th"} and self.current and "".join(self.current).strip():
            self.current.append(" | ")
        elif tag == "hr":
            self._finish()
            self.blocks.append(("---", False))

    def handle_endtag(self, tag: str) -> None:
        if self.ignored:
            for index in range(len(self.ignored) - 1, -1, -1):
                if self.ignored[index] == tag:
                    del self.ignored[index:]
                    break
            return
        if tag in _BLOCK_TAGS or tag in _HEADING_TAGS or tag == "pre":
            self._finish()

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.current.append(data if self.in_code else re.sub(r"\s+", " ", data))


def html_to_markdown(source: str) -> str:
    """Extract visible headings, paragraphs, lists, tables, and code blocks.

    The result is the canonical citation text. The original HTML is stored
    separately as an immutable acquisition artifact.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("HTML 来源没有可用正文")
    try:
        source_size = len(source.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("HTML 来源不是合法 UTF-8") from exc
    if source_size > MAX_DOCUMENT_BYTES:
        raise ValueError("HTML 来源超过大小上限")

    parser = _Extractor()
    parser.feed(source)
    parser.close()
    parser._finish()
    document = "\n\n".join(block for block, _ in parser.blocks).strip()
    if not document:
        raise ValueError("HTML 来源没有可用正文")
    return require_document_content(document)
