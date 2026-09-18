"""机械门自身的测试。

**一个永远返回成功的校验器等于没有校验器。**

这个文件的唯一目的是证明新加的两道 CI 门（许可证扫描、迁移校验）
**真的能发现坏情况**，而不只是"能运行、不报错"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.migrations import check_migrations as cm  # noqa: E402
from tools.security import scan_licenses as sl  # noqa: E402

# --------------------------------------------------------------- 许可证判定


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 名称形态
        ("MIT", sl.Verdict.PERMISSIVE),
        ("BSD-3-Clause", sl.Verdict.PERMISSIVE),
        ("Apache-2.0", sl.Verdict.PERMISSIVE),
        ("Python Software Foundation License", sl.Verdict.PERMISSIVE),
        # 弱 copyleft 不能被归入「宽松」—— 那是在替使用者做一个未经验证的判断。
        # 它不阻塞发布，但必须显式提示。
        ("Mozilla Public License 2.0 (MPL 2.0)", sl.Verdict.WEAK_COPYLEFT),
        ("MPL-2.0", sl.Verdict.WEAK_COPYLEFT),
        ("Eclipse Public License 2.0", sl.Verdict.WEAK_COPYLEFT),
        # 全文形态：证书正文里根本没有 "MIT" / "BSD" 字样。
        # 只认名称会让这类包全部掉进「无法判定」，把扫描器变成噪音机。
        (
            "Permission is hereby granted, free of charge, to any person obtaining a copy",
            sl.Verdict.PERMISSIVE,
        ),
        (
            "Redistribution and use in source and binary forms, with or without modification",
            sl.Verdict.PERMISSIVE,
        ),
        # copyleft：连写、全名、弱 copyleft 三种形态都要抓到
        ("GPL-3.0-only", sl.Verdict.COPYLEFT),
        ("AGPL-3.0", sl.Verdict.COPYLEFT),
        ("GNU General Public License v3", sl.Verdict.COPYLEFT),
        ("GNU Affero General Public License v3", sl.Verdict.COPYLEFT),
        ("GNU Lesser General Public License v2.1", sl.Verdict.COPYLEFT),
        ("LGPL-2.1-or-later", sl.Verdict.COPYLEFT),
        # 双许可：有宽松那条可选，不阻塞但要提示
        ("MIT OR GPL-3.0-only", sl.Verdict.DUAL),
        ("Apache-2.0 AND LGPL-2.1", sl.Verdict.DUAL),
        # 无法归类必须落到 UNKNOWN，进「待人工确认」——绝不能静默放过
        ("Zope Public License", sl.Verdict.UNKNOWN),
        ("Server Side Public License", sl.Verdict.UNKNOWN),
        ("", sl.Verdict.UNKNOWN),
        ("   ", sl.Verdict.UNKNOWN),
    ],
)
def test_license_classification(text, expected):
    assert sl.classify_license(text) is expected


def test_mit_full_text_is_not_unknown():
    """回归：MIT 全文曾因不含 "MIT" 字样被判成「无法判定」。"""
    mit_full = (
        "Permission is hereby granted, free of charge, to any person obtaining a copy "
        "of this software and associated documentation files (the Software), to deal "
        "in the Software without restriction."
    )
    assert sl.classify_license(mit_full) is sl.Verdict.PERMISSIVE


def test_lgpl_full_name_is_copyleft():
    """回归：`GNU Lesser General Public License` 全名曾不被任何模式捕获。"""
    assert sl.classify_license("GNU Lesser General Public License v3") is sl.Verdict.COPYLEFT


# --------------------------------------------------------------- 迁移校验


def _write_migration(tmp_path: Path, name: str, revision: str, down: str | None) -> None:
    versions = tmp_path / "versions"
    versions.mkdir(exist_ok=True)
    down_literal = "None" if down is None else repr(down)
    (versions / f"{name}.py").write_text(
        f'revision = "{revision}"\ndown_revision = {down_literal}\n',
        encoding="utf-8",
    )


def _redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把模块级路径常量指向临时目录，让 alembic 存在。"""
    (tmp_path / "alembic.ini").write_text("", encoding="utf-8")
    monkeypatch.setattr(cm, "ALEMBIC_INI", tmp_path / "alembic.ini")
    monkeypatch.setattr(cm, "VERSIONS_DIR", tmp_path / "versions")


@pytest.mark.invariant
def test_main_reports_skip_without_alembic(monkeypatch, tmp_path, capsys):
    """没有迁移工具时必须明确说「跳过」，且不能让读者误以为通过了。"""
    monkeypatch.setattr(cm, "ALEMBIC_INI", tmp_path / "alembic.ini")
    monkeypatch.setattr(cm, "VERSIONS_DIR", tmp_path / "versions")

    assert cm.main([]) == 0
    out = capsys.readouterr().out
    assert "[SKIP]" in out
    assert "不是通过" in out


