"""node / tool 注册表：声明模型与机械门。

设计依据：
- 03 号规格 §4 —— node 与工具注册表、版本化、未知 node 拒绝执行。
- 05 号规格 §5.2 —— 工具声明 `intent_tag` / `sink_class` / `owner_module` /
  `min_authority` / `exclusivity`，用于边界判定与重叠检测。
- 05 号规格 §6.3 —— 无幂等声明者最高只给 A1。

本包只依赖标准库与 `core`。
"""
