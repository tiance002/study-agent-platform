# study_metrics_snapshot() 容量实验报告（2026-09）

## 结论

**决定：保留现有实现（SQL 聚合函数 + 15 秒缓存），本轮不引入增量汇总表或新迁移。**

- 1 万 / 10 万事实规模下快照 p95 分别为 9.8 ms / 21.1 ms，远低于 250 ms 目标。
- 100 万事实规模下 p95 = 666.7 ms，**未达到** 250 ms 目标；但在 15 秒缓存约束下，
  每进程对数据库的查询频率被钳制在 ≤ 4 次/分钟，667 ms p95 对应约 4.4% 的单核占用，
  当前阶段不构成产品问题（触发条件与升级路径见文末执行计划）。
- 已按计划评估增量方案：仅做函数内合并扫描（当前对 acquisition_jobs、
  acquisition_fetch_observations、teaching_runs 各扫描两次）预计只能降到约 350–400 ms，
  无法达标；达标需要增量汇总表，属于大 blast radius 改动（写路径 + 回填 + 并发 + 回滚 +
  一致性测试），记为技术债 `TD-001`。

## 实验方法

- 工具：`tools/bench_metrics_snapshot.py`（可复现，见文末命令）。
- 隔离：`pg_support.temp_test_database()` 创建随机临时库（`study_test_` 前缀）并迁移到
  head；每次写入前显式 `require_test_database()`；实验后整库删除。**不连接业务库。**
- 规模定义：N 条事实 = acquisition_jobs N/4 + acquisition_fetch_observations N/2
  （每条 job 两次抓取观察）+ teaching_runs N/8 + provider_attempts N/8。
- 种子：合成租户/项目/会话/消息/来源父链，全部标签取自封闭集合
  （status/outcome/routing_decision/provider_family 等均按 CHECK 允许值分布），
  满足全部 CHECK/UNIQUE/FK 约束；三个规模在同一库内**累计**增长。
- 测量：每规模先 `ANALYZE`；以应用角色 `study_app`（走 SECURITY DEFINER 路径）预热 1 次
  后连续调用 `SELECT public.study_metrics_snapshot()` 20 次，记录每次延迟；
  另对函数内读取四类事实表的聚合查询逐个 `EXPLAIN (ANALYZE, BUFFERS)`。

## 环境

| 项 | 值 |
|---|---|
| OS | Windows 11 10.0.26200 |
| CPU | Intel 第 14 代（Family 6 Model 183），20 核 |
| PostgreSQL | 16.4（Windows x64，Visual C++ build 1940） |
| shared_buffers | 32 MB（开发默认，未调优） |
| work_mem | 4 MB |
| effective_cache_size | 4 GB |
| block_size | 8192 |
| max_parallel_workers_per_gather | 2 |

> 注意：本机 PG 为安装默认配置。32 MB 的 shared_buffers 显著低于生产水位，
> 冷/热边界以下的扫描成本被系统性放大（100 万规模下 provider 聚合出现
> 5916 块 shared read）。生产环境按内存调优后绝对数字会改善，但
> 全表扫描随事实量线性增长的结构性结论不变。

## 结果

### 快照整体延迟（应用角色连续调用，20 次）

| 累计事实规模 | min | p50 | p95 | max |
|---|---|---|---|---|
| 10,000 | 2.9 ms | 5.4 ms | 9.8 ms | 10.7 ms |
| 100,000 | 7.1 ms | 17.2 ms | 21.1 ms | 22.4 ms |
| 1,000,000 | 538.8 ms | 596.3 ms | 666.7 ms | 671.5 ms |

100 万规模连续采样（ms）：
538.8–671.5，分布稳定，无异常离群（采样列表见脚本输出）。

### 函数内各聚合查询 EXPLAIN (ANALYZE, BUFFERS)

