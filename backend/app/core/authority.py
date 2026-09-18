"""权限轴 A0–A3。

**为什么放在 `core` 而不是 `registry` 里**：策略网关要比较它，工具声明要声明它。
任何一方定义、另一方导入，都会造成层间反向依赖 —— import 方向测试会直接拒绝。
跨层共享的基础概念只能住在 `core`。

**铁律**：本轴与 L 模型档位、D 教学影响度互不相干，不得取数值最大值，
只能通过策略表映射（词汇表明确规定）。数值只用于比较大小。
"""

from __future__ import annotations

from enum import IntEnum


class Authority(IntEnum):
    """工具与副作用的权限轴。"""

    A0 = 0    # 不调用工具的回答
    A1A = 1   # 项目内、非敏感读取
    A1B = 2   # 敏感读取
    A1C = 3   # 外部网络读取
    A1D = 4   # 消耗型读取（沙箱秒数、外部配额）
    A2 = 5    # 可撤销或受控的写入与对外可见动作
    A3 = 6    # 不可逆、资金、删除或无法语义等价补偿的动作

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS: dict[Authority, str] = {
    Authority.A0: "A0",
    Authority.A1A: "A1a",
    Authority.A1B: "A1b",
    Authority.A1C: "A1c",
    Authority.A1D: "A1d",
    Authority.A2: "A2",
    Authority.A3: "A3",
}
