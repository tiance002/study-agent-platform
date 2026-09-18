"""node 与 tool 的声明模型。

字段的取舍原则：**凡是用于边界判定的属性都必须显式声明**，不允许靠命名或文档约定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# 权限轴定义在 `core/authority.py`（策略层与注册表都要用，属跨层共享概念）。
# 此处再导出，保持既有导入点 `from app.registry.models import Authority` 可用。
from app.core.authority import Authority  # noqa: F401


class SinkClass(StrEnum):
    """工具结果的去向。用于区分「同名不同用途」，是重叠检测的第二个维度。"""

    CONTEXT = "context"      # 进入模型上下文
    ARTIFACT = "artifact"    # 落对象存储
    EXTERNAL = "external"    # 作用于外部系统
    FACT = "fact"            # 写业务事实（只允许追加型）


class Exclusivity(StrEnum):
    """是否允许其他工具完成同一意图。"""

    EXCLUSIVE = "exclusive"
    COMPOSABLE = "composable"


class IdempotencyLevel(StrEnum):
    """幂等能力阶梯。顺序即从强到弱。"""

    NATIVE = "native"                    # 原生幂等
    QUERYABLE = "queryable"              # 可查询对账
    CONDITIONAL_WRITE = "conditional_write"
    COMPENSABLE = "compensable"          # 可补偿，需标注是否语义等价
    UNSAFE_TO_RETRY = "unsafe_to_retry"  # 不可安全重试


# 允许进入 A2/A3 的幂等语义集合。其余一律最高 A1。
IDEMPOTENT_ENOUGH_FOR_WRITE = frozenset(
    {
        IdempotencyLevel.NATIVE,
        IdempotencyLevel.QUERYABLE,
        IdempotencyLevel.CONDITIONAL_WRITE,
        IdempotencyLevel.COMPENSABLE,
    }
)


class ModelTier(StrEnum):
    L0 = "L0"   # 确定性程序
    L1 = "L1"   # 本地小模型
    L2 = "L2"   # 云端模型


@dataclass(frozen=True)
class ToolSpec:
    """工具声明。缺失任一用于边界判定的字段即拒绝注册。"""

    tool_id: str
    intent_tag: str
    sink_class: SinkClass
    owner_module: str
    min_authority: Authority
    exclusivity: Exclusivity
    idempotency: IdempotencyLevel
    params_schema: dict = field(default_factory=dict)
    # 网络与文件边界。空表示不访问。
    network_domains: tuple[str, ...] = ()
    filesystem_paths: tuple[str, ...] = ()
    # 是否返回外部内容（返回即强制 taint）
    returns_external_content: bool = False
    # 是否消耗资源（沙箱秒数、外部配额等）
    is_consumptive: bool = False
    requires_audit: bool = True
    # 单次调用的成本上界。预算预留必须用上界，结算才用实际值。
    max_cost_units: int = 1
    description: str = ""

    @property
    def intent_key(self) -> tuple[str, str, int]:
        """重叠检测主键：意图 + 去向 + 权限档位。"""
        return (self.intent_tag, str(self.sink_class), int(self.min_authority))


@dataclass(frozen=True)
class NodeSpec:
    """node 声明。上限用于保证循环必然退出（05/06 号文档的退出条件 1）。"""

    node_id: str
    input_schema: str
    output_schema: str
    allowed_tools: tuple[str, ...]
    min_tier: ModelTier
    authority_ceiling: Authority
    validator_id: str | None = None
    declared_skills: tuple[str, ...] = ()
    # 退出条件：任一到顶即必须终止，不允许"再试一次"
    max_tool_calls: int = 8
    max_loops: int = 4
    max_recursion: int = 1
    max_concurrency: int = 2
    # 预算上限（单位见 budget 模块）
    max_steps: int = 12
    max_tokens: int = 200_000
    max_cost_micros: int = 5_000_000
    max_sandbox_seconds: int = 0

    def tool_limit(self) -> int:
        return min(self.max_tool_calls, len(self.allowed_tools) * self.max_loops)
