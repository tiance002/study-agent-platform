"""持久化教学预算的领域层：纯不变量 + 价格快照。

## 为什么这一层没有 I/O

预算最容易错的地方不是 SQL，而是**记账规则**：容量判定、状态迁移、
金额换算。把它们收成纯函数，内存适配器与 PostgreSQL 适配器跑的是
**同一份规则**（契约测试在两个实现上各跑一遍同一套断言），
而不是"两份代码碰巧一样"。

## 记账纪律

- **整数微单位**（micro = 计费货币的 10^-6）。浮点累计会丢分位，
  钱的事实不允许 `0.1 + 0.2` 问题。
- **价格快照带版本**：改价 = 新版本号。历史预留行记着**当时的**版本
  与单价换算出的金额，改价不改历史。
- **三种持有状态**：`held`（额度已扣、还没派发）/ `in_flight`
  （已派发，结果未知）/ `settled` / `released`。费用敞口 =
  `reserved + in_flight` 的和；只有 `settled` 才算真花了钱。
- **缺 usage 不等于零费用**：结算必须有权威用量；拿不到就保持
  `in_flight`（敞口保留，进对账），绝不静默释放或按 0 结算。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.errors import ErrorCode, PlatformError

#: 价格快照版本。模拟期单价是占位值（见下）；接入真实 provider 后
#: 以新版本号落新的单价，历史行仍记旧版本。
PRICE_VERSION = "teaching-price/v1"

#: 模拟单价（微单位 / token）。刻意非零：零单价会让"漏结算"在测试里
#: 不可见（金额全是 0，加不加都一样）。
INPUT_MICRO_PER_TOKEN = 1
OUTPUT_MICRO_PER_TOKEN = 2


def estimate_micro(input_tokens: int, output_tokens: int) -> int:
    """按当前价格快照换算微单位。输入必须非负。"""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token 数不能为负")
    return input_tokens * INPUT_MICRO_PER_TOKEN + output_tokens * OUTPUT_MICRO_PER_TOKEN


def usage_to_micro(input_tokens: int, output_tokens: int) -> int:
    """provider 报告的权威用量 → 微单位（结算用）。与估价同一公式。"""
    return estimate_micro(input_tokens, output_tokens)


class ReservationState(StrEnum):
    """一笔预留的生命周期。迁移合法性由 `transition` 判定（唯一出口）。"""

    HELD = "held"
    IN_FLIGHT = "in_flight"
    SETTLED = "settled"
    RELEASED = "released"

    @property
    def is_terminal(self) -> bool:
        return self in (ReservationState.SETTLED, ReservationState.RELEASED)


def transition(current: ReservationState, next_state: ReservationState) -> ReservationState:
    """预留状态机的**唯一**判定出口。非法迁移抛 BUDGET 错误。"""
    allowed: dict[ReservationState, set[ReservationState]] = {
        ReservationState.HELD: {ReservationState.IN_FLIGHT, ReservationState.RELEASED},
        # in_flight → released 只有一种合法情形：可证明 provider 未收到请求
        # （DISPATCH_FAILED）。结果未知时绝不能释放 —— 那是无声的零费用。
        ReservationState.IN_FLIGHT: {ReservationState.SETTLED, ReservationState.RELEASED},
        ReservationState.SETTLED: set(),
        ReservationState.RELEASED: set(),
    }
    if next_state not in allowed[current]:
        raise PlatformError(
            ErrorCode.BUDGET_TREE_INVALID,
            f"预留状态不能从 {current} 迁移到 {next_state}",
        )
    return next_state


@dataclass(frozen=True)
class BudgetAccountFacts:
    """一个预算账户的当前事实（租户级或项目级）。"""

    scope: str  # "tenant" | "project"
    total_micro: int
    reserved_micro: int
    in_flight_micro: int
    spent_micro: int
    price_version: str

    @property
    def exposure_micro(self) -> int:
        """费用敞口 = 已扣未结（含 in_flight 的未知结果）。"""
        return self.reserved_micro + self.in_flight_micro

    @property
    def available_micro(self) -> int:
        return self.total_micro - self.exposure_micro - self.spent_micro

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "total_micro": self.total_micro,
            "reserved_micro": self.reserved_micro,
            "in_flight_micro": self.in_flight_micro,
            "spent_micro": self.spent_micro,
            "exposure_micro": self.exposure_micro,
            "available_micro": self.available_micro,
            "price_version": self.price_version,
        }


def assert_capacity(
    tenant: BudgetAccountFacts,
    project: BudgetAccountFacts,
    *,
    estimated_micro: int,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    max_input_tokens: int,
    max_output_tokens: int,
) -> None:
    """预留前的**唯一**容量判定（两个适配器共用）。

    四道闸，各自拒绝一种超支：

    1. 单次输入 token 超过项目上限（上界不是"实际用量"，是**承诺上限**）；
    2. 单次输出 token 超过项目上限；
    3. 项目账户余额不足；
    4. 租户账户余额不足（跨项目聚合的物化行，同一事务里一起扣）。

    判定失败是 `BUDGET_EXCEEDED` —— 它与"参数不合法"是两回事：
    前者可重试（等别的运行结算后额度回来），后者重试一百次也一样。
    """
    if estimated_input_tokens > max_input_tokens:
        raise PlatformError(
            ErrorCode.BUDGET_EXCEEDED,
            f"输入 token 估计上界 {estimated_input_tokens} 超过项目上限 {max_input_tokens}",
        )
    if estimated_output_tokens > max_output_tokens:
        raise PlatformError(
            ErrorCode.BUDGET_EXCEEDED,
            f"输出 token 上界 {estimated_output_tokens} 超过项目上限 {max_output_tokens}",
        )
    if estimated_micro > project.available_micro:
        raise PlatformError(
            ErrorCode.BUDGET_EXCEEDED,
            "项目预算不足：拒绝本次预留（不产生负额度）",
        )
    if estimated_micro > tenant.available_micro:
        raise PlatformError(
            ErrorCode.BUDGET_EXCEEDED,
            "租户总预算不足：拒绝本次预留（不产生负额度）",
        )
