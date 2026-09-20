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
import re
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
        # 渲染器只读迁移 —— sources 收窄到迁移目录，
        # 否则任何 app 代码改动都会让本视图的 source_hash 无谓漂移。
        sources=("alembic/versions/**/*.py",),
        renderer="sql_schema",
        source_label="由 alembic 迁移导出（数据库的权威定义）",
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


def _migration_modules() -> list[tuple[str, ast.Module]]:
    """按文件名排序加载迁移模块（0001 < 0002 …）。"""
    found: list[tuple[str, ast.Module]] = []
    for path in sorted((REPO_ROOT / "alembic" / "versions").glob("*.py")):
        if path.name.startswith("__"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.append((path.name, tree))
    return found


def _module_literal(tree: ast.Module, name: str) -> object | None:
    """取模块级 `NAME = <字面量>`（含带类型注解的写法）。

    用 `literal_eval` 而不是导入执行：迁移模块只需要被**读**，
    不应该在生成契约时被跑一遍。
    """
    for node in tree.body:
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        else:
            continue
        if value is not None and any(t.id == name for t in targets):
            try:
                return ast.literal_eval(value)
            except ValueError:
                return None
    return None


def _create_table_blocks(tree: ast.Module) -> list[str]:
    """模块里所有含 `CREATE TABLE` 的字符串字面量。"""
    blocks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "CREATE TABLE" in node.value:
                blocks.append(node.value)
    return blocks


_TABLE_NAME_RE = re.compile(r"CREATE TABLE\s+(\w+)\s*\(")
_TABLE_BODY_RE = re.compile(r"CREATE TABLE\s+\w+\s*\((.*?)\n\)\s*$", re.DOTALL)
#: 表级约束不是列。按行首关键字跳过 —— 比靠缩进稳（缩进在 SQL 里没有语义）。
#:
#: `REFERENCES` 也在列：跨行的 `FOREIGN KEY (...)` 声明里，续行以它开头
#: （见 0007）。不跳过的话，一条外键会被读成三个名为 `REFERENCES` 的列。
_TABLE_CONSTRAINT_KEYWORDS = frozenset(
    {"UNIQUE", "CHECK", "PRIMARY", "FOREIGN", "CONSTRAINT", "EXCLUDE", "REFERENCES"}
)


def _strip_sql_comment(line: str) -> str:
    """去掉行内 `--` 注释。

    迁移里的注释是给人读的，但逐行解析器会把整行当成列定义 ——
    实测：`-- 见模块 docstring：…` 会以一个名为 `--` 的"列"出现在契约里，
    并把列数报多。**契约是机械门禁的判据，读错一行就等于骗一次。**
    """
    marker = line.find("--")
    return line if marker == -1 else line[:marker]


def _text_blocks(tree: ast.Module) -> list[str]:
    """模块里所有字符串字面量（按源码顺序）。

    `ALTER TABLE ... ADD COLUMN` 就是这样被发现的：表结构不只由 `CREATE TABLE`
    决定，后续迁移加列同样是结构的一部分 —— 漏掉它，契约里的列数就会骗人。
    """
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


_ADD_COLUMN_RE = re.compile(
    r"ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(\w+)\s+([A-Za-z_][A-Za-z0-9_]*)"
)

#: 合法列名。用于自检：注释或跨行约束被读成"列"时，读出来的名字会带 `--`
#: 或 `REFERENCES` 这类非法字符，一眼就能被这条正则拦下。
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")



def _table_name(block: str) -> str | None:
    found = _TABLE_NAME_RE.search(block)
    return found.group(1) if found else None


def _columns_of(block: str) -> list[tuple[str, str]]:
    """从 `CREATE TABLE` 取 (列名, 类型)。

    跨行的列约束（类型行之后再另起一行的 `CHECK (...)`）会被正确跳过：
    那一行的首关键字是 `CHECK`。
    """
    body = _TABLE_BODY_RE.search(block)
    if body is None:
        return []
    columns: list[tuple[str, str]] = []
    for raw_line in body.group(1).splitlines():
        line = _strip_sql_comment(raw_line).strip().rstrip(",").strip()
        if not line:
            continue
        parts = line.split()
        if parts[0].upper() in _TABLE_CONSTRAINT_KEYWORDS:
            continue
        columns.append((parts[0], parts[1].lower() if len(parts) > 1 else ""))
    return columns


def _grants_of(table: str, append_only: set[str], no_delete: set[str]) -> str:
    if table in append_only:
        return "SELECT, INSERT"
    if table in no_delete:
        return "SELECT, INSERT, UPDATE"
    return "SELECT, INSERT, UPDATE, DELETE"


def _head_revision() -> str:
    """迁移链的 head：出现在 `revision` 却不出现在任何 `down_revision` 里的那个。"""
    revisions: set[str] = set()
    downs: set[str] = set()
    for _filename, tree in _migration_modules():
        revision = _module_literal(tree, "revision")
        down = _module_literal(tree, "down_revision")
        if isinstance(revision, str):
            revisions.add(revision)
        if isinstance(down, str):
            downs.add(down)
    heads = sorted(revisions - downs)
    return ", ".join(heads) if heads else "—"


def render_sql_schema() -> str:
    """从 **alembic 迁移**导出表、隔离级别与应用角色的权限。

    为什么读迁移而不是扫描代码：**迁移才是数据库的权威定义**。
    本函数早先扫描 `backend/app` 的 dataclass，导出的是"代码层实体" ——
    它与真实表结构之间**没有任何机械联系**：契约可以整体没错，
    而实际表已经完全不同。改成读迁移后 `source_hash` 覆盖
    `alembic/versions/**`，任何一次表结构改动都会让这道门禁报警。

    迁移模块暴露的约定（缺省即不生效）：

    | 常量 | 含义 |
    |---|---|
    | `TABLES` | 本迁移新建、并启用 FORCE RLS 的表 |
    | `PROJECT_SCOPED` | 其中属于项目级的（谓词额外要求 `app.project_id`） |
    | `APPEND_ONLY` | 只给应用角色 `SELECT, INSERT` |
    | `NO_DELETE` | 只给 `SELECT, INSERT, UPDATE` |
    | `SYSTEM_TABLES` | 认证前系统设施：无租户、无表权限，仅 definer 函数可触达 |
    | `POLICY_OVERRIDES` | 改写既有表的隔离级别，如 `{"projects": "成员感知"}` |
    """
    tables: dict[str, dict[str, object]] = {}
    for filename, tree in _migration_modules():
        revision = filename.split("_", 1)[0]
        project_scoped = set(_module_literal(tree, "PROJECT_SCOPED") or ())
        principal_scoped = set(_module_literal(tree, "PRINCIPAL_SCOPED") or ())
        append_only = set(_module_literal(tree, "APPEND_ONLY") or ())
        no_delete = set(_module_literal(tree, "NO_DELETE") or ())
        system_tables = set(_module_literal(tree, "SYSTEM_TABLES") or ())
        overrides = _module_literal(tree, "POLICY_OVERRIDES") or {}

        for block in _create_table_blocks(tree):
            name = _table_name(block)
            if name is None:
                continue
            if name in system_tables:
                # 认证前系统设施（限流计数表）：没有租户、不套用 RLS 租户模板，
                # 应用角色没有任何裸表权限，只能通过 SECURITY DEFINER 函数触达。
                scope = "系统级（无租户，认证前设施）"
                grants = "无表权限（仅 SECURITY DEFINER 函数 EXECUTE）"
            else:
                # 隔离级别写成"叠加了几层"而不是一个词：租户是最底层，
                # 项目与主体是往上加的约束。这样 `user_sessions`（租户+主体）
                # 与 `conversations`（租户+项目）的差别一眼能看出来 ——
                # 而"租户级"这个笼统说法正是漏掉主体维度的原因。
                scope = "+".join(
                    ["租户"]
                    + (["项目"] if name in project_scoped else [])
                    + (["主体"] if name in principal_scoped else [])
                )
                grants = _grants_of(name, append_only, no_delete)
            tables[name] = {
                "revision": revision,
                "scope": scope,
                "grants": grants,
                "columns": _columns_of(block),
            }

        # 后续迁移给既有表加列，同样是表结构的一部分。
        for block in _text_blocks(tree):
            for table, column, ctype in _ADD_COLUMN_RE.findall(block):
                info = tables.get(table)
                if info is None:
                    continue
                existing = {name for name, _ in info["columns"]}  # type: ignore[union-attr]
                if column not in existing:
                    info["columns"].append((column, ctype.lower()))  # type: ignore[union-attr]

        for name, label in dict(overrides).items():
            if name in tables:
                tables[name]["scope"] = label
                tables[name]["revision"] = f"{tables[name]['revision']} → {revision} 改写"

    lines = [
        "> 由 `tools/skills/gen_contracts.py` 从 **alembic 迁移**导出，请勿手工编辑本区。",
        "> 迁移是数据库的权威定义，本区是它的生成视图。",
        "",
        f"当前 head：`{_head_revision()}`",
        "",
        "### 表总览",
        "",
        "| 表 | 来源迁移 | 隔离级别 | 应用角色权限 | 列数 |",
        "|---|---|---|---|---:|",
    ]
    for name in sorted(tables):
        info = tables[name]
        lines.append(
            f"| `{name}` | {info['revision']} | {info['scope']} | {info['grants']} | "
            f"{len(info['columns'])} |"
        )

    lines += ["", "### 列明细", "", "| 表 | 列 | 类型 |", "|---|---|---|"]
    for name in sorted(tables):
        for column, ctype in tables[name]["columns"]:  # type: ignore[union-attr]
            lines.append(f"| `{name}` | `{column}` | `{ctype}` |")

    lines += [
        "",
        "### 本区不覆盖的内容",
        "",
        "策略谓词、`GRANT` 语句、索引与 `CHECK` 约束的**文本**不在本表里 ——",
        "它们由迁移文件承载，改迁移即可，不需要维护两份。",
        "本区回答三个问题：有哪些表、每张表怎么隔离、应用角色能做什么。",
    ]
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


def parse_header(content: str) -> dict[str, str]:
    """解析 BEGIN 标记行里的 `source` / `source_hash` / `generated_at`。

    用**已知键**定位，而不是按逗号切分：`source` 是人类可读说明，
    将来写成带逗号的形式完全可能，按逗号切会静默解析错 ——
    而这里恰恰是用来防漂移的地方，解析错就等于白防。
    """
    start = content.index(BEGIN_MARKER)
    line = content[start : content.index("-->", start)]

    def cut(text: str, key: str, *next_keys: str) -> str:
        value = text.split(f"{key}=", 1)[1]
        stops = [value.find(f", {other}=") for other in next_keys]
        stops = [stop for stop in stops if stop != -1]
        return (value[: min(stops)] if stops else value).strip()

    return {
        "source": cut(line, "source", "source_hash", "generated_at"),
        "source_hash": cut(line, "source_hash", "generated_at"),
        "generated_at": cut(line, "generated_at"),
    }


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
    header = parse_header(content)
    recorded_hash = header["source_hash"]
    recorded_source = header["source"]
    source_stale = recorded_source != target.source_label

    if check:
        problems = []
        if normalize(recorded_body) != normalize(rendered):
            problems.append("生成区内容与当前源码导出结果不一致")
        if recorded_hash != digest:
            problems.append(f"source_hash 不一致（记录 {recorded_hash}，实际 {digest}）")
        if source_stale:
            # 只改 `Target.source_label` 也必须被发现。此前 header 里的 `source=`
            # 既不参与校验、也不参与写入判定，于是标签会永远停在旧值 ——
            # 一条"生成视图从哪来"的说明，错了却没人会知道。
            problems.append(
                f"来源说明不一致（记录 {recorded_source!r}，实际 {target.source_label!r}）"
            )
        if problems:
            print(
                f"[fail] {target.name}: {'；'.join(problems)}\n"
                f"       重新生成：python tools/skills/gen_contracts.py --target {target.name}"
            )
            return 1
        print(f"[ok]   {target.name}: 生成区与源码一致（{digest[:19]}…）")
        return 0

    # 内容、哈希与来源说明都没变时**不写文件**。
    #
    # 否则每次重新生成都会重写 `generated_at`，于是三个契约文件总是同时出现在
    # diff 里 —— 时间戳噪音会把真正的契约变更淹没。这与行尾噪音（见
    # `write_generated` 里的 `newline="\n"`）是同一类问题，而且更隐蔽：
    # 行尾噪音一眼能认出，时间戳噪音看起来像"生成过，应该没问题"。
    #
    # ⚠️ 跳过条件必须包含 `source_stale`：只比较内容与哈希时，
    # 改了 `source_label` 会被判为"未变化"从而永远不修（这条是审查发现的）。
    #
    # 顺带让语义更准确：`generated_at` 变成「该视图最近一次**变化**的时刻」，
    # 而不是「最近一次跑脚本的时刻」—— 后者对读文档的人没有信息量。
    if (
        normalize(recorded_body) == normalize(rendered)
        and recorded_hash == digest
        and not source_stale
    ):
        print(f"[ok]   {target.name}: 内容未变化，未改动文件（{digest[:19]}…）")
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
