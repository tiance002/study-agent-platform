"""node / tool 注册表的机械门证明。

对应 05 号规格 §5.2 的重叠门与 §6.3 的写入安全门，以及 03 号规格 §4 的注册表约束。
"""

from __future__ import annotations

import pytest
from app.core.errors import ErrorCode, PlatformError
from app.registry.models import (
    Authority,
    Exclusivity,
    IdempotencyLevel,
    ModelTier,
    NodeSpec,
    SinkClass,
    ToolSpec,
)
from app.registry.registry import MAX_TOOLS_PER_NODE, Registry
from app.workflow.catalog import build_registry


def tool(tool_id: str, **overrides) -> ToolSpec:
    base = dict(
        tool_id=tool_id,
        intent_tag=f"intent_{tool_id}",
        sink_class=SinkClass.CONTEXT,
        owner_module="testing",
        min_authority=Authority.A1A,
        exclusivity=Exclusivity.EXCLUSIVE,
        idempotency=IdempotencyLevel.NATIVE,
    )
    base.update(overrides)
    return ToolSpec(**base)


def node(node_id: str, tools: tuple[str, ...], **overrides) -> NodeSpec:
    base = dict(
        node_id=node_id,
        input_schema="In/v1",
        output_schema="Out/v1",
        allowed_tools=tools,
        min_tier=ModelTier.L0,
        authority_ceiling=Authority.A1C,
    )
    base.update(overrides)
    return NodeSpec(**base)


# ------------------------------------------------------------------ 重叠门


@pytest.mark.invariant
def test_same_intent_cannot_be_implemented_twice():
    """05 号规格 §5.2：同一 (intent_tag, sink_class, min_authority) 下不允许两个独占工具。

    这是「模型不知道该调哪个」的机械拦截点。
    """
    registry = Registry()
    registry.register_tool(tool("search_v1", intent_tag="retrieve_chunk"))
    with pytest.raises(PlatformError) as exc:
        registry.register_tool(tool("search_v2", intent_tag="retrieve_chunk"))
    assert exc.value.code is ErrorCode.TOOL_INTENT_CONFLICT
    assert exc.value.details["existing"] == "search_v1"


@pytest.mark.invariant
def test_different_sink_class_does_not_conflict():
    """同名意图但去向不同（context vs artifact）不算冲突。"""
    registry = Registry()
    registry.register_tool(tool("a", intent_tag="produce", sink_class=SinkClass.CONTEXT))
    registry.register_tool(tool("b", intent_tag="produce", sink_class=SinkClass.ARTIFACT))


@pytest.mark.invariant
def test_composable_tools_may_coexist():
    """显式声明 composable 的工具允许共用同一意图。"""
    registry = Registry()
    registry.register_tool(
        tool("a", intent_tag="enrich", exclusivity=Exclusivity.COMPOSABLE)
    )
    registry.register_tool(
        tool("b", intent_tag="enrich", exclusivity=Exclusivity.COMPOSABLE)
    )


# ------------------------------------------------------- 写入安全门 / 必填字段


@pytest.mark.invariant
def test_write_tool_without_idempotency_is_rejected():
    """05 号规格 §6.3：无幂等声明者最高只给 A1。"""
    registry = Registry()
    with pytest.raises(PlatformError) as exc:
        registry.register_tool(
            tool("dangerous", min_authority=Authority.A2, idempotency=IdempotencyLevel.UNSAFE_TO_RETRY)
        )
    assert exc.value.code is ErrorCode.TOOL_DECLARATION_INVALID


@pytest.mark.invariant
def test_network_tool_requires_external_read_authority():
    """访问外部网络至少需要 A1c。"""
    registry = Registry()
    with pytest.raises(PlatformError) as exc:
        registry.register_tool(
            tool("fetch", min_authority=Authority.A1A, network_domains=("example.com",))
        )
    assert exc.value.code is ErrorCode.TOOL_DECLARATION_INVALID


@pytest.mark.invariant
@pytest.mark.parametrize("field", ["intent_tag", "owner_module"])
def test_missing_declaration_field_is_rejected(field):
    """用于边界判定的字段缺失即拒绝注册。"""
    registry = Registry()
    with pytest.raises(PlatformError) as exc:
        registry.register_tool(tool("t1", **{field: ""}))
    assert exc.value.code is ErrorCode.TOOL_DECLARATION_INVALID


