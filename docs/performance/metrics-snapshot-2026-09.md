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
- **2026-09-24 更新（0019）**：迁移 0019 替换了快照函数（新增 retrieval_decision 聚合）。
  用非空检索决策重测后，10 万事实规模 p95 = 28.5 ms 仍远低于 250 ms 目标；100 万规模
  p95 恶化至 1099.2 ms（旧值 666.7 ms）。100 万本就在 TD-001 范畴内，本轮不新增升级
  动作，详见下文「0019 重测」一节。

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

## 0019 重测（2026-09-24，retrieval_decision 非空）

### 背景

迁移 0019 给 `teaching_runs` 增加 `retrieval_decision jsonb`（闭集 CHECK），并把
`study_metrics_snapshot()` 整体替换为带 `retrieval_decisions` 聚合的新版（对
`retrieval_decision` 做 GROUP BY jsonb）。上文的 0018 数据测的是**空 JSONB 分组**，
不能作为 0019 的基准，故用非空决策数据重测。

### 采样环境

- 时间：2026-09-24 16:12–16:25（Asia/Shanghai，本地 Windows 11）。
- PostgreSQL：`PostgreSQL 16.4, compiled by Visual C++ build 1940, 64-bit`（`SELECT version()`）。
- 配置同上文环境表（shared_buffers 32 MB / work_mem 4 MB / effective_cache_size 4 GB /
  block_size 8192 / max_parallel_workers_per_gather 2，均未调优）。
- 三个规模在同一随机临时库（`study_test_2530b360ec0d`）内累计增长，每规模 repeat 20 次。

### 种子断言（防止测到空分组）

每条合成 `teaching_runs` 按确定性规则（按序号取模）写入合法 `retrieval_decision`：
约 80% `keyword`（reason_code `''`，ranking_version `keyword/v1`）、约 10% `hybrid`
（reason_code `''`，`hybrid-rrf/v1`）、约 10% `degraded`（reason_code
`vector_index_unavailable`，`hybrid-rrf/v1`），policy_version 一律 `retrieval-route/v1`，
全部通过 0019 CHECK。种子阶段断言非空决策数 == run 总数且三种模式齐全，失败即退出：

| 累计规模 | 非空/总数 | keyword | hybrid | degraded |
|---|---|---|---|---|
| 10,000 | 1225/1225 | 975 | 125 | 125 |
| 100,000 | 12475/12475 | 9975 | 1250 | 1250 |
| 1,000,000 | 124975/124975 | 99975 | 12500 | 12500 |

> 已知的种子保真度偏差（0018 基准同样存在，为保持可比未改动）：首阶段的
> messages→conversations JOIN 存在既有 off-by-one（会话号为 `bench_conv1..k`，
> JOIN 拼出 `bench_conv0..k-1`），首阶段序号 ≡1 (mod k) 的消息/run 被静默丢弃，
> 故实际 runs 比名义少 0.02%（1M：124,975 vs 125,000；1万：1225 vs 1250）。

### 快照整体延迟（应用角色连续调用，20 次）

| 累计事实规模 | min | p50 | p95 | max | 0018 p95（参考） |
|---|---|---|---|---|---|
| 10,000 | 3.5 ms | 4.0 ms | 4.4 ms | 4.9 ms | 9.8 ms |
| 100,000 | 8.5 ms | 23.8 ms | **28.5 ms** | 28.8 ms | 21.1 ms |
| 1,000,000 | 766.1 ms | 913.1 ms | **1099.2 ms** | 1131.2 ms | 666.7 ms |

### 函数内各聚合查询 EXPLAIN (ANALYZE, BUFFERS)（执行耗时 ms）

| 查询 | 1万 | 10万 | 100万 | 100万 buffers (hit/read) |
|---|---|---|---|---|
| acquisition_jobs 状态总计 | 0.5 | 10.1 | 112.6 | 2274 / 0 |
| acquisition_jobs 对账计数 | 0.2 | 4.7 | 73.5 | 2653 / 0 |
| fetch_duration 直方图 | 1.5 | 29.1 | 106.1 | 490 / 0 |
| fetch_bytes 直方图 | 1.4 | 53.6 | 111.2 | 1260 / 0 |
| teaching 路由决策分组 | 0.9 | 31.2 | 86.2 | 14301 / 0 |
| **teaching 检索决策分组（0019 新增）** | 0.9 | 25.1 | 88.0 | 14784 / 0 |
| teaching 对账计数 | 0.2 | 3.4 | 140.7 | 6088 / 0 |
| provider_attempts JOIN teaching_runs | 1.1 | 48.6 | 1194.9 | 6216 / 5916 |
| **函数整体**（study_app 执行） | 3.3 | 24.9 | 940.5 | 13458 / 0 |

检索决策聚合的计划形状（单独在 1 万规模临时库上验证）：
`Seq Scan on teaching_runs`（Filter: retrieval_decision IS NOT NULL，actual rows=1225）
→ `HashAggregate`（actual rows=3，即三个决策组）。`teaching_runs` 现有索引
（pkey / user_message_id / 租户组合 / claim 部分索引 / scope）均不覆盖
`retrieval_decision`，该聚合在所有规模下都是全表顺序扫描——与函数内其余聚合同构。

### 与 0018 旧值对比及结论

1. **10 万事实规模：达标。** p95 = 28.5 ms ≤ 250 ms 目标（0018 为 21.1 ms，+7.4 ms，
   与新增一条 teaching_runs 全表聚合的量级一致）。断言确认测的是非空三模式分组，
   **0019 基准成立**。
2. **100 万事实规模：超标，维持 TD-001，不新增升级动作。** p95 = 1099.2 ms > 250 ms，
   且比 0018 的 666.7 ms 高约 65%。拆解：
   - 新增检索聚合本身在 100 万规模约 88 ms，不是主因；
   - 主因是 `teaching_runs` 因新增 jsonb 列行宽增大（路由决策分组的 buffer 命中从
     8947 块增至 14301 块，约 +60%），函数内所有 teaching_runs 扫描（路由/检索/
     对账/provider JOIN）同步变贵，叠加本机 32 MB shared_buffers 的放大效应
     （provider JOIN 仍出现 5916 块 shared read）。
   - 按 TD-001 触发条件判断：真实数据量距 50 万行遥远；15 秒缓存下即使达到 100 万
     规模，DB 占用约 1.1 s × 4/60 ≈ 7.3% 单核（0018 口径为 4.4%），仍未触发立即
     优化。**但 0019 后的 100 万基线已是 ~1.1 s，若真实增长接近任一触发条件
     （任一事实表近 50 万行 / /metrics DB p95 > 250 ms / 需要更低缓存 TTL），
     应按新基线直接执行 TD-001 的增量汇总方案。**

## 复现

```powershell
.venv\Scripts\python tools\bench_metrics_snapshot.py --scales 10000 100000 1000000 --repeat 20
```

要求本机 127.0.0.1:5432 可达的 PostgreSQL（含 `study_app`/`study_worker` 角色，
与 PG 测试套件同一环境）。脚本全程只操作 `study_test_` 前缀的随机临时库。
