"""import 方向测试：层间依赖必须单向，且 L4 模块之间不得横向互调。

设计依据：
- 06 号规格 §2.1 —— 禁止反向依赖；**禁止 L4 模块之间横向调用**（否则策略、预算、
  审计的注入点会变成两处，等于多开一条绕过路径）。
- 06 号规格 §4.2 —— CI 必须拦截的四类违规中的第 1 类。
- 实施计划任务 1 —— 「CI 能机械拒绝反向依赖」。

实现方式：静态解析源码的 import 语句，不做运行时导入，因此与被测模块的副作用无关。
这比"约定大家都别写错"可靠得多。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# ---- 分层定义（06 号规格 §1）------------------------------------------------
# 数值越小越底层。依赖只允许指向不高于自己的层。
LAYER = {
    "core": 0,
    "tenancy": 1,      # 租户上下文是跨层共享的基础设施
    "policy": 1,       # 同理：任何层都要能读策略与 token
    "budget": 1,
    "audit": 1,
    "registry": 2,     # 能力基础：只描述边界，不做事
    "knowledge": 3,    # 能力实现
    "execution": 3,
    "learning": 4,     # 领域读模型
    "workflow": 5,     # 编排
    "api": 6,          # 接入
    # 驱动层（后台 worker）：与接入层同级 —— 它可以依赖下面所有层，
    # 而下面任何层都不得依赖它。与接入层并列而不是更高，是因为两者都是
    # "外部世界的入口"，互不隶属（worker 不该导入 HTTP 层）。
    "workers": 6,
}

# L4 内部的横向依赖白名单。空集合是刻意的：任何新增都必须先讨论。
LATERAL_ALLOWED: dict[tuple[str, str], str] = {
    ("execution", "registry"): "执行器必须查注册表才能知道工具边界",
}

# Current package dependency boundary. Entries are explicit so a new package or
# cross-package edge must be reviewed instead of silently escaping the layer test.
# `platform` is the composition root: it may import any non-HTTP layer, and
# api/workers depend on it instead of the HTTP entry `main`.
ALLOWED_IMPORTS: dict[str, set[str]] = {
    "api": {"audit", "budget", "core", "deployment", "execution", "identity", "knowledge", "learning", "platform", "policy", "product", "teaching", "tenancy", "workflow"},
    "audit": {"core"},
    "budget": {"core"},
    "core": set(),
    "db": {"audit", "budget", "core", "execution", "identity", "knowledge", "learning", "policy", "product", "teaching"},
    "deployment": {"db", "identity"},
    "execution": {"audit", "budget", "core", "policy", "registry"},
    "identity": {"audit", "core"},
    "knowledge": {"core", "identity", "policy", "product", "tenancy"},
    "learning": {"core", "identity", "product"},
    "main": {"api", "core", "metrics", "platform"},
    "metrics": set(),
    "platform": {"audit", "budget", "core", "db", "deployment", "execution", "identity", "knowledge", "learning", "policy", "product", "registry", "teaching", "workflow"},
    "policy": {"core"},
    "product": {"core", "identity"},
    "registry": {"core"},
    "teaching": {"budget", "core", "deployment", "identity", "knowledge", "product"},
    "tenancy": {"core"},
    "workers": {"core", "deployment", "identity", "knowledge", "platform", "policy", "teaching"},
    "workflow": {"audit", "budget", "core", "execution", "knowledge", "learning", "policy", "registry", "tenancy"},
}


def module_files() -> list[Path]:
    return sorted(path for path in APP_ROOT.rglob("*.py") if path.name != "__init__.py")


def module_name(path: Path) -> str:
    return path.relative_to(APP_ROOT).with_suffix("").parts[0]


@pytest.mark.invariant
def test_all_packages_and_dependency_edges_are_reviewed():
    modules = {
        name.removesuffix(".py")
        for path in APP_ROOT.rglob("*.py")
        if (name := path.relative_to(APP_ROOT).parts[0]) != "__init__.py"
    }
    assert modules == ALLOWED_IMPORTS.keys(), (
        f"模块清单变化：新增 {modules - ALLOWED_IMPORTS.keys()}，"
        f"已删除 {ALLOWED_IMPORTS.keys() - modules}"
    )
    violations = [
        f"{path.relative_to(APP_ROOT)} -> {target}"
        for path in module_files()
        for target in imported_app_modules(path)
        if target != module_name(path).removesuffix(".py")
        and target not in ALLOWED_IMPORTS[module_name(path).removesuffix(".py")]
    ]
    assert not violations, "未审查的跨模块导入：\n" + "\n".join(violations)


def imported_app_modules(path: Path) -> set[str]:
    """解析一个文件里所有 `app.x` 形式的导入目标（含函数内延迟导入）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    found.add(alias.name.split(".")[1])
    return found


