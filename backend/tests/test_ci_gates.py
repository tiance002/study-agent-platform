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
        ("Mozilla Public License 2.0 (MPL 2.0)", sl.Verdict.PERMISSIVE),
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

    assert cm.main() == 0
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
    assert graph == {"a": None, "b": "a"}


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

    assert cm.main() == 1
    assert "[FAIL]" in capsys.readouterr().out
