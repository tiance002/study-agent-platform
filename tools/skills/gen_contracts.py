"""契约生成与一致性校验（骨架）。

对应 `docs/skills/README.md` 的「生成区 / 手写区」机制，以及实施计划任务 9。

三份首批契约各自有一个生成目标：

| 契约 | 源 | 导出内容 |
|---|---|---|
| `sql-schema` | `alembic/versions/**`、`backend/app/**/models.py` | 表、列、约束、索引、RLS、迁移链 |
| `protocol` | `backend/app/api/**`、`backend/app/**/schemas.py`、`node 定义` | OpenAPI 形状、错误码、node schema |
| `tool-catalog` | `backend/app/execution/registry/**`、`backend/app/workflow/catalog.py` | 工具清单与边界字段 |

用法：

```bash
python tools/skills/gen_contracts.py --target tool-catalog --check
python tools/skills/gen_contracts.py --target sql-schema
```

**设计期行为**：当源文件尚不存在时，脚本不会伪造内容，而是明确报告「源未就绪」并以
退出码 0 结束 —— 让 CI 在代码落地前保持绿色，但绝不假装校验已经生效。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "docs" / "skills"

BEGIN_MARKER = "<!-- BEGIN GENERATED:"
END_MARKER = "<!-- END GENERATED -->"


@dataclass(frozen=True)
class Target:
    name: str
    contract_path: Path
    sources: tuple[str, ...]


TARGETS: dict[str, Target] = {
    "sql-schema": Target(
        name="sql-schema",
        contract_path=SKILLS_DIR / "contracts" / "sql-schema.md",
        sources=("alembic/versions/**/*.py", "backend/app/**/models.py"),
    ),
    "protocol": Target(
        name="protocol",
        contract_path=SKILLS_DIR / "contracts" / "protocol.md",
        sources=(
            "backend/app/api/**/*.py",
            "backend/app/**/schemas.py",
            "backend/app/workflow/catalog.py",
        ),
    ),
    "tool-catalog": Target(
        name="tool-catalog",
        contract_path=SKILLS_DIR / "contracts" / "tool-catalog.md",
        sources=(
            "backend/app/workflow/catalog.py",
            "backend/app/execution/**/*.py",
        ),
    ),
}


def collect_sources(target: Target) -> list[Path]:
    """收集源文件。排序保证 `source_hash` 可复现。"""
    found: list[Path] = []
    for pattern in target.sources:
        found.extend(sorted(REPO_ROOT.glob(pattern)))
    return sorted(set(found))


def source_hash(paths: list[Path]) -> str:
    """源文件按路径排序后内容的 SHA-256。

    只要有一行代码变了，哈希就变 —— 这是「改了源码必须同步契约」的机械依据。
    """
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def read_generated_hash(contract_path: Path) -> str | None:
    text = contract_path.read_text(encoding="utf-8")
    match = re.search(r"source_hash=([A-Za-z0-9:]+)", text)
    return match.group(1) if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="契约生成与一致性校验")
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    args = parser.parse_args(argv)

    target = TARGETS[args.target]
    if not target.contract_path.exists():
        print(f"[fail] 契约文件不存在：{target.contract_path}", file=sys.stderr)
        return 2

    paths = collect_sources(target)
    if not paths:
        print(
            f"[skip] {target.name}: 源文件尚未就绪（{'、'.join(target.sources)}）。\n"
            f"       契约保留设计期内容，生成区仍是占位 —— 这不是校验通过。"
        )
        return 0

    current = source_hash(paths)
    recorded = read_generated_hash(target.contract_path)

    print(f"[info] {target.name}: 源文件 {len(paths)} 个，source_hash={current}")
    print(f"[info] 契约记录：{recorded}")

    if args.check:
        if recorded == current:
            print("[ok]   生成区与源码一致")
            return 0
        print(
            "[fail] 生成区与源码不一致：源码已改动但契约未重新生成。\n"
            f"       契约：{target.contract_path}\n"
            f"       执行：python tools/skills/gen_contracts.py --target {target.name}"
        )
        return 1

    print(
        f"[todo] 请把导出结果写入 {target.contract_path} 的生成区，"
        f"并把 source_hash 更新为 {current}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
