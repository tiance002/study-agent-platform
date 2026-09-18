"""契约生成机制的自我验证。

审查指出：CI 的契约一致性门当时**必然失败** —— 契约文件仍记录 `source_hash=PENDING`，
而源码已经存在。修复后这里不仅验证「当前能通过」，还要验证
**「机制真的能发现漂移」**：一个永远返回成功的校验器等于没有校验器。
"""

from __future__ import annotations

import pytest

from tools.skills import gen_contracts as gc


@pytest.mark.invariant
@pytest.mark.parametrize("name", sorted(gc.TARGETS))
def test_contracts_are_in_sync_with_sources(name):
    """三份契约的生成区必须与当前源码一致 —— 等价于 CI 的那道门。"""
    assert gc.run_target(gc.TARGETS[name], check=True) == 0


def _tamper_generated_region(path, old: str, new: str) -> None:
    """只在**生成区**内替换文本。

    不能用整文件的 `str.replace`：同一个词在手写区也可能出现
    （例如 `retrieve_chunk` 同时是 `intent_tag` 的文档示例）。
    整文件替换会连手写区一起改，让"验证生成区被改动"的测试变得名不副实 ——
    它测的就成了"文件里任何地方变了"。
    """
    content = path.read_text(encoding="utf-8")
    start = content.index(gc.BEGIN_MARKER)
    end = content.index(gc.END_MARKER)
    region = content[start:end]
    assert old in region, f"生成区里没有 {old!r}，测试前提不成立"
    path.write_text(
        content[:start] + region.replace(old, new) + content[end:], encoding="utf-8"
    )


def _copy_of(name: str, tmp_path) -> tuple[gc.Target, "object"]:
    original = gc.TARGETS[name]
    copy_path = tmp_path / f"{name}.md"
    copy_path.write_text(original.contract_path.read_text(encoding="utf-8"), encoding="utf-8")
    target = gc.Target(
        name=original.name,
        contract_path=copy_path,
        sources=original.sources,
        renderer=original.renderer,
        source_label=original.source_label,
    )
    return target, copy_path


@pytest.mark.invariant
def test_check_detects_hand_edited_generated_block(tmp_path):
    """手工改动生成区必须被发现，否则这道门形同虚设。"""
    target, copy_path = _copy_of("tool-catalog", tmp_path)

    assert gc.run_target(target, check=True) == 0, "复制后应当是一致的"

    _tamper_generated_region(copy_path, "retrieve_chunk", "retrieve_chunk_tampered")
    assert gc.run_target(target, check=True) == 1, "生成区被手改后必须报错"


@pytest.mark.invariant
def test_regeneration_does_not_touch_files_when_nothing_changed(tmp_path):
    """内容没变时不重写文件 —— 否则时间戳噪音会把真正的契约变更淹没。

    修复前的行为是：每次 `--all` 都重写 `generated_at`，于是三个契约文件
    总是同时出现在 diff 里。这类噪音比行尾噪音更隐蔽 ——
    行尾噪音一眼能认出，时间戳噪音看起来像"生成过，应该没问题"。
    """
    target, copy_path = _copy_of("tool-catalog", tmp_path)
    before = copy_path.read_text(encoding="utf-8")
    mtime_before = copy_path.stat().st_mtime_ns

    assert gc.run_target(target, check=False) == 0

    assert copy_path.read_text(encoding="utf-8") == before, "内容未变却改写了文件"
    assert copy_path.stat().st_mtime_ns == mtime_before, "内容未变却触碰了文件"


@pytest.mark.invariant
def test_regeneration_still_repairs_a_hand_edited_block(tmp_path):
    """内容真的变了就必须写入。

    这条是上一条的对照：没有它，"跳过写入"可能退化成"永远不写"，
    而"永远不写"同样能让上一条测试通过 —— 那才是最难发现的坏状态。
    """
    target, copy_path = _copy_of("tool-catalog", tmp_path)

    _tamper_generated_region(copy_path, "retrieve_chunk", "retrieve_chunk_tampered")

    assert gc.run_target(target, check=False) == 0

    restored = copy_path.read_text(encoding="utf-8")
    assert "retrieve_chunk_tampered" not in restored, "被改坏的生成区没有被修复"
    assert gc.run_target(target, check=True) == 0


