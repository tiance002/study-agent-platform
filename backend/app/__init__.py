"""Agent 工程学习规划平台。

`main` 是应用组成根；`api` 与 `workers` 是入口。领域端口分布在各领域包的
`ports.py`，`db` 提供 PostgreSQL 适配器，`core` 提供共享基础契约。
跨包导入由 `tests/test_import_direction.py` 的显式清单约束。
"""