@pytest.mark.invariant
def test_clean_chain_passes(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    _write_migration(tmp_path, "0001_root", "a", None)
    _write_migration(tmp_path, "0002_next", "b", "a")

    graph, problems = cm.load_revisions()
    cm.check(graph, problems)

    assert problems == []
    # 父节点统一用元组表示：根迁移 = 空元组，单父 = 长度 1。
    # 这样合并迁移（多父）不需要另一套分支。
    assert graph == {"a": (), "b": ("a",)}


@pytest.mark.invariant
def test_duplicate_revision_is_reported(monkeypatch, tmp_path):
    """revision 重复会让升级路径二义，必须拦。"""
    _redirect(monkeypatch, tmp_path)
    _write_migration(tmp_path, "0001", "a", None)
    _write_migration(tmp_path, "0002", "a", None)

    _, problems = cm.load_revisions()
    assert any("重复" in item for item in problems)


@pytest.mark.invariant
def test_multiple_heads_are_reported(monkeypatch, tmp_path):
    """多个 head 意味着 schema 可能分叉成两套。"""
    _redirect(monkeypatch, tmp_path)
    _write_migration(tmp_path, "0001", "a", None)
    _write_migration(tmp_path, "0002", "b", "a")
    _write_migration(tmp_path, "0003", "c", "a")

    graph, problems = cm.load_revisions()
    cm.check(graph, problems)
    assert any("多个 head" in item for item in problems)


@pytest.mark.invariant
def test_cycle_is_reported(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    _write_migration(tmp_path, "0001", "a", "b")
    _write_migration(tmp_path, "0002", "b", "a")

    graph, problems = cm.load_revisions()
    cm.check(graph, problems)
    assert any("环" in item for item in problems)


@pytest.mark.invariant
def test_dangling_down_revision_is_reported(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    _write_migration(tmp_path, "0001", "a", "ghost")

    graph, problems = cm.load_revisions()
    cm.check(graph, problems)
    assert any("不存在" in item for item in problems)


@pytest.mark.invariant
def test_main_fails_when_alembic_exists_but_versions_empty(monkeypatch, tmp_path, capsys):
    """有 alembic 配置却没有迁移文件 —— 这不是「跳过」，是配置不完整，必须失败。"""
    _redirect(monkeypatch, tmp_path)
    (tmp_path / "versions").mkdir()

    assert cm.main([]) == 1
    assert "[FAIL]" in capsys.readouterr().out


@pytest.mark.invariant
def test_missing_down_revision_is_reported(monkeypatch, tmp_path):
    """缺 `down_revision` 字段 ≠ 显式写 `None`。

    缺字段通常只是漏写。把它当成「隐式根迁移」放过，这条迁移会在头部判定里
    凭空变成一个新的 head，**把真实的分叉掩盖掉** —— 恰好是最该被看见的情况。
    """
    _redirect(monkeypatch, tmp_path)
    versions = tmp_path / "versions"
    versions.mkdir(exist_ok=True)
    (versions / "0001.py").write_text('revision = "a"\n', encoding="utf-8")

    revisions, problems = cm.load_revisions()
    assert any("缺少 down_revision" in item for item in problems), problems
    assert revisions == {"a": ()}


@pytest.mark.invariant
def test_explicit_none_is_a_valid_root(monkeypatch, tmp_path):
    """显式写 `None` 才是合法的根迁移声明。"""
    _redirect(monkeypatch, tmp_path)
    _write_migration(tmp_path, "0001", "a", None)

    revisions, problems = cm.load_revisions()
    assert problems == []
    assert revisions == {"a": ()}


@pytest.mark.invariant
def test_merge_migration_is_parsed_as_multiple_parents(monkeypatch, tmp_path):
    """合并迁移的 `down_revision` 是元组，必须被解析成多个父节点。"""
    _redirect(monkeypatch, tmp_path)
    versions = tmp_path / "versions"
    versions.mkdir(exist_ok=True)
    (versions / "0001.py").write_text(
        'revision = "d"\ndown_revision = ("b", "c")\n', encoding="utf-8"
    )

    revisions, problems = cm.load_revisions()
    assert problems == []
    assert revisions["d"] == ("b", "c")


@pytest.mark.invariant
def test_merge_migration_still_yields_single_head(monkeypatch, tmp_path):
    """合并之后必须回到单头。

    早期实现把元组整体当成一个字符串去比对头部，于是合并后的迁移
    会被误判成「凭空多出来的 head」，而真正的分叉反而看不见 ——
    两种错都会发生，方向还相反。
    """
    _redirect(monkeypatch, tmp_path)
    _write_migration(tmp_path, "0001", "a", None)
    _write_migration(tmp_path, "0002", "b", "a")
    _write_migration(tmp_path, "0003", "c", "a")
    versions = tmp_path / "versions"
    (versions / "0004.py").write_text(
        'revision = "d"\ndown_revision = ("b", "c")\n', encoding="utf-8"
    )

    revisions, problems = cm.load_revisions()
    cm.check(revisions, problems)

    assert problems == [], problems


@pytest.mark.invariant
def test_real_fork_is_still_detected(monkeypatch, tmp_path):
    """确实分叉时必须报多头 —— 不能因为支持合并就把真分叉也放过。"""
    _redirect(monkeypatch, tmp_path)
    _write_migration(tmp_path, "0001", "a", None)
    _write_migration(tmp_path, "0002", "b", "a")
    _write_migration(tmp_path, "0003", "c", "a")

    revisions, problems = cm.load_revisions()
    cm.check(revisions, problems)

    assert any("多个 head" in item for item in problems), problems


@pytest.mark.invariant
def test_require_active_turns_skip_into_failure(monkeypatch, tmp_path, capsys):
    """`--require-active` 让「跳过」变成失败。

    否则这道门会永远停在提示状态：日志里写着 SKIP，CI 一路绿灯，
    没人会发现它其实什么都没校验。
    """
    monkeypatch.setattr(cm, "ALEMBIC_INI", tmp_path / "alembic.ini")
    monkeypatch.setattr(cm, "VERSIONS_DIR", tmp_path / "versions")

    assert cm.main([]) == 0
    capsys.readouterr()
    assert cm.main(["--require-active"]) == 1
    assert "[FAIL]" in capsys.readouterr().out


# --------------------------------------------------------------- 许可证豁免规则


class _FakeMeta:
    """模拟 importlib.metadata 的 email.Message 接口。"""

    def __init__(self, **fields: str) -> None:
        self._fields = fields

    def get(self, key: str, default=None):
        return self._fields.get(key, default)

    def get_all(self, key: str):
        return []


class _FakeDist:
    def __init__(self, **fields: str) -> None:
        self.metadata = _FakeMeta(**fields)


@pytest.mark.invariant
def test_allowlist_entry_without_reason_is_an_error(monkeypatch, tmp_path):
    """无理由豁免必须直接报错。

    「不允许无理由豁免」如果只写在注释里，就等于没有。
    """
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("somepkg\n", encoding="utf-8")
    monkeypatch.setattr(sl, "ALLOWLIST", allowlist)

    with pytest.raises(ValueError):
        sl.load_allowlist()


@pytest.mark.invariant
def test_allowlist_entry_with_reason_is_accepted(monkeypatch, tmp_path):
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("somepkg  # 仅在测试期使用，不随产物分发\n", encoding="utf-8")
    monkeypatch.setattr(sl, "ALLOWLIST", allowlist)

    assert sl.load_allowlist() == {"somepkg": "仅在测试期使用，不随产物分发"}


@pytest.mark.invariant
def test_unknown_license_blocks_release(monkeypatch, tmp_path, capsys):
    """无法判定的许可必须**阻塞**。

    早期版本让它静默通过，等于扫描器自带一个看不见的洞：
    任何规则没覆盖到的新许可证都会从那里溜走。
    """
    monkeypatch.setattr(sl, "ALLOWLIST", tmp_path / "missing.txt")
    monkeypatch.setattr(
        sl,
        "distributions",
        lambda: [_FakeDist(Name="weird-pkg", License="Zope Public License")],
    )

    assert sl.main() == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "weird-pkg" in out


@pytest.mark.invariant
def test_weak_copyleft_warns_but_does_not_block(monkeypatch, tmp_path, capsys):
    """弱 copyleft 不阻塞，但必须出现在输出里 —— 不能被静默归入「宽松」。"""
    monkeypatch.setattr(sl, "ALLOWLIST", tmp_path / "missing.txt")
    monkeypatch.setattr(
        sl,
        "distributions",
        lambda: [_FakeDist(Name="mpl-pkg", License="MPL-2.0")],
    )

    assert sl.main() == 0
    out = capsys.readouterr().out
    assert "[WARN]" in out
    assert "mpl-pkg" in out


@pytest.mark.invariant
def test_strong_copyleft_blocks_release(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sl, "ALLOWLIST", tmp_path / "missing.txt")
    monkeypatch.setattr(
        sl,
        "distributions",
        lambda: [_FakeDist(Name="gpl-pkg", License="GNU General Public License v3")],
    )

    assert sl.main() == 1
    assert "[FAIL]" in capsys.readouterr().out


@pytest.mark.invariant
def test_allowlisted_package_is_skipped(monkeypatch, tmp_path, capsys):
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("gpl-pkg # 已获商业授权\n", encoding="utf-8")
    monkeypatch.setattr(sl, "ALLOWLIST", allowlist)
    monkeypatch.setattr(
        sl,
        "distributions",
        lambda: [_FakeDist(Name="gpl-pkg", License="GNU General Public License v3")],
    )

    assert sl.main() == 0
    assert "1 条豁免" in capsys.readouterr().out
