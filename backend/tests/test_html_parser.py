"""HTML normalization preserves useful document structure without executing markup."""

from __future__ import annotations

import pytest
from app.knowledge.html_parser import html_to_markdown


def test_html_parser_preserves_headings_paragraphs_and_code() -> None:
    source = """
    <article>
      <nav>站点导航</nav>
      <h1>事务处理</h1>
      <p>保持 <em>原子性</em> &amp; 隔离。</p>
      <h2>示例</h2>
      <pre><code>if valid:\n    commit()</code></pre>
      <script>不应进入资料</script>
      <style>.secret { display: none }</style>
    </article>
    """

    parsed = html_to_markdown(source)

    assert "# 事务处理" in parsed
    assert "保持 原子性 & 隔离。" in parsed
    assert "## 示例" in parsed
    assert "```\nif valid:\n    commit()\n```" in parsed
    assert "站点导航" not in parsed
    assert "不应进入资料" not in parsed
    assert ".secret" not in parsed


def test_html_parser_rejects_documents_without_visible_content() -> None:
    with pytest.raises(ValueError, match="没有可用正文"):
        html_to_markdown("<nav>目录</nav><script>hidden</script><style>hidden</style>")