@pytest.mark.invariant
def test_check_detects_stale_source_label(tmp_path):
    """只改 `Target.source_label` 也必须被发现。

    此前 header 里的 `source=` 既不参与校验、也不参与写入判定。
    后果是「生成视图从哪来」这句说明错了却没人知道 ——
    而且上一轮加的"内容未变化就不写文件"会把这个问题**放大**：
    内容确实没变，于是连重新生成都不会修复旧标签。

    这类缺陷的共同点是：**它不会报错**。只有专门比对才看得见。
    """
    target, copy_path = _copy_of("tool-catalog", tmp_path)

    # 只改来源说明，正文与哈希都不动。
    changed = gc.Target(
        name=target.name,
        contract_path=target.contract_path,
        sources=target.sources,
        renderer=target.renderer,
        source_label="换了一个来源说明",
    )

    assert gc.run_target(changed, check=True) == 1, "--check 必须发现来源说明漂移"

    # 普通生成要真的修好它（不能因为"内容没变"就跳过）。
    assert gc.run_target(changed, check=False) == 0
    assert gc.parse_header(copy_path.read_text(encoding="utf-8"))["source"] == "换了一个来源说明"
    assert gc.run_target(changed, check=True) == 0


@pytest.mark.invariant
def test_header_parsing_tolerates_commas_in_source_label(tmp_path):
    """来源说明里带逗号时也必须解析正确。

    按逗号切分看似够用，但 `source` 是人类可读说明，写成「A、B、C」或带逗号
    完全可能 —— 那会让校验拿到一个被截断的标签，于是**该报的漂移报不出来**。
    这里是防止"防漂移机制自己漂移"。
    """
    target, copy_path = _copy_of("tool-catalog", tmp_path)
    changed = gc.Target(
        name=target.name,
        contract_path=target.contract_path,
        sources=target.sources,
        renderer=target.renderer,
        source_label="注册表导出, 含权限轴与幂等声明",
    )
    assert gc.run_target(changed, check=False) == 0

    header = gc.parse_header(copy_path.read_text(encoding="utf-8"))
    assert header["source"] == "注册表导出, 含权限轴与幂等声明"
    assert header["source_hash"].startswith("sha256:")
    assert header["generated_at"]


@pytest.mark.invariant
def test_check_detects_stale_source_hash(tmp_path):
    """source_hash 被改坏也必须被发现（防止绕过内容比对）。"""
    original = gc.TARGETS["tool-catalog"]
    copy_path = tmp_path / "tool-catalog.md"
    text = original.contract_path.read_text(encoding="utf-8")

    start = text.index(gc.BEGIN_MARKER)
    header_end = text.index("\n", start)
    header = text[start:header_end]
    current_hash = header.split("source_hash=")[1].split(",")[0]
    forged = text[:start] + header.replace(current_hash, "sha256:0000000000") + text[header_end:]
    copy_path.write_text(forged, encoding="utf-8")

    target = gc.Target(
        name=original.name,
        contract_path=copy_path,
        sources=original.sources,
        renderer=original.renderer,
        source_label=original.source_label,
    )
    assert gc.run_target(target, check=True) == 1


@pytest.mark.invariant
def test_source_hash_is_sensitive_to_content(tmp_path):
    """源码内容一变，哈希就变 —— 这是「改了源码必须同步契约」的依据。"""
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    first = gc.source_hash([source])
    source.write_text("value = 2\n", encoding="utf-8")
    assert gc.source_hash([source]) != first


@pytest.mark.invariant
def test_every_target_declares_sources_and_renderer():
    """target 配置必须完整，否则校验会静默跳过。"""
    for name, target in gc.TARGETS.items():
        assert target.sources, f"{name} 没有声明源"
        assert target.renderer in gc.RENDERERS, f"{name} 的 renderer 未实现"
        assert target.contract_path.exists(), f"{name} 的契约文件不存在"
