"""确定性切块器的边界测试。

这个文件守的是一件事：**片段内容永远是原文的精确切片**。
其余的行为（切在哪、重叠多少、路径怎么拼）都可以被讨论，
唯独这条不能有余地 —— 它一旦破了，"引用可核验"就是一句空话，
而界面上的引用看起来完全正常（内容读得通，只是不是那个位置）。

因此每个用例开头都是同一条断言：
    `text[chunk.span_start:chunk.span_end] == chunk.content`
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import pairwise

import pytest
from app.knowledge.models import MAX_DOCUMENT_BYTES, SourceDocument
from app.knowledge.processor import DocumentProcessor


def document(text: str, *, title: str = "事务讲义") -> SourceDocument:
    return SourceDocument(
        document_id="doc_test",
        tenant_id="tenant_test",
        project_id="proj_test",
        source_id="src_test",
        version=1,
        document_title=title,
        content=text,
        media_type="text/markdown",
        language="zh",
        observed_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )


def assert_spans_round_trip(text: str, chunks) -> None:
    """每个片段都必须能从原文精确回读，且指纹与内容一致。"""
    for chunk in chunks:
        assert text[chunk.span_start : chunk.span_end] == chunk.content, (
            f"片段 {chunk.chunk_index} 的 span 与内容不一致"
        )
        assert chunk.content_hash.startswith("sha256:")
        assert chunk.span_end - chunk.span_start == len(chunk.content)
        assert chunk.heading_level == len(chunk.heading_path)


@pytest.mark.invariant
def test_markdown_chunks_preserve_exact_source_spans():
    text = "# 事务\n\n提交成功。\n\n## 回滚\n\n失败时回滚。"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert len(chunks) == 2
    assert chunks[0].heading_path == ("事务",)
    assert chunks[1].heading_path == ("事务", "回滚")
    assert chunks[1].content == "## 回滚\n\n失败时回滚。"


@pytest.mark.invariant
def test_oversized_section_splits_within_limit_and_keeps_index_contiguous():
    paragraphs = [f"第{i}段讲的是事务的隔离级别与提交顺序。" * 6 for i in range(12)]
    text = "# 事务\n\n" + "\n\n".join(paragraphs)
    processor = DocumentProcessor(max_chars=400, overlap_chars=40)
    chunks = processor.parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert all(len(chunk.content) <= 400 for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    # 切点落在行边界上：每段结束处原文是换行，说明没有从句子中间切断。
    for chunk in chunks[:-1]:
        assert text[chunk.span_end] == "\n"


@pytest.mark.invariant
def test_overlap_is_bounded_and_never_crosses_sections():
    paragraphs = [f"第{i}段：回滚要写清楚边界条件。" * 8 for i in range(10)]
    text = "# 段一\n\n" + "\n\n".join(paragraphs) + "\n\n# 段二\n\n另一节的内容。"
    processor = DocumentProcessor(max_chars=300, overlap_chars=50)
    chunks = processor.parse(document(text))

    assert_spans_round_trip(text, chunks)
    section_one = [c for c in chunks if c.heading_path == ("段一",)]
    assert len(section_one) > 1, "这个用例要求第一节真的被切开了"
    for previous, following in pairwise(section_one):
        overlap = previous.span_end - following.span_start
        assert 0 < overlap <= 50, f"重叠必须落在 (0, 50] 内，实际 {overlap}"
    # 重叠永不跨越节边界：第二节的第一个片段不包含第一节的尾巴。
    section_two = [c for c in chunks if c.heading_path == ("段二",)]
    assert section_two[0].content.startswith("# 段二")


@pytest.mark.invariant
def test_fence_content_is_never_read_as_a_heading():
    """围栏代码里的 `# 注释` 不是标题。

    逐行扫描会把它们当成真标题，于是后续所有片段的 `heading_path` 全错 ——
    而错的是路径，内容看起来完全正常，检索加权与展示缩进却会跟着错。
    """
    text = (
        "# 事务\n\n"
        "```bash\n"
        "# 这是 shell 注释，不是 Markdown 标题\n"
        "psql -c 'BEGIN'\n"
        "```\n\n"
        "## 回滚\n\n"
        "失败时回滚。\n"
    )
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert [chunk.heading_path for chunk in chunks] == [("事务",), ("事务", "回滚")]
    assert "这是 shell 注释" in chunks[0].content


@pytest.mark.invariant
def test_unclosed_fence_absorbs_rest_of_document():
    """未闭合的围栏整块处理，而不是把里面的内容当成标题/段落。

    未闭合只是语法错误，不该造成语义翻转 —— 否则一段截断的粘贴内容
    会让整篇文档的标题路径错位。
    """
    text = "# 事务\n\n```python\nprint('未闭合')\n# 仍然在代码里\n"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert len(chunks) == 1
    assert chunks[0].heading_path == ("事务",)
    assert chunks[0].content.endswith("# 仍然在代码里")


@pytest.mark.invariant
def test_list_and_table_are_kept_whole():
    """列表与表格是"空行分隔的连续行"，不会被逐行拆散。

    逐行拆开会让 `- 提交` 变成一个独立片段：检索命中"提交"时引回来的
    只有两个字，而它在原文里是与上下文一起读的。

    这里用"**只有一个片段**"来证明它没被拆开 —— 若按行切，片段数必然是 3。
    """
    text = (
        "# 命令\n\n"
        "- 开启事务\n"
        "- 提交\n"
        "- 回滚\n\n"
        "| 隔离级别 | 现象 |\n"
        "|---|---|\n"
        "| 读已提交 | 不可重复读 |\n"
    )
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    # 标题 / 列表 / 表格同属一个标题路径（`# 命令`），因此只有一节、一个片段。
    assert len(chunks) == 1
    content = chunks[0].content
    assert "- 开启事务\n- 提交\n- 回滚" in content
    assert "| 隔离级别 | 现象 |\n|---|---|\n| 读已提交 | 不可重复读 |" in content


@pytest.mark.invariant
def test_heading_without_body_produces_no_chunk():
    """只有标题、没有正文的节不产出片段 —— 否则检索里会出现
    "命中但什么也没说"的项，看起来像系统坏了。"""
    text = "# 事务\n\n## 空节\n\n## 回滚\n\n失败时回滚。"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert [chunk.heading_path for chunk in chunks] == [("事务", "回滚")]
    assert "空节" not in "".join(chunk.content for chunk in chunks)


@pytest.mark.invariant
def test_skipped_heading_level_does_not_fabricate_empty_nodes():
    """层级跳级不补空节点：路径只记录真实存在的标题。

    `heading_level` 因此等于**路径深度**，而不是 Markdown 里 `#` 的个数。
    这条定义必须唯一 —— 若"层级"有两种解释，检索加权与展示缩进会各按一种来。
    """
    text = "# 顶层\n\n### 跳到三级\n\n内容。"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert chunks[0].heading_path == ("顶层", "跳到三级")
    assert chunks[0].heading_level == 2


@pytest.mark.invariant
def test_plain_text_has_empty_heading_path():
    text = "第一段。\n\n第二段。"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert all(chunk.heading_path == () for chunk in chunks)
    assert all(chunk.heading_level == 0 for chunk in chunks)


@pytest.mark.invariant
def test_atx_closing_hashes_are_not_part_of_the_title():
    text = "# 事务 ##\n\n内容。"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert chunks[0].heading_path == ("事务",)


@pytest.mark.invariant
def test_bare_hash_line_is_not_a_heading():
    """单独一行 `#` 没有标题文本 —— 不编名字，退回普通段落。"""
    text = "#\n\n内容。"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert chunks[0].heading_path == ()
    assert chunks[0].content.startswith("#")


@pytest.mark.invariant
def test_single_overlong_line_is_hard_cut_within_limit():
    """单行超长时只能硬切。至少"每个片段不超过上限"这条性质仍成立。

    这里**不能**断言"所有片段拼起来等于原文"：相邻片段带着重叠，
    拼接必然出现重复内容，而重复不是缺陷。被切开时要守的性质是
    **覆盖性** —— 从第 0 个字符到文末没有空洞（也不许边界倒挂）。
    """
    text = "只有一行" + "很长的内容" * 200
    processor = DocumentProcessor(max_chars=100, overlap_chars=10)
    chunks = processor.parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert all(len(chunk.content) <= 100 for chunk in chunks)
    assert chunks[0].span_start == 0
    assert chunks[-1].span_end == len(text)
    for previous, following in pairwise(chunks):
        overlap = previous.span_end - following.span_start
        assert 0 <= overlap <= 10, f"重叠必须落在 [0, 10] 内，实际 {overlap}"


@pytest.mark.invariant
def test_offsets_are_character_based_not_byte_based():
    """偏移按**字符**计。按字节算会让每个中文片段都从半个字开始。"""
    text = "# 事务\n\n提交成功，回滚失败。"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert chunks[0].span_start == 0
    assert chunks[0].span_end == len(text.rstrip("\n").rstrip())


@pytest.mark.invariant
@pytest.mark.parametrize("content", ["", "   ", "\n\n", "   \n\n  \n"])
def test_blank_document_is_rejected_at_contract_level(content: str):
    """空文档与纯空白文档在**契约层**就无法表达。

    所以"空白文档产出零片段"不是切块器里的一个分支，而是"这个输入不存在" ——
    切块器永远收不到它。这类性质写在类型上比写在分支里可靠得多：
    分支会被后来的人当成冗余删掉，而它删掉的是一条安全边界。
    """
    with pytest.raises(ValueError, match="content"):
        document(content)


@pytest.mark.invariant
def test_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        DocumentProcessor(max_chars=0)
    with pytest.raises(ValueError):
        DocumentProcessor(overlap_chars=-1)
    with pytest.raises(ValueError):
        DocumentProcessor(max_chars=100, overlap_chars=100)


@pytest.mark.invariant
def test_lone_surrogate_is_rejected_before_the_processor():
    """JSON 允许 `\\ud800` 这样的孤立代理项转义，解出来是 Python 能持有、
    却编码不成 UTF-8 的字符串。

    不在这里拦，`encode` 抛的是 `UnicodeEncodeError` —— 它不是 ValueError，
    pydantic 不会翻成 422，请求会以 500 结束。
    """
    with pytest.raises(ValueError):
        document("坏内容\ud800")


@pytest.mark.invariant
def test_oversized_content_is_rejected_by_bytes_not_characters():
    """中文一字三字节：按字符设限时 100 万字符是 3 MiB，
    能过应用层校验却在数据库 CHECK 上炸掉，用户拿到的是 500。"""
    with pytest.raises(ValueError) as excinfo:
        document("中" * (MAX_DOCUMENT_BYTES // 3 + 1))
    assert "字节" in str(excinfo.value)

    # 边界之内必须放行（恰好等于上限）。
    exact = "a" * MAX_DOCUMENT_BYTES
    assert len(document(exact).content) == MAX_DOCUMENT_BYTES


@pytest.mark.invariant
def test_unsupported_media_type_is_rejected():
    with pytest.raises(ValueError) as excinfo:
        SourceDocument(
            document_id="doc", tenant_id="t", project_id="p", source_id="s",
            version=1, document_title="讲义", content="内容",
            media_type="application/pdf", language="zh",
            observed_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        )
    assert "media_type" in str(excinfo.value)
