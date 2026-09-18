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


@pytest.mark.invariant
def test_check_detects_hand_edited_generated_block(tmp_path):
    """手工改动生成区必须被发现，否则这道门形同虚设。"""
    original = gc.TARGETS["tool-catalog"]
    copy_path = tmp_path / "tool-catalog.md"
    copy_path.write_text(original.contract_path.read_text(encoding="utf-8"), encoding="utf-8")
    target = gc.Target(
        name=original.name,
        contract_path=copy_path,
        sources=original.sources,
        renderer=original.renderer,
        source_label=original.source_label,
    )

    assert gc.run_target(target, check=True) == 0, "复制后应当是一致的"

    copy_path.write_text(
        copy_path.read_text(encoding="utf-8").replace(
            "retrieve_chunk", "retrieve_chunk_tampered"
        ),
        encoding="utf-8",
    )
    assert gc.run_target(target, check=True) == 1, "生成区被手改后必须报错"


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
