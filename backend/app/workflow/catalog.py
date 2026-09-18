"""内置 node 与 tool 声明。

这些声明是运行时的**边界事实**：某个工具能做什么、能碰什么、需要什么权限，
全部在这里显式写明，不依赖命名习惯或文档约定。

注册时会经过三道机械门（见 `registry/registry.py`）：
必填字段门、重叠门、写入安全门；另有单 node 工具数上限。
"""

from __future__ import annotations

from app.registry.models import (
    Authority,
    Exclusivity,
    IdempotencyLevel,
    ModelTier,
    NodeSpec,
    SinkClass,
    ToolSpec,
)
from app.registry.registry import Registry

# --------------------------------------------------------------------- 工具

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        tool_id="retrieve_project_chunks",
        intent_tag="retrieve_chunk",
        sink_class=SinkClass.CONTEXT,
        owner_module="knowledge",
        min_authority=Authority.A1A,
        exclusivity=Exclusivity.EXCLUSIVE,
        idempotency=IdempotencyLevel.NATIVE,
        returns_external_content=True,
        max_cost_units=3,
        description="按查询召回项目内的相关资料片段",
    ),
    ToolSpec(
        tool_id="read_source_span",
        intent_tag="read_source_span",
        sink_class=SinkClass.CONTEXT,
        owner_module="knowledge",
        min_authority=Authority.A1A,
        exclusivity=Exclusivity.EXCLUSIVE,
        idempotency=IdempotencyLevel.NATIVE,
        returns_external_content=True,
        description="已知 source_id 与行范围时精确回读原文",
    ),
    ToolSpec(
        tool_id="fetch_external_url",
        intent_tag="fetch_external",
        sink_class=SinkClass.CONTEXT,
        owner_module="knowledge",
        min_authority=Authority.A1C,
        exclusivity=Exclusivity.EXCLUSIVE,
        idempotency=IdempotencyLevel.NATIVE,
        network_domains=("docs.python.org", "fastapi.tiangolo.com"),
        returns_external_content=True,
        max_cost_units=2,
        description="经独立 Fetcher 抓取白名单域名内容，结果强制带 taint",
    ),
    ToolSpec(
        tool_id="query_competency_graph",
        intent_tag="read_graph",
        sink_class=SinkClass.CONTEXT,
        owner_module="learning",
        min_authority=Authority.A1A,
        exclusivity=Exclusivity.EXCLUSIVE,
        idempotency=IdempotencyLevel.NATIVE,
        max_cost_units=4,
        description="读能力组件与显式先修关系",
    ),
    ToolSpec(
        tool_id="run_validator",
        intent_tag="validate_artifact",
        sink_class=SinkClass.CONTEXT,
        owner_module="learning",
        min_authority=Authority.A1A,
        exclusivity=Exclusivity.EXCLUSIVE,
        idempotency=IdempotencyLevel.NATIVE,
        description="运行确定性验证器，判断产物是否合格",
    ),
    ToolSpec(
        tool_id="run_in_sandbox",
        intent_tag="execute_code",
        sink_class=SinkClass.ARTIFACT,
        owner_module="execution",
        min_authority=Authority.A1D,
        exclusivity=Exclusivity.EXCLUSIVE,
        idempotency=IdempotencyLevel.QUERYABLE,
        is_consumptive=True,
        description="一次性隔离环境中执行用户代码（本版为占位实现，未接入真实沙箱）",
    ),
    ToolSpec(
        tool_id="append_project_evidence",
        intent_tag="append_evidence",
        sink_class=SinkClass.FACT,
        owner_module="learning",
        min_authority=Authority.A2,
        exclusivity=Exclusivity.EXCLUSIVE,
        # A2 必须能对账或补偿，否则拒绝注册 —— 这里靠 native 幂等通过机械门。
        idempotency=IdempotencyLevel.NATIVE,
        description="追加一条证据事实（只允许追加）",
    ),
)

# --------------------------------------------------------------------- node

NODE_SPECS: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="intake_goal",
        input_schema="IntakeGoalIn/v1",
        output_schema="IntakeGoalOut/v1",
        allowed_tools=(),
        min_tier=ModelTier.L2,
        authority_ceiling=Authority.A0,
        declared_skills=("domain-model", "sql-schema"),
        max_tool_calls=0,
        max_loops=1,
        max_steps=2,
        max_tokens=20_000,
        max_cost_micros=200_000,
    ),
    NodeSpec(
        node_id="diagnose_prerequisites",
        input_schema="DiagnoseIn/v1",
        output_schema="DiagnoseOut/v1",
        allowed_tools=("query_competency_graph",),
        min_tier=ModelTier.L0,
        authority_ceiling=Authority.A1A,
        validator_id="validator/prereq-closure/v1",
        declared_skills=("evidence-rules",),
        max_tool_calls=4,
        max_loops=2,
        max_steps=6,
        max_tokens=40_000,
        max_cost_micros=400_000,
    ),
    NodeSpec(
        node_id="retrieve_material",
        input_schema="RetrieveIn/v1",
        output_schema="RetrieveOut/v1",
        allowed_tools=("retrieve_project_chunks", "read_source_span", "fetch_external_url"),
        min_tier=ModelTier.L0,
        authority_ceiling=Authority.A1C,
        declared_skills=("egress-rules", "protocol"),
        max_tool_calls=6,
        max_loops=3,
        max_steps=8,
        max_tokens=80_000,
        max_cost_micros=800_000,
    ),
    NodeSpec(
        node_id="validate_and_record",
        input_schema="ValidateIn/v1",
        output_schema="ValidateOut/v1",
        allowed_tools=("run_validator", "append_project_evidence"),
        min_tier=ModelTier.L0,
        authority_ceiling=Authority.A2,
        validator_id="validator/rubric/v1",
        declared_skills=("evidence-rules", "tool-catalog"),
        max_tool_calls=4,
        max_loops=2,
        max_steps=6,
        max_tokens=40_000,
        max_cost_micros=300_000,
    ),
)


def build_registry() -> Registry:
    """构建并冻结内置注册表。冻结后版本号固定，运行可归因。"""
    registry = Registry()
    for spec in TOOL_SPECS:
        registry.register_tool(spec)
    for node in NODE_SPECS:
        registry.register_node(node)
    registry.freeze()
    return registry
