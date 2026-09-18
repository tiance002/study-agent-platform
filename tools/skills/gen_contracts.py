"""契约生成与一致性校验。

三个 target 的导出逻辑：

| 契约 | 导出来源 | 导出内容 |
|---|---|---|
| `sql-schema` | `backend/app/**` 的 dataclass 与枚举（AST 扫描） | 代码层实体清单、字段数、是否含租户/项目列 |
| `protocol` | FastAPI OpenAPI + ErrorCode + node 定义 | 接口路径、错误码、typed node 的输入输出 schema |
| `tool-catalog` | `build_registry()` | 工具与 node 的边界字段（意图、去向、权限、幂等） |

⚠️ `sql-schema` 当前导出的是**代码层实体**，不是 PostgreSQL 表结构 ——
   迁移（alembic）落地后应改为由迁移导出，届时本文件的对应 renderer 需要替换。
   这一点写在生成区里，避免有人误以为数据库 DDL 已被覆盖。

用法：

```bash
python tools/skills/gen_contracts.py --target tool-catalog           # 写入生成区
python tools/skills/gen_contracts.py --target tool-catalog --check   # 只校验（CI 用）
python tools/skills/gen_contracts.py --all --check
```

一致性判定**基于重新渲染的内容比对**，而不只是 `source_hash`：
只比哈希会漏掉「手改了生成区但没改哈希」这种情况。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "docs" / "skills"
sys.path.insert(0, str(REPO_ROOT / "backend"))

BEGIN_MARKER = "<!-- BEGIN GENERATED:"
END_MARKER = "<!-- END GENERATED -->"


@dataclass(frozen=True)
class Target:
    name: str
    contract_path: Path
    sources: tuple[str, ...]
    renderer: str
    # 写进生成区 header 的来源说明。由配置决定，**不**从旧文件解析：
    # 否则来源变了、标签却会一直沿用旧的，读的人就被误导了。
    source_label: str


TARGETS: dict[str, Target] = {
    "sql-schema": Target(
        name="sql-schema",
        contract_path=SKILLS_DIR / "contracts" / "sql-schema.md",
        sources=("backend/app/**/*.py", "alembic/versions/**/*.py"),
        renderer="sql_schema",
        source_label="AST 扫描 backend/app 实体",
    ),
    "protocol": Target(
        name="protocol",
        contract_path=SKILLS_DIR / "contracts" / "protocol.md",
        sources=(
            "backend/app/api/**/*.py",
            "backend/app/core/errors.py",
            "backend/app/workflow/catalog.py",
        ),
        renderer="protocol",
        source_label="FastAPI OpenAPI + ErrorCode + node 定义",
    ),
    "tool-catalog": Target(
        name="tool-catalog",
        contract_path=SKILLS_DIR / "contracts" / "tool-catalog.md",
        sources=(
            "backend/app/workflow/catalog.py",
            "backend/app/execution/**/*.py",
        ),
        renderer="tool_catalog",
        source_label="tool_registry",
    ),
}


# --------------------------------------------------------------------- 源与哈希


def collect_sources(target: Target) -> list[Path]:
    """收集源文件。排序保证 `source_hash` 可复现。"""
    found: list[Path] = []
    for pattern in target.sources:
        found.extend(REPO_ROOT.glob(pattern))
    return sorted({path for path in found if path.is_file()})


def source_hash(paths: list[Path]) -> str:
    """源文件按路径排序后内容的 SHA-256。

    只要有一行代码变了哈希就变 —— 这是「改了源码必须同步契约」的机械依据。
    """
    digest = hashlib.sha256()
    for path in paths:
        try:
            key = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            # 仓库外的路径（测试里会用到）退化为绝对路径，不影响可复现性。
            key = path.as_posix()
        digest.update(key.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


# --------------------------------------------------------------------- 渲染器


def render_sql_schema() -> str:
    """扫描代码中的实体定义。

    导出**代码层实体**：类名、模块、字段数、是否含 `tenant_id` / `learning_project_id`。
    「含租户列」这件事能一眼看出来，正是这份契约最该守住的性质。
    """
    rows: list[tuple[str, str, int, str, str]] = []
    for path in sorted((REPO_ROOT / "backend" / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = path.relative_to(REPO_ROOT / "backend").with_suffix("").as_posix().replace("/", ".")
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
            if "dataclass" not in decorators:
                continue
            fields = [
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            ]
            if not fields:
                continue
            rows.append(
                (
                    node.name,
                    module,
                    len(fields),
                    "是" if "tenant_id" in fields else "—",
                    "是" if "learning_project_id" in fields else "—",
                )
            )

    lines = [
        "> 由 `tools/skills/gen_contracts.py` 从代码扫描导出，请勿手工编辑本区。",
        "> **注意**：这是**代码层实体**，不是 PostgreSQL 表。迁移落地后应改由 alembic 导出 DDL。",
        "",
        "| 实体 | 定义模块 | 字段数 | 含 tenant_id | 含 learning_project_id |",
        "|---|---|---:|---|---|",
    ]
    for name, module, count, tenant, project in sorted(rows, key=lambda r: (r[1], r[0])):
        lines.append(f"| `{name}` | `{module}` | {count} | {tenant} | {project} |")
    lines.append("")
    lines.append(f"合计 {len(rows)} 个实体。")
    return "\n".join(lines)


def render_protocol() -> str:
    """从 FastAPI 的 OpenAPI 与错误码枚举导出接口契约。"""
    from app.core.errors import ErrorCode
    from app.main import build_platform, create_app

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(platform=build_platform(var_dir=Path(tmp)))
        spec = app.openapi()

    lines = [
        "> 由 `tools/skills/gen_contracts.py` 从 FastAPI OpenAPI 与错误码枚举导出，请勿手工编辑本区。",
        "",
        "### HTTP 接口",
        "",
        "| 路径 | 方法 | 说明 |",
        "|---|---|---|",
    ]
    for path, operations in sorted(spec.get("paths", {}).items()):
        for method in sorted(operations):
            summary = operations[method].get("summary") or ""
            lines.append(f"| `{path}` | {method.upper()} | {summary} |")

    lines += [
        "",
        "### 稳定错误码",
        "",
        f"共 {len(list(ErrorCode))} 个，一经发布不得改变语义，只能追加。",
        "",
        "| 错误码 |",
        "|---|",
    ]
    for code in sorted(ErrorCode, key=lambda c: c.value):
        lines.append(f"| `{code.value}` |")

    lines += [
        "",
        "### typed node 输入输出 schema",
        "",
        "| node | 输入 schema | 输出 schema | 最低档位 |",
        "|---|---|---|---|",
    ]
    from app.workflow.catalog import build_registry

    registry = build_registry()
    for node_id in registry.node_ids():
        node = registry.node(node_id)
        lines.append(
            f"| `{node.node_id}` | `{node.input_schema}` | `{node.output_schema}` | {node.min_tier} |"
        )
    return "\n".join(lines)


def render_tool_catalog() -> str:
    """从 tool registry 导出工具与 node 的边界字段。"""
    from app.workflow.catalog import build_registry

    registry = build_registry()

    lines = [
        "> 由 `tools/skills/gen_contracts.py` 从 tool registry 导出，请勿手工编辑本区。",
        "> registry 是机器真相源，本区是它的生成视图；**禁止双写**。",
        "",
        f"registry_version: `{registry.version}`",
        "",
        "### 工具",
        "",
        "| tool_id | intent_tag | sink_class | owner_module | min_authority | exclusivity | idempotency | 成本上界 | 网络边界 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for tool_id in registry.tool_ids():
        spec = registry.tool(tool_id)
        domains = ", ".join(spec.network_domains) if spec.network_domains else "—"
        lines.append(
            f"| `{spec.tool_id}` | `{spec.intent_tag}` | {spec.sink_class} | {spec.owner_module} | "
            f"{spec.min_authority.label} | {spec.exclusivity} | {spec.idempotency} | "
            f"{spec.max_cost_units} | {domains} |"
        )

    lines += [
        "",
        "### node",
        "",
        "| node_id | 允许工具 | 最低档位 | 权限上限 | 工具次数 | 循环 | 递归 | 并发 |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for node_id in registry.node_ids():
        node = registry.node(node_id)
        tools = ", ".join(f"`{t}`" for t in node.allowed_tools) or "—"
        lines.append(
            f"| `{node.node_id}` | {tools} | {node.min_tier} | {node.authority_ceiling.label} | "
            f"{node.max_tool_calls} | {node.max_loops} | {node.max_recursion} | {node.max_concurrency} |"
        )

    lines += [
        "",
        "### 重叠检测结果",
        "",
        "注册时机械门已保证：同一 `(intent_tag, sink_class, min_authority)` 下不存在多个 exclusive 工具。",
        "若本表出现重复意图组合，说明门被绕过，应当视为构建失败。",
    ]
    return "\n".join(lines)


RENDERERS = {
    "sql_schema": render_sql_schema,
    "protocol": render_protocol,
    "tool_catalog": render_tool_catalog,
}


# --------------------------------------------------------------------- 区块读写


def extract_generated(content: str) -> str:
    """取出生成区内容（不含 BEGIN / END 标记行）。"""
    start = content.index(BEGIN_MARKER)
    start_line_end = content.index("\n", start) + 1
    end = content.index(END_MARKER)
    return content[start_line_end:end].strip()


def normalize(block: str) -> str:
    """比对前去掉不稳定的生成时间行。"""
    return "\n".join(
        line for line in block.splitlines() if not line.startswith("> 生成时间：")
    ).strip()


def write_generated(contract_path: Path, body: str, digest: str, source_label: str) -> None:
    content = contract_path.read_text(encoding="utf-8")
    start = content.index(BEGIN_MARKER)
    end = content.index(END_MARKER)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_header = (
        f"{BEGIN_MARKER} source={source_label}, source_hash={digest}, generated_at={stamp} -->\n"
    )
    new_body = f"> 生成时间：{stamp}\n\n{body}\n"
    contract_path.write_text(
        content[:start] + new_header + new_body + content[end:],
        encoding="utf-8",
        # 显式写 \n：Windows 上 Path.write_text 默认把 \n 转成 \r\n，
        # 而 .gitattributes 要求文本用 LF。不指定的话，每次生成都会产生
        # 整文件的行尾 diff 噪音，把真正的契约变更淹没掉。
        newline="\n",
    )


# --------------------------------------------------------------------- 主流程


def run_target(target: Target, *, check: bool) -> int:
    if not target.contract_path.exists():
        print(f"[fail] {target.name}: 契约文件不存在（{target.contract_path}）")
        return 2

    paths = collect_sources(target)
    if not paths:
        print(
            f"[skip] {target.name}: 源文件尚未就绪（{'、'.join(target.sources)}）。\n"
            f"       契约保留设计期内容 —— 这**不是**校验通过。"
        )
        return 0

    digest = source_hash(paths)
    rendered = RENDERERS[target.renderer]()
    content = target.contract_path.read_text(encoding="utf-8")
    recorded_body = extract_generated(content)
    recorded_hash = content[content.index(BEGIN_MARKER):].split("source_hash=")[1].split(",")[0]

    if check:
        problems = []
        if normalize(recorded_body) != normalize(rendered):
            problems.append("生成区内容与当前源码导出结果不一致")
        if recorded_hash != digest:
            problems.append(f"source_hash 不一致（记录 {recorded_hash}，实际 {digest}）")
        if problems:
            print(
                f"[fail] {target.name}: {'；'.join(problems)}\n"
                f"       重新生成：python tools/skills/gen_contracts.py --target {target.name}"
            )
            return 1
        print(f"[ok]   {target.name}: 生成区与源码一致（{digest[:19]}…）")
        return 0

    write_generated(target.contract_path, rendered, digest, target.source_label)
    print(f"[ok]   {target.name}: 已写入生成区（{digest[:19]}…）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="契约生成与一致性校验")
    parser.add_argument("--target", choices=sorted(TARGETS))
    parser.add_argument("--all", action="store_true", help="处理全部 target")
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    args = parser.parse_args(argv)

    if not args.target and not args.all:
        parser.error("需要 --target 或 --all")

    names = sorted(TARGETS) if args.all else [args.target]
    worst = 0
    for name in names:
        worst = max(worst, run_target(TARGETS[name], check=args.check))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
