"""校验 `docs/skills/manifest.yaml`。

五道门（见 `docs/skills/README.md`）：

1. 结构门：字段完整、`id` 唯一、`layer` 合法、`active` 条目的路径必须存在；
2. 依赖门：只允许 L0 → L1 → L2，无环，**`active` 不得依赖 `planned`**；
3. 预算门：`size_estimate` 超出所在层预算即失败；
4. 复审门：`expiry` 过期告警，超过宽限期升级为失败；
5. 生成门：由各契约的生成脚本单独负责（本文件不重复实现）。

用法：

```bash
python tools/skills/check_manifest.py docs/skills/manifest.yaml
python tools/skills/check_manifest.py docs/skills/manifest.yaml --today 2026-09-18
```
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

REQUIRED_FIELDS = ("id", "layer", "status", "path", "triggers", "expiry")
VALID_LAYERS = ("L0", "L1", "L2")
VALID_STATUS = ("active", "planned")
LAYER_ORDER = {"L0": 0, "L1": 1, "L2": 2}
EXPIRY_GRACE_DAYS = 30


class Problems:
    """收集问题。门禁失败必须一次性报全，而不是逐个挤牙膏。"""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors


def check(manifest_path: Path, today: date) -> Problems:
    problems = Problems()
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        problems.error("manifest 顶层必须是映射")
        return problems

    layers = raw.get("layers") or {}
    entries = raw.get("entries") or []
    if not entries:
        problems.error("manifest 没有任何条目")
        return problems

    root = manifest_path.parent
    known: dict[str, dict] = {}

    # ---- 门 1：结构
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.error(f"entries[{index}] 不是映射")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in entry]
        if missing:
            problems.error(f"entries[{index}] 缺少字段：{missing}")
            continue

        entry_id = entry["id"]
        if entry_id in known:
            problems.error(f"id 重复：{entry_id}")
        known[entry_id] = entry

        if entry["layer"] not in VALID_LAYERS:
            problems.error(f"{entry_id}: layer 非法（{entry['layer']}）")
        if entry["status"] not in VALID_STATUS:
            problems.error(f"{entry_id}: status 非法（{entry['status']}）")
        if not entry["triggers"]:
            problems.error(f"{entry_id}: triggers 不能为空，否则无法按需加载")

        if entry["status"] == "active":
            target = root / entry["path"]
            if not target.exists():
                problems.error(f"{entry_id}: active 条目但文件不存在（{entry['path']}）")

        # ---- 门 3：体积预算
        layer_cfg = layers.get(entry["layer"], {})
        budget = (
            layer_cfg.get("budget_tokens")
            if entry["layer"] == "L0"
            else layer_cfg.get("budget_tokens_per_file")
        )
        size = entry.get("size_estimate")
        if budget and size and size > budget:
            problems.error(
                f"{entry_id}: 体积估算 {size} 超过 {entry['layer']} 预算 {budget}，应拆分"
            )

        # ---- 门 4：到期复审
        expiry = entry.get("expiry")
        if isinstance(expiry, str):
            try:
                expiry_date = date.fromisoformat(expiry)
            except ValueError:
                problems.error(f"{entry_id}: expiry 不是合法日期（{expiry}）")
            else:
                overdue = (today - expiry_date).days
                if overdue > EXPIRY_GRACE_DAYS:
                    problems.error(f"{entry_id}: 已于 {expiry} 到期且超出宽限期，必须复审")
                elif overdue > 0:
                    problems.warn(f"{entry_id}: 已于 {expiry} 到期，请尽快复审")

    # ---- 门 2：依赖
    for entry_id, entry in known.items():
        for dep in entry.get("depends_on") or []:
            if dep not in known:
                problems.error(f"{entry_id}: 依赖不存在的条目 {dep}")
                continue
            dep_entry = known[dep]
            if LAYER_ORDER[dep_entry["layer"]] > LAYER_ORDER[entry["layer"]]:
                problems.error(
                    f"{entry_id}({entry['layer']}) 依赖了更高层 {dep}({dep_entry['layer']})，"
                    f"违反单向依赖"
                )
            if entry["status"] == "active" and dep_entry["status"] == "planned":
                problems.error(f"{entry_id} 是 active，但依赖尚未编写的 {dep}(planned)")

    problems.errors.extend(_find_cycles(known))
    return problems


def _find_cycles(known: dict[str, dict]) -> list[str]:
    """深度优先找环。依赖成环会让按需加载永远无法收敛。"""
    found: list[str] = []
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 1:
            found.append("依赖成环：" + " → ".join([*trail, node]))
            return
        if state.get(node) == 2:
            return
        state[node] = 1
        for dep in known[node].get("depends_on") or []:
            if dep in known:
                visit(dep, [*trail, node])
        state[node] = 2

    for entry_id in known:
        visit(entry_id, [])
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验设计知识 manifest")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--today", type=str, default=None, help="用于到期检查的日期（ISO）")
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        print(f"找不到 manifest：{args.manifest}", file=sys.stderr)
        return 2

    today = date.fromisoformat(args.today) if args.today else date.today()
    problems = check(args.manifest, today)

    for warning in problems.warnings:
        print(f"[warn]  {warning}")
    for error in problems.errors:
        print(f"[error] {error}")

    if problems.ok:
        print(f"[ok]    manifest 校验通过（{len(problems.warnings)} 条告警）")
        return 0
    print(f"[fail]  {len(problems.errors)} 个问题需要修复")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
