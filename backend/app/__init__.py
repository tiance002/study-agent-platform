"""Agent 工程学习规划平台 —— 控制面（批次一最小闭环，开发适配器版）。

分层（见 06 号规格 §1）：

- L1 接入：`api/`
- L2 边界：`tenancy/`（身份与租户上下文）、`policy/`（Policy Gateway）
- L3 编排：`workflow/`、`learning/`（计划与投影写入）
- L4 能力：`registry/`、`knowledge/`、`execution/`
- L5 事实：由 `tenancy/ports.py` 定义、`tenancy/memory_store.py` 实现的适配器
- 旁路：`audit/`

**依赖方向单向**：上层可依赖下层，反向依赖由 import 方向测试机械拒绝。
"""
