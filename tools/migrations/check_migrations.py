"""迁移校验门。

## 为什么要单独一个脚本

实施计划要求 CI 覆盖「数据库迁移校验」。本项目**尚未引入 Alembic**，
所以这个门当前会输出 `[SKIP]` 并以 0 退出。

**关键区分：跳过 ≠ 通过。** 一个永远返回成功的校验器等于没有校验器，
所以这里把两种结果打印得清清楚楚，让人一眼能看出「这条门现在没在保护你」。

## 引入 Alembic 后自动切换为真实校验

一旦出现 `alembic.ini` 或 `alembic/versions/*.py`，本脚本立刻检查：

1. 每条迁移都有 `revision` 与 `down_revision`；
2. `revision` 不重复（重复会让升级路径二义）；
3. 迁移链**单头** —— 多个 head 意味着 schema 可能分叉成两套；
4. 无环（沿 `down_revision` 回溯必须能走到根）。

用法：

```bash
python tools/migrations/check_migrations.py
```
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"

REVISION_RE = re.compile(r"^revision(?::\s*str)?\s*=\s*(.+)$", re.MULTILINE)
DOWN_REVISION_RE = re.compile(r"^down_revision(?::\s*[^=]+)?\s*=\s*(.+)$", re.MULTILINE)


def _literal(text: str) -> str | None:
    """把 `'abc'` / `"abc"` / `None` / `('a', 'b')` 解析成规范化字符串。"""
    text = text.strip()
    if text.startswith("#"):
        text = text.split("#", 1)[0].strip() or text
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (tuple, list)):
        return str(tuple(value))
    return None


def load_revisions() -> tuple[dict[str, str | None], list[str]]:
    """返回 {revision: down_revision} 与问题清单。"""
    graph: dict[str, str | None] = {}
    problems: list[str] = []

    for path in sorted(VERSIONS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision_match = REVISION_RE.search(source)
        if revision_match is None:
            problems.append(f"{path.name}：缺少 revision 声明")
            continue

        revision = _literal(revision_match.group(1))
        if revision is None:
            problems.append(f"{path.name}：revision 不是可解析的字面量")
            continue
        if revision in graph:
            problems.append(f"{path.name}：revision 重复 —— {revision}")
            continue

        down_match = DOWN_REVISION_RE.search(source)
        graph[revision] = _literal(down_match.group(1)) if down_match else None

    return graph, problems


def check(graph: dict[str, str | None], problems: list[str]) -> None:
    referenced = {down for down in graph.values() if down}
    for down in referenced:
        # down_revision 可能是元组形式（合并迁移），逐个核对。
        candidates = [
            part.strip(" '\"()")
            for part in down.strip("()").split(",")
            if part.strip(" '\"()")
        ]
        for candidate in candidates:
            if candidate not in graph:
                problems.append(f"down_revision 指向不存在的 revision：{candidate}")

    heads = [rev for rev in graph if rev not in referenced]
    if len(heads) > 1:
        problems.append(
            "迁移链出现多个 head：" + ", ".join(sorted(heads)) + " —— schema 可能分叉"
        )

    # 沿 down_revision 回溯，检查是否有环。
    for start in graph:
        seen: set[str] = set()
        node: str | None = start
        while node is not None:
            if node in seen:
                problems.append(f"迁移链存在环，起点：{start}")
                break
            seen.add(node)
            node = graph.get(node)


def main() -> int:
    if not ALEMBIC_INI.exists() and not VERSIONS_DIR.exists():
        print("[SKIP] 尚未引入 Alembic —— 本门当前不提供任何保护。")
        print("       引入后（出现 alembic.ini 或 alembic/versions/）本脚本自动开始校验。")
        print("       注意：这是**跳过**，不是通过。")
        return 0

    if not VERSIONS_DIR.exists() or not any(VERSIONS_DIR.glob("*.py")):
        print("[FAIL] 存在 Alembic 配置但没有任何迁移文件")
        return 1

    graph, problems = load_revisions()
    if not graph:
        print("[FAIL] 未能解析出任何 revision")
        return 1

    check(graph, problems)

    if problems:
        print(f"[FAIL] 迁移校验发现 {len(problems)} 个问题：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print(f"[OK] 迁移校验通过：{len(graph)} 条迁移，单头，无环")
    return 0


if __name__ == "__main__":
    sys.exit(main())
