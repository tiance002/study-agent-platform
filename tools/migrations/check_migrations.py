"""迁移校验门。

## 为什么要单独一个脚本

实施计划要求 CI 覆盖「数据库迁移校验」。本项目**尚未引入 Alembic**，
所以这个门当前会输出 `[SKIP]` 并以 0 退出。

**关键区分：跳过 ≠ 通过。** 一个永远返回成功的校验器等于没有校验器，
所以这里把两种结果打印得清清楚楚，让人一眼能看出「这条门现在没在保护你」。

接入 Alembic 后，CI 应当加上 `--require-active`：那时「跳过」要变成失败，
否则这条门会永远停在"提示"状态而不自知。

## 校验内容

1. 每条迁移都有 `revision`，且不重复（重复会让升级路径二义）；
2. 每条迁移都**显式声明** `down_revision` —— 根迁移要写 `None`。
   缺字段通常只是漏写，不能当成"隐式根迁移"放过（见下方说明）；
3. `down_revision` 指向的 revision 必须存在；
4. 迁移链**单头** —— 多个 head 意味着 schema 可能分叉成两套；
5. 无环。

用法：

```bash
python tools/migrations/check_migrations.py
python tools/migrations/check_migrations.py --require-active   # 跳过即失败
```
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"

# `revision: str = "abc"` 与 `revision = "abc"` 都要认。
REVISION_RE = re.compile(r"^revision(?::\s*[^=\n]+)?\s*=\s*(.+)$", re.MULTILINE)
DOWN_REVISION_RE = re.compile(r"^down_revision(?::\s*[^=\n]+)?\s*=\s*(.+)$", re.MULTILINE)


def _strip_inline_comment(text: str) -> str:
    """去掉行尾注释，但不动字符串字面量里的 `#`。"""
    in_single = in_double = False
    for index, char in enumerate(text):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return text[:index]
    return text


def _parse_parents(text: str) -> tuple[str, ...] | None:
    """把 `down_revision` 的取值解析成父节点元组。

    三种合法形态：

    - `None`           → 根迁移，返回空元组
    - `"abc"`          → 单父
    - `("a", "b")`     → **合并迁移**，两个父节点

    合并迁移这一形态曾经被漏掉：把元组整体当成一个字符串去比对头部，
    于是合并之后的迁移会被误判成"凭空多出来的 head"，而真正的分叉反而看不见。
    """
    try:
        value = ast.literal_eval(_strip_inline_comment(text).strip())
    except (ValueError, SyntaxError):
        return None

    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else None
    if isinstance(value, (tuple, list)):
        if not value or not all(isinstance(item, str) and item for item in value):
            return None
        return tuple(value)
    return None


def _parse_revision(text: str) -> str | None:
    try:
        value = ast.literal_eval(_strip_inline_comment(text).strip())
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) and value else None


def load_revisions() -> tuple[dict[str, tuple[str, ...]], list[str]]:
    """返回 {revision: 父节点元组} 与问题清单。"""
    revisions: dict[str, tuple[str, ...]] = {}
    problems: list[str] = []

    for path in sorted(VERSIONS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")

        revision_match = REVISION_RE.search(source)
        if revision_match is None:
            problems.append(f"{path.name}：缺少 revision 声明")
            continue

        revision = _parse_revision(revision_match.group(1))
        if revision is None:
            problems.append(f"{path.name}：revision 不是可解析的非空字符串字面量")
            continue
        if revision in revisions:
            problems.append(f"{path.name}：revision 重复 —— {revision}")
            continue

        down_match = DOWN_REVISION_RE.search(source)
        if down_match is None:
            # 「缺字段」和「显式写 None」是两回事：
            # 前者通常是漏写（会让这条迁移被误当成根，掩盖真实的分叉），
            # 后者才是作者明确声明「这是根迁移」。所以这里必须报错。
            problems.append(
                f"{path.name}：缺少 down_revision 声明"
                f"（根迁移请显式写 None，不要省略该字段）"
            )
            revisions[revision] = ()
            continue

        parents = _parse_parents(down_match.group(1))
        if parents is None:
            problems.append(f"{path.name}：down_revision 不是可解析的字面量")
            revisions[revision] = ()
            continue

        revisions[revision] = parents

    return revisions, problems


def check(revisions: dict[str, tuple[str, ...]], problems: list[str]) -> None:
    referenced: set[str] = set()
    for revision, parents in revisions.items():
        for parent in parents:
            referenced.add(parent)
            if parent not in revisions:
                problems.append(
                    f"{revision} 的 down_revision 指向不存在的 revision：{parent}"
                )

    heads = sorted(rev for rev in revisions if rev not in referenced)
    if len(heads) > 1:
        problems.append(
            "迁移链出现多个 head：" + ", ".join(heads) + " —— schema 可能分叉"
        )

    # 环检测用三色标记。早前用「走过的节点集合」判断，
    # 在合并迁移（一个节点有多个父）下会把合法结构误报成环。
    white, grey, black = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(revisions, white)
    reported: set[str] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        color[node] = grey
        for parent in revisions.get(node, ()):
            if parent not in revisions:
                continue
            if color[parent] is grey:
                cycle = " -> ".join((*path, node, parent))
                if cycle not in reported:
                    reported.add(cycle)
                    problems.append(f"迁移链存在环：{cycle}")
                continue
            if color[parent] is white:
                visit(parent, (*path, node))
        color[node] = black

    for node in revisions:
        if color[node] is white:
            visit(node, ())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据库迁移校验")
    parser.add_argument(
        "--require-active",
        action="store_true",
        help="把「跳过」也视为失败。引入 Alembic 之后 CI 应带上此参数，"
        "否则这道门会永远停在提示状态。",
    )
    args = parser.parse_args(argv)

    if not ALEMBIC_INI.exists() and not VERSIONS_DIR.exists():
        print("[SKIP] 尚未引入 Alembic —— 本门当前不提供任何保护。")
        print("       引入后（出现 alembic.ini 或 alembic/versions/）本脚本自动开始校验。")
        print("       注意：这是**跳过**，不是通过。")
        if args.require_active:
            print("[FAIL] 已指定 --require-active，跳过视为失败。")
            return 1
        return 0

    if not VERSIONS_DIR.exists() or not any(VERSIONS_DIR.glob("*.py")):
        print("[FAIL] 存在 Alembic 配置但没有任何迁移文件")
        return 1

    revisions, problems = load_revisions()
    if not revisions:
        print("[FAIL] 未能解析出任何 revision")
        for item in problems:
            print(f"  - {item}")
        return 1

    check(revisions, problems)

    if problems:
        print(f"[FAIL] 迁移校验发现 {len(problems)} 个问题：")
        for item in problems:
            print(f"  - {item}")
        return 1

    heads = [rev for rev in revisions if rev not in {
        parent for parents in revisions.values() for parent in parents
    }]
    print(f"[OK] 迁移校验通过：{len(revisions)} 条迁移，单头（{heads[0]}），无环")
    return 0


if __name__ == "__main__":
    sys.exit(main())