def imported_app_paths(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(
                alias.name for alias in node.names if alias.name.startswith("app.")
            )
    return found


@pytest.mark.invariant
def test_no_upward_dependencies():
    """任何模块都不得依赖比自己更高的层。"""
    violations: list[str] = []
    for path in module_files():
        source_layer = LAYER.get(module_name(path))
        if source_layer is None:
            continue
        for target in imported_app_modules(path):
            target_layer = LAYER.get(target)
            if target_layer is None or target == module_name(path):
                continue
            if target_layer > source_layer:
                violations.append(
                    f"{path.relative_to(APP_ROOT)}: {module_name(path)}(L{source_layer}) "
                    f"→ {target}(L{target_layer})"
                )
    assert not violations, "存在向上依赖：\n" + "\n".join(violations)


@pytest.mark.invariant
def test_no_lateral_dependencies_between_capability_modules():
    """06 号规格 §2.1：L4 能力模块之间不得横向调用。"""
    violations: list[str] = []
    for path in module_files():
        source = module_name(path)
        if LAYER.get(source) != 3:
            continue
        for target in imported_app_modules(path):
            if target == source or LAYER.get(target) != 3:
                continue
            if (source, target) not in LATERAL_ALLOWED:
                violations.append(f"{path.relative_to(APP_ROOT)}: {source} → {target}")
    assert not violations, (
        "存在 L4 横向依赖（应把共享契约下沉到 core）：\n" + "\n".join(violations)
    )


@pytest.mark.invariant
def test_core_has_no_internal_dependencies():
    """`core` 是零依赖基座，不得依赖任何 app 内模块。"""
    violations: list[str] = []
    for path in sorted((APP_ROOT / "core").glob("*.py")):
        for target in imported_app_modules(path):
            violations.append(f"{path.name} → {target}")
    assert not violations, "core 出现了内部依赖：\n" + "\n".join(violations)


@pytest.mark.invariant
def test_shared_contracts_live_in_core():
    """谱系引用这类跨层契约必须住在 core，而不是某个 L4 模块里。

    这条测试拦住的是「图省事在 knowledge 里定义、再让 execution 去导入」这种写法。
    """
    from app.core.artifacts import ArtifactRef, DisplayPolicy

    assert ArtifactRef.__module__ == "app.core.artifacts"
    assert DisplayPolicy.__module__ == "app.core.artifacts"


def test_memory_mode_import_does_not_require_psycopg():
    code = textwrap.dedent(
        """
        import importlib.abc
        import sys

        class BlockPsycopg(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "psycopg" or fullname.startswith("psycopg."):
                    raise ModuleNotFoundError("psycopg intentionally unavailable")
                return None

        sys.meta_path.insert(0, BlockPsycopg())
        import app.main
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(APP_ROOT.parent)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.invariant
def test_entry_packages_do_not_import_http_composition_root():
    """`api/` 与 `workers/` 不得静态导入 `app.main`（HTTP 组成根）。

    `app.main` 在导入期就会创建应用实例（`app = create_app()`）。
    worker CLI 或路由模块导入它，等于让"后台进程/路由层"反过来拖起整个
    Web 入口。平台装配应从 `app.platform` 取得。
    """
    violations = [
        f"{path.relative_to(APP_ROOT)} -> {target}"
        for package in ("api", "workers")
        for path in sorted((APP_ROOT / package).rglob("*.py"))
        for target in imported_app_paths(path)
        if target == "app.main" or target.startswith("app.main.")
    ]
    assert not violations, "api/workers 反向依赖 app.main：\n" + "\n".join(violations)


@pytest.mark.invariant
def test_adapter_boundaries_do_not_regress_to_direct_cycles():
    violations: list[str] = []
    forbidden_package_edges = {
        "api": "app.db",
        "audit": "app.db",
        "identity": "app.product",
    }
    for source, forbidden in forbidden_package_edges.items():
        for path in sorted((APP_ROOT / source).rglob("*.py")):
            for target in imported_app_paths(path):
                if target == forbidden or target.startswith(forbidden + "."):
                    violations.append(f"{path.relative_to(APP_ROOT)} -> {target}")

    learning_store = APP_ROOT / "db" / "learning_store.py"
    if "app.learning.loop_store" in imported_app_paths(learning_store):
        violations.append("db/learning_store.py -> app.learning.loop_store")

    assert not violations, "适配器边界发生回退：\n" + "\n".join(violations)


@pytest.mark.invariant
def test_database_adapters_do_not_import_http_layer():
    violations = [
        f"{path.relative_to(APP_ROOT)} -> {target}"
        for path in (APP_ROOT / "db").rglob("*.py")
        for target in imported_app_paths(path)
        if target == "app.api" or target.startswith("app.api.")
    ]
    assert not violations, "数据适配器依赖 HTTP 接入层：\n" + "\n".join(violations)
