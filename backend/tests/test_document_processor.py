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


def document(text: str, *, title: str = "事务讲义", media_type: str = "text/markdown"):
    return SourceDocument(
        document_id="doc_test",
        tenant_id="tenant_test",
        project_id="proj_test",
        source_id="src_test",
        version=1,
        document_title=title,
        content=text,
        media_type=media_type,
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


def assert_no_content_lost(text: str, chunks) -> None:
    """**非空白内容一个字符都没丢**。

    与 `assert_spans_round_trip` 互补：那条只保证"每个片段都是切片"，
    空集的片段集合能完美通过它 —— 而"整篇被丢弃、产出零片段"正是
    R4-06 的失败模式（`# plain heading` 被当成无正文标题），
    界面上的表现是"上传成功但搜不到"。
    """
    covered: set[int] = set()
    for chunk in chunks:
        covered.update(range(chunk.span_start, chunk.span_end))
    for index, char in enumerate(text):
        if char.isspace():
            continue
        assert index in covered, f"原文第 {index} 个字符 {char!r} 不在任何片段里"


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


# ------------------------------------------------- 围栏长度与缩进（R4-07）


@pytest.mark.invariant
def test_a_shorter_fence_does_not_close_a_longer_one():
    """四反引号里的三反引号**不闭合**外层围栏。

    "四反引号包三反引号"是展示 Markdown 语法示例的标准写法。只比字符不比长度时，
    内部那三个反引号会提前闭合外层围栏 —— 于是示例里的 `# 井号` 变成真标题，
    污染其后所有片段的 `heading_path`（而那是排序权重的输入，错了看不出来）。
    """
    text = "````\n```\n# 这是示例里的井号\n```\n````\n\n正文段落。\n"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert_no_content_lost(text, chunks)
    # 围栏与正文的路径都是空 → 它们并入**同一节**、产出**一个**片段
    # （分组规则：同一路径的连续块属于同一节）。关键是路径里没有那个 `#`。
    assert len(chunks) == 1
    assert chunks[0].heading_path == ()
    assert "# 这是示例里的井号" in chunks[0].content


@pytest.mark.invariant
def test_a_fence_is_closed_only_by_its_own_character():
    """波浪号围栏不会被反引号围栏闭合（反之亦然）。

    两种围栏混用是真实文本里常见的写法（中文教程尤其多）。
    只判"有没有三个同种字符"会让它们互相闭合，而"闭合"意味着
    后面的正文被当成代码、或代码里的 `#` 被当成标题 —— 两个方向都会错。
    """
    text = "~~~\n```\n# 井号在波浪号围栏里\n~~~\n\n正文。\n"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert_no_content_lost(text, chunks)
    assert len(chunks) == 1 and chunks[0].heading_path == ()
    assert "# 井号在波浪号围栏里" in chunks[0].content

    # 反方向：反引号围栏里的波浪号同样不闭合它。
    mirrored = "```\n~~~\n# 井号在反引号围栏里\n```\n\n正文。\n"
    mirrored_chunks = DocumentProcessor().parse(document(mirrored))
    assert_spans_round_trip(mirrored, mirrored_chunks)
    assert len(mirrored_chunks) == 1 and mirrored_chunks[0].heading_path == ()


@pytest.mark.invariant
def test_an_indented_fence_still_protects_its_content():
    """缩进的围栏照样算围栏 —— 这里**刻意比 CommonMark 宽松**。

    CommonMark 只允许最多三个前导空格（四个算缩进代码块）。但两侧的错
    不对称：把缩进代码块当成围栏，代价是"少算一点检索权重"；
    把它当成正文，代价是代码里的 `#` 变成标题、**静默污染其后所有片段的
    `heading_path`**。所以宁可宽松。

    这条断言同时也是"缩进规则"的落点：无论缩进几格，围栏内容都不许被当成标题。
    """
    for indent in ("   ", "    ", "\t"):
        text = f"{indent}```\n{indent}# 井号在缩进的围栏里\n{indent}```\n\n正文。\n"
        chunks = DocumentProcessor().parse(document(text))

        assert_spans_round_trip(text, chunks)
        assert_no_content_lost(text, chunks)
        assert len(chunks) == 1 and chunks[0].heading_path == (), (
            f"缩进 {indent!r} 的围栏内容被当成了标题"
        )
        assert "# 井号在缩进的围栏里" in chunks[0].content


@pytest.mark.invariant
def test_a_backtick_fence_whose_info_string_contains_a_backtick_is_not_a_fence():
    """反引号围栏的 info string 不允许含反引号（CommonMark）。

    少了这条，`` ```a`b `` 会被当成围栏开头，于是紧随其后的正文全被当成代码 ——
    内容不丢，但正文再也不会被识别出标题，而"为什么这份资料的标题都没了"
    从任何报错里都看不出来。
    """
    text = "```a`b\n# 这一行是真标题\n\n正文。\n"
    chunks = DocumentProcessor().parse(document(text))

    assert_spans_round_trip(text, chunks)
    assert_no_content_lost(text, chunks)
    assert ("这一行是真标题",) in [chunk.heading_path for chunk in chunks]


# --------------------------------------------- 媒体类型决定解析路径（R4-06）


@pytest.mark.invariant
def test_plain_text_keeps_hashes_and_fences_as_literal_text():
    """`text/plain` 里的 `#` 与围栏都只是文字，整篇不许被丢掉。

    R4-06 的失败模式：`# plain heading` 是一份**完全合法**的纯文本，
    按 Markdown 规则却成了"只有标题、没有正文"的节 → 产出**零片段**。
    用户看到的是"上传成功但搜不到"，而库里确实什么都没有。
    """
    text = "# plain heading"
    chunks = DocumentProcessor().parse(document(text, media_type="text/plain"))

    assert_spans_round_trip(text, chunks)
    assert_no_content_lost(text, chunks)
    assert len(chunks) == 1
    assert chunks[0].content == text
    assert chunks[0].heading_path == ()
    assert chunks[0].heading_level == 0


@pytest.mark.invariant
@pytest.mark.parametrize(
    "text",
    [
        "# plain heading",
        "```\n# 围栏在纯文本里也只是文字\n```",
        "- 列表项\n- 另一项",
        "第一段。\n\n第二段。",
        "   \n\n前导空白之后有内容。",
        "| 表头 |\n|---|\n| 行 |",
    ],
)
def test_plain_text_never_loses_content(text):
    """纯文本的更强性质：**非空内容必然产出至少一个片段**。

    （Markdown 允许"只有标题"的文档产出零片段 —— 它确实没有可检索的正文。
    纯文本没有"标题"这个概念，所以零片段只可能是缺陷。）
    """
    chunks = DocumentProcessor().parse(document(text, media_type="text/plain"))

    assert chunks, "纯文本被整篇丢弃了"
    assert_spans_round_trip(text, chunks)
    assert_no_content_lost(text, chunks)
    assert all(chunk.heading_path == () for chunk in chunks)


@pytest.mark.invariant
def test_the_media_type_decides_how_the_same_text_is_parsed():
    """同一段文本，两种媒体类型给出**不同**的结果 —— 这是路径分叉的判据。

    只测"纯文本能出片段"是不够的：如果两种类型走同一条路径，
    上面那些纯文本用例会在 Markdown 路径下**也**通过（只要内容不是
    "# 开头"），于是"按类型分派"这件事永远没被验证过。
    这里用同一段文本正面对比两侧的差异。
    """
    text = "# 只有标题\n"
    markdown = DocumentProcessor().parse(document(text, media_type="text/markdown"))
    plain = DocumentProcessor().parse(document(text, media_type="text/plain"))

    assert markdown == (), "Markdown 的'只有标题'节按设计不产出片段"
    assert len(plain) == 1 and plain[0].content == "# 只有标题"

    # 反方向：一段含围栏的 Markdown，纯文本路径不认围栏，但两者都不许丢内容。
    fenced = "# 标题\n\n```\n# 注释\n```\n"
    assert_no_content_lost(
        fenced, DocumentProcessor().parse(document(fenced, media_type="text/markdown"))
    )
    assert_no_content_lost(
        fenced, DocumentProcessor().parse(document(fenced, media_type="text/plain"))
    )


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