@pytest.mark.invariant
def test_empty_tool_id_is_rejected():
    """工具标识不能为空。"""
    registry = Registry()
    with pytest.raises(PlatformError) as exc:
        registry.register_tool(tool(""))
    assert exc.value.code is ErrorCode.TOOL_DECLARATION_INVALID


# ------------------------------------------------------------------ node 约束


@pytest.mark.invariant
def test_node_cannot_reference_unregistered_tool():
    registry = Registry()
    with pytest.raises(PlatformError) as exc:
        registry.register_node(node("n1", ("ghost_tool",)))
    assert exc.value.code is ErrorCode.TOOL_NOT_REGISTERED


@pytest.mark.invariant
def test_node_ceiling_must_cover_its_tools():
    """node 的权限上限不得低于其工具所需权限。"""
    registry = Registry()
    registry.register_tool(tool("writer", min_authority=Authority.A2))
    with pytest.raises(PlatformError) as exc:
        registry.register_node(
            node("n1", ("writer",), authority_ceiling=Authority.A1A)
        )
    assert exc.value.code is ErrorCode.TOOL_NOT_ALLOWED_FOR_NODE


@pytest.mark.invariant
def test_node_tool_count_is_capped():
    """05 号规格 §3.2：单 node 常驻工具数超过 8 说明职责过宽。"""
    registry = Registry()
    tool_ids = []
    for index in range(MAX_TOOLS_PER_NODE + 1):
        tool_id = f"t{index}"
        registry.register_tool(tool(tool_id))
        tool_ids.append(tool_id)
    with pytest.raises(PlatformError) as exc:
        registry.register_node(node("wide", tuple(tool_ids)))
    assert exc.value.code is ErrorCode.TOOL_DECLARATION_INVALID


@pytest.mark.invariant
def test_node_limits_must_be_positive():
    """六类退出条件中的第 1 类：上限必须为正，否则循环无法保证退出。"""
    registry = Registry()
    with pytest.raises(PlatformError) as exc:
        registry.register_node(node("bad", (), max_loops=0))
    assert exc.value.code is ErrorCode.TOOL_DECLARATION_INVALID


@pytest.mark.invariant
def test_tool_call_limit_may_be_zero_for_pure_explanation_node():
    """不调工具的 node（如纯解释）应允许 max_tool_calls = 0。"""
    registry = Registry()
    registry.register_node(node("explain", (), max_tool_calls=0))


@pytest.mark.invariant
def test_unknown_node_is_rejected():
    """不变量 #5：未注册的 node 默认拒绝。"""
    registry = build_registry()
    with pytest.raises(PlatformError) as exc:
        registry.node("totally_unknown")
    assert exc.value.code is ErrorCode.NODE_NOT_REGISTERED


# ------------------------------------------------------------------ 冻结与版本


@pytest.mark.invariant
def test_registry_cannot_change_after_freeze():
    """注册表冻结后不得再改，运行必须固定版本（不变量 #13）。"""
    registry = build_registry()
    with pytest.raises(PlatformError) as exc:
        registry.register_tool(tool("late_addition"))
    assert exc.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION


@pytest.mark.invariant
def test_registry_version_is_reproducible():
    """相同内容必得相同版本号，运行才可归因。"""
    assert build_registry().version == build_registry().version


@pytest.mark.invariant
def test_tools_are_loaded_per_node():
    """05 号规格 §3.2：工具 schema 按需加载，低权限 node 不接收无关描述。"""
    registry = build_registry()
    diagnose_tools = {spec.tool_id for spec in registry.tools_for_node("diagnose_prerequisites")}
    assert diagnose_tools == {"query_competency_graph"}
    assert "append_project_evidence" not in diagnose_tools


@pytest.mark.invariant
def test_builtin_catalog_uses_distinct_intents():
    """内置工具目录本身不得有意图冲突（靠注册时机械门保证）。"""
    registry = build_registry()
    seen = {}
    for tool_id in registry.tool_ids():
        spec = registry.tool(tool_id)
        assert spec.intent_key not in seen, f"{tool_id} 与 {seen.get(spec.intent_key)} 意图冲突"
        seen[spec.intent_key] = tool_id