| 查询 | 1万 | 10万 | 100万 | 100万 buffers (hit/read) |
|---|---|---|---|---|
| acquisition_jobs 状态总计 | 0.6 ms | 6.1 ms | 122.9 ms | 4954 / 0 |
| acquisition_jobs 对账计数 | 0.3 ms | 2.8 ms | 96.1 ms | 5333 / 0 |
| fetch_duration 直方图 | 1.5 ms | 15.1 ms | 159.5 ms | 3714 / 0 |
| fetch_bytes 直方图 | 2.7 ms | 40.2 ms | 156.5 ms | 4484 / 0 |
| teaching 路由决策分组 | 1.0 ms | 23.7 ms | 107.0 ms | 8947 / 0 |
| teaching 对账计数 | 0.4 ms | 5.7 ms | 111.6 ms | 3754 / 0 |
| provider_attempts JOIN teaching_runs | 2.1 ms | 42.4 ms | 1289.2 ms | 3882 / 5916 |
| **函数整体**（study_app 执行） | 5.1 ms | 14.5 ms | 682.5 ms | 9369 / 0 |

> 单条 EXPLAIN 的时间含独立计划启动成本且不带函数内聚合收口，故各条之和大于
> 函数整体执行时间；provider 单条查询无 GROUP BY、返回全部明细行，是其中的
> 极端上界。结构性结论：**全部聚合均为全表顺序扫描，无 WHERE 谓词可利用现有
> `acquisition_fetch_observations_aggregate_idx`（该索引按 outcome 前缀组织，
> 直方图聚合需要扫全部行）**；acquisition_jobs、teaching_runs 各被扫描两次，
> fetch_observations 被扫描两次（duration 与 bytes 两个 CTE）。

### 15 秒缓存的实测查询频率

`PostgresMetricsStore`（`backend/app/db/metrics_store.py`）以 15.0 秒 TTL 缓存快照，
因此无论 Prometheus 抓取间隔多短，**每进程对数据库的实际查询频率上限为
4 次/分钟（240 次/小时）**。在 100 万事实规模、p95 = 667 ms 下：

- 数据库时间占用 ≈ 0.667 s × 4 / 60 s ≈ **4.4% 单核**；
- 1 万 / 10 万规模下 < 0.15% 单核。

## 决定依据

1. **当前量级距离 1M 事实遥远**：100 万事实对应约 25 万次资料获取（每次含 2 条抓取
   观察）+ 12.5 万次教学运行。首版阶段真实数据在 10 万量级内，p95 = 21 ms。
2. **15 秒缓存把成本钳制在可忽略水平**（见上节），快照慢不会传导为用户可感知延迟，
   也不会对连接池形成压力。
3. **增量汇总表方案的 blast radius 大**：需要新迁移（汇总表 + 回填）、获取/教学两条
   写路径的计数更新（并发正确性）、回滚策略、与原 SQL 聚合的一致性 PG 测试，以及
   /metrics 权限回归。属于 ship-first §11 的"极限性能优化需真实 workload"范畴。
4. **仅优化函数本身不达标**：合并重复扫描预计 350–400 ms（>250 ms），不值得为此
   引入函数替换迁移。

## 执行计划（保留决定的条件与升级路径）

以下任一条件成立时，启动 `TD-001` 的增量汇总方案（或先做函数内合并扫描的过渡项）：

1. 真实环境任一事实表接近 50 万行（预计快照 p95 将超过 ~300 ms）；
2. 监控发现 /metrics 请求的数据库耗时 p95 > 250 ms（production 配置下复测）；
3. 需要将缓存 TTL 降到 5 秒以下（查询频率上升 3 倍以上）。

实施要求（与计划任务 6 一致）：回填、并发更新、回滚、与原 SQL 聚合一致性均需 PG
测试；跨租户汇总权限（SECURITY DEFINER + REVOKE FROM PUBLIC）与封闭标签输出
保持不变；/metrics 默认关闭、Bearer 令牌校验回归通过。

## 复现

```powershell
.venv\Scripts\python tools\bench_metrics_snapshot.py --scales 10000 100000 1000000 --repeat 20
```

要求本机 127.0.0.1:5432 可达的 PostgreSQL（含 `study_app`/`study_worker` 角色，
与 PG 测试套件同一环境）。脚本全程只操作 `study_test_` 前缀的随机临时库。
