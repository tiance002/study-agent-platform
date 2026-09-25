# 已核实的现状

- `docs/reviews/2026-09-23-code-architecture-audit.md` 中 A08 尚未完成，A13 仅有 15 秒缓存，A12 保留历史资料；`api/workers -> main` 仍在导入清单中。
- `tools/run_round8_gate.py` 只对 `frontend/app.js` 执行 `node --check`，未覆盖 `api-client.js` 和 `views.js`。
- `backend/app/api/product_routes.py` 混合会话、计划、资料、知识检索和学习接口；`frontend/app.js` 的 `App` 仍管理多领域状态；`workflow/runtime.py` 包含幂等、预算和失败收尾。
- 指标 SQL 函数聚合 `acquisition_jobs`、`acquisition_fetch_observations`、`teaching_runs` 和 `provider_attempts`；单纯 `EXPLAIN SELECT public.study_metrics_snapshot()` 不能充分展示函数内部各查询的代价，需分段执行计划与整体耗时共同测量。
- `backend/tests/pg_support.py` 提供 `temp_test_database()` 和测试库名前缀保护，性能实验不得使用业务库。
- 历史轮次脚本被旧计划引用，`tools/run_round8_gate.py` 仍是现存门禁入口；未跟踪的 Trae 文件需保留。
