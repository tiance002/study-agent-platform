# 技术债登记簿

> 按 ship-first 技能 §16 记录 P2/P3 问题：记录而不是立即修，不阻塞当前里程碑。
> TD 编号全局递增，不回收复用。

## TD-001 study_metrics_snapshot() 在百万事实规模下 p95 超出 250 ms 目标

- 状态：已评估，暂缓实施（保留现有 SQL 聚合 + 15 秒缓存实现）
- 影响：100 万事实规模下快照 p95 = 666.7 ms（目标 250 ms）；10 万规模内 p95 = 21.1 ms 不受影响。/metrics 抓取经 15 秒 TTL 缓存，数据库查询频率上限 4 次/分钟/进程，当前不构成用户可感知问题。
- 触发条件：任一事实表（acquisition_jobs / acquisition_fetch_observations / teaching_runs / provider_attempts）接近 50 万行；或生产配置下实测 /metrics 数据库耗时 p95 > 250 ms；或需要把缓存 TTL 降到 5 秒以下。
- 当前临时方案：保留 0018 迁移的 SECURITY DEFINER SQL 聚合函数与 PostgresMetricsStore 的 15 秒缓存。
- 为什么现在不修：达标需增量汇总表（新迁移 + 获取/教学写路径计数更新 + 回填 + 并发 + 回滚 + 一致性 PG 测试），blast radius 大；当前真实数据量级在 10 万以内且缓存已把数据库占用钳制在 ~4.4% 单核；属 ship-first §11"极限性能优化需真实 workload"范畴。
- 后续验证方法：`tools/bench_metrics_snapshot.py --scales 10000 100000 1000000`（详见 `docs/performance/metrics-snapshot-2026-09.md`）。
- 建议处理阶段：Phase D（Latency 专项调优）；过渡项可先做函数内合并重复扫描（预计降至 350–400 ms，仍不达标，仅在需要小幅改善时单独评估）。
- 标记：ship-first、architecture-followup 任务 6
