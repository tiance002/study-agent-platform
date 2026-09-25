"""在独立临时库上实测 study_metrics_snapshot() 的容量表现。

用法::

    .venv\\Scripts\\python tools\\bench_metrics_snapshot.py
    .venv\\Scripts\\python tools\\bench_metrics_snapshot.py --scales 10000 100000 1000000 --repeat 20

安全约束：一切写入都发生在 ``pg_support.temp_test_database()`` 创建的随机临时库
（库名以 ``study_test_`` 开头），每次写入前显式 ``require_test_database()``；
本脚本绝不连接业务库。

事实规模定义：N 条事实 = acquisition_jobs N/4 + acquisition_fetch_observations N/2
（每条 job 两次抓取观察）+ teaching_runs N/8 + provider_attempts N/8。
种子数据使用合成租户/项目与封闭标签，满足全部 CHECK/UNIQUE/FK 约束。

测量内容：
- 每个规模先 ANALYZE，再以应用角色（study_app，走 SECURITY DEFINER 路径）
  连续调用 ``SELECT public.study_metrics_snapshot()``，记录每次延迟与 p95；
- 对函数内读取四类事实表的聚合查询（含 0019 新增的 retrieval_decision 分组）
  分别 ``EXPLAIN (ANALYZE, BUFFERS)``，记录执行时间与 shared buffer 命中/读取。

0019 基准要求：每条 teaching_runs 都带合法 ``retrieval_decision``
（80% keyword / 10% hybrid / 10% degraded，满足迁移 CHECK 闭集），
种子阶段断言非空决策数等于 run 总数且三种模式齐全，否则报错退出 ——
防止在空 JSONB 分组上测出假阳性结论。
"""

from __future__ import annotations

import argparse
import math
import platform
import re
import sys
from pathlib import Path
from time import perf_counter

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

from pg_support import reachable, require_test_database, temp_test_database  # noqa: E402

#: 函数调用本身（作为 study_app 执行，验证 SECURITY DEFINER 权限路径）。
SNAPSHOT_SQL = "SELECT public.study_metrics_snapshot()"

#: 从 0018 迁移的 study_metrics_snapshot() 函数体逐字提取的四类表聚合查询，
#: 用于 EXPLAIN (ANALYZE, BUFFERS) 定位函数内部各表的开销。
EXPLAIN_QUERIES: dict[str, str] = {
    "acquisition_jobs_totals": """
        SELECT
            COALESCE(sum(attempt_count), 0)::bigint AS claims_total,
            count(*) FILTER (WHERE status = 'succeeded')::bigint,
            count(*) FILTER (WHERE status = 'failed')::bigint,
            count(*) FILTER (WHERE status = 'unknown')::bigint,
            count(*) FILTER (WHERE status = 'queued')::bigint,
            count(*) FILTER (WHERE status = 'running')::bigint,
            count(*)::bigint
        FROM public.acquisition_jobs
    """,
    "acquisition_jobs_reconciliation": """
        SELECT count(*)::bigint FROM public.acquisition_jobs WHERE status = 'unknown'
    """,
    "fetch_duration_histogram": """
        SELECT COALESCE(jsonb_agg(row_value ORDER BY row_value->>'outcome'), '[]'::jsonb) AS rows
        FROM (
          SELECT jsonb_build_object(
                'outcome', outcome,
                'bucket_counts', jsonb_build_object(
                    '0.1', count(*) FILTER (WHERE duration_seconds <= 0.1),
                    '0.25', count(*) FILTER (WHERE duration_seconds <= 0.25),
                    '0.5', count(*) FILTER (WHERE duration_seconds <= 0.5),
                    '1', count(*) FILTER (WHERE duration_seconds <= 1),
                    '2.5', count(*) FILTER (WHERE duration_seconds <= 2.5),
                    '5', count(*) FILTER (WHERE duration_seconds <= 5),
                    '10', count(*) FILTER (WHERE duration_seconds <= 10),
                    '30', count(*) FILTER (WHERE duration_seconds <= 30),
                    '+Inf', count(*)
                ),
                'count', count(*),
                'sum', COALESCE(sum(duration_seconds), 0)
          ) AS row_value
          FROM public.acquisition_fetch_observations
          WHERE duration_seconds IS NOT NULL
          GROUP BY outcome
        ) AS grouped_fetch_durations
    """,
    "fetch_bytes_histogram": """
        SELECT COALESCE(jsonb_agg(row_value ORDER BY row_value->>'outcome'), '[]'::jsonb) AS rows
        FROM (
          SELECT jsonb_build_object(
                'outcome', outcome,
                'bucket_counts', jsonb_build_object(
                    '1024', count(*) FILTER (WHERE response_body_bytes <= 1024),
                    '4096', count(*) FILTER (WHERE response_body_bytes <= 4096),
                    '16384', count(*) FILTER (WHERE response_body_bytes <= 16384),
                    '65536', count(*) FILTER (WHERE response_body_bytes <= 65536),
                    '262144', count(*) FILTER (WHERE response_body_bytes <= 262144),
                    '1048576', count(*) FILTER (WHERE response_body_bytes <= 1048576),
                    '+Inf', count(*)
                ),
                'count', count(*),
                'sum', COALESCE(sum(response_body_bytes), 0)
          ) AS row_value
          FROM public.acquisition_fetch_observations
          WHERE response_body_bytes IS NOT NULL
          GROUP BY outcome
        ) AS grouped_fetch_bytes
    """,
    "teaching_route_decisions": """
        SELECT routing_decision, count(*)::bigint AS decision_count
        FROM public.teaching_runs
        WHERE routing_decision IS NOT NULL
        GROUP BY routing_decision
    """,
    "teaching_retrieval_decisions": """
        SELECT retrieval_decision, count(*)::bigint AS decision_count
        FROM public.teaching_runs
        WHERE retrieval_decision IS NOT NULL
        GROUP BY retrieval_decision
    """,
    "teaching_reconciliation": """
        SELECT count(*)::bigint FROM public.teaching_runs WHERE status = 'reconciliation_required'
    """,
    "provider_attempts_base": """
        SELECT
            p.provider_family AS provider,
            p.status AS attempt_status,
            p.input_tokens,
            p.output_tokens,
            CASE
                WHEN p.result_payload->>'provider_status' IN
                    ('completed', 'refused', 'dispatch_failed', 'timeout', 'malformed', 'truncated')
                    THEN p.result_payload->>'provider_status'
                WHEN COALESCE(p.result_payload->>'error_code', r.error_code) = 'PROVIDER_DISPATCH_FAILED'
                    THEN 'dispatch_failed'
                WHEN COALESCE(p.result_payload->>'error_code', r.error_code) IN
                    ('PROVIDER_REFUSED', 'PROVIDER_REFUSED_USAGE_UNKNOWN') THEN 'refused'
                WHEN COALESCE(p.result_payload->>'error_code', r.error_code) IN
                    ('PROVIDER_MALFORMED', 'PROVIDER_MALFORMED_USAGE_UNKNOWN') THEN 'malformed'
                WHEN COALESCE(p.result_payload->>'error_code', r.error_code) IN
                    ('PROVIDER_TRUNCATED', 'PROVIDER_TRUNCATED_USAGE_UNKNOWN') THEN 'truncated'
                WHEN r.error_code = 'PROVIDER_TIMEOUT' THEN 'timeout'
                ELSE 'unknown'
            END AS outcome,
            CASE
                WHEN p.input_tokens IS NOT NULL AND p.output_tokens IS NOT NULL THEN 'reported'
                WHEN p.input_tokens IS NULL AND p.output_tokens IS NULL
                     AND p.status IN ('completed', 'failed') THEN 'missing'
                ELSE 'unknown'
            END AS usage_state
        FROM public.provider_attempts AS p
        JOIN public.teaching_runs AS r
          ON r.tenant_id = p.tenant_id
         AND r.project_id = p.project_id
         AND r.run_id = p.run_id
    """,
}

FACT_TABLES = (
    "acquisition_jobs",
    "acquisition_fetch_observations",
    "teaching_runs",
    "provider_attempts",
)


def seed_phase(conn: psycopg.Connection, delta: int, state: dict) -> dict[str, int]:
    """向临时库追加 delta 条事实记录（合成租户/项目、封闭标签）。

    ``state`` 持有各实体的全局 id 偏移与累计数量，保证跨阶段（规模递增）时
    id 不冲突、FK 父链完整。
    """
    jobs = delta // 4
    observations = jobs * 2  # 每条 job 两次抓取观察（attempt 1 失败、attempt 2 成功）
    runs = delta // 8
    attempts = min(delta - jobs - observations - runs, runs)
    new_tenants = max(1, delta // 50_000)
    new_projects = max(1, delta // 2_500)
    new_convs = max(1, runs // 25)

    tenants_total = state["tenants_total"] + new_tenants
    projects_total = state["projects_total"] + new_projects
    t_off = state["tenant"]
    p_off = state["project"]
    c_off = state["conv"]
    src_off = state["source"]
    cand_off = state["candidate"]
    job_off = state["job"]
    msg_off = state["msg"]
    run_off = state["run"]
    pa_off = state["pa"]

    with conn.transaction():
        conn.execute(
            "INSERT INTO tenants (tenant_id, name)"
            " SELECT 'bench_t' || (g + %s), 'bench-tenant' FROM generate_series(1, %s) AS g",
            (t_off, new_tenants),
        )
        conn.execute(
            "INSERT INTO principals (principal_id, tenant_id)"
            " SELECT 'bench_u' || (g + %s), 'bench_t' || (g + %s)"
            " FROM generate_series(1, %s) AS g",
            (t_off, t_off, new_tenants),
        )
        conn.execute(
            "INSERT INTO projects (project_id, tenant_id, name)"
            " SELECT 'bench_p' || (g + %s),"
            " 'bench_t' || (mod(g - 1 + %s, %s) + 1), 'bench-project'"
            " FROM generate_series(1, %s) AS g",
            (p_off, p_off, tenants_total, new_projects),
        )
        conn.execute(
            "INSERT INTO conversations (conversation_id, tenant_id, project_id, title)"
            " SELECT 'bench_conv' || (g + %s), p.tenant_id, p.project_id, 'bench'"
            " FROM generate_series(1, %s) AS g"
            " JOIN projects p ON p.project_id = 'bench_p' || (mod(g - 1 + %s, %s) + 1)",
            (c_off, new_convs, c_off, projects_total),
        )
        conn.execute(
            "INSERT INTO sources (source_id, tenant_id, project_id, display_name,"
            " media_type, identity_hash, acquisition)"
            " SELECT 'bench_src' || (g + %s), p.tenant_id, p.project_id, 'bench.md',"
            " 'text/markdown', 'sha256:bench-' || (g + %s), '{}'::jsonb"
            " FROM generate_series(1, %s) AS g"
            " JOIN projects p ON p.project_id = 'bench_p' || (mod(g - 1 + %s, %s) + 1)",
            (src_off, src_off, jobs, src_off, projects_total),
        )
        conn.execute(
            "INSERT INTO source_candidates (candidate_id, tenant_id, project_id, url,"
            " title, snippet, source_domain, status, discovered_at, expires_at)"
            " SELECT 'bench_cand' || (g + %s), p.tenant_id, p.project_id,"
            " 'https://bench.example/doc-' || (g + %s), 'bench doc', '', 'bench.example',"
            " 'selected', now() - interval '2 hours', now() + interval '6 hours'"
            " FROM generate_series(1, %s) AS g"
            " JOIN projects p ON p.project_id = 'bench_p' || (mod(g - 1 + %s, %s) + 1)",
            (cand_off, cand_off, jobs, cand_off, projects_total),
        )
        conn.execute(
            "INSERT INTO acquisition_jobs (acquisition_id, tenant_id, project_id,"
            " source_id, candidate_id, requested_by, url, title, media_type, language,"
            " idempotency_key, status, attempt_count, error_code, error_detail)"
            " SELECT 'bench_job' || (g + %s), s.tenant_id, s.project_id, s.source_id,"
            " 'bench_cand' || (g + %s), 'bench-requester',"
            " 'https://bench.example/doc-' || (g + %s), 'bench doc', 'text/markdown', 'zh',"
            " 'bench-idem-' || (g + %s),"
            " CASE WHEN mod(g, 20) = 0 THEN 'failed' WHEN mod(g, 20) = 1 THEN 'unknown'"
            "      ELSE 'succeeded' END,"
            " 2,"
            " CASE WHEN mod(g, 20) = 0 THEN 'FETCH_FAILED' WHEN mod(g, 20) = 1 THEN 'FETCH_UNKNOWN'"
            "      ELSE '' END,"
            " ''"
            " FROM generate_series(1, %s) AS g"
            " JOIN sources s ON s.source_id = 'bench_src' || (g + %s)",
            (job_off, cand_off, src_off, job_off, jobs, job_off),
        )
        conn.execute(
            "INSERT INTO acquisition_fetch_observations (tenant_id, project_id,"
            " acquisition_id, attempt_number, outcome, duration_seconds, response_body_bytes)"
            " SELECT j.tenant_id, j.project_id, j.acquisition_id, a.attempt_number,"
            " CASE WHEN a.attempt_number = 1 THEN 'failed' ELSE 'succeeded' END,"
            " CASE WHEN a.attempt_number = 1 THEN 2.0 + mod(g, 280)"
            "      ELSE 0.1 + mod(g, 20)::double precision / 10.0 END,"
            " CASE WHEN a.attempt_number = 1 THEN 20000 + mod(g, 100000)"
            "      ELSE 1024 + mod(g, 500000) END"
            " FROM generate_series(1, %s) AS g"
            " CROSS JOIN (VALUES (1), (2)) AS a(attempt_number)"
            " JOIN acquisition_jobs j ON j.acquisition_id = 'bench_job' || (g + %s)",
            (jobs, job_off),
        )
        conn.execute(
            "INSERT INTO messages (message_id, tenant_id, project_id, conversation_id,"
            " seq, role, content)"
            " SELECT 'bench_msg' || (g + %s), c.tenant_id, c.project_id, c.conversation_id,"
            " (g - 1) / %s + 1, 'user', 'bench question'"
            " FROM generate_series(1, %s) AS g"
            " JOIN conversations c ON c.conversation_id = 'bench_conv' || (mod(g - 1, %s) + %s)",
            (msg_off, new_convs, runs, new_convs, c_off),
        )
        conn.execute(
            "INSERT INTO teaching_runs (run_id, tenant_id, project_id, conversation_id,"
            " user_message_id, principal_id, question, status, attempt_count, model_id,"
            " prompt_version, ranking_version, routing_decision, retrieval_decision,"
            " error_code, error_detail)"
            " SELECT 'bench_run' || (g + %s), m.tenant_id, m.project_id, m.conversation_id,"
            " m.message_id, 'bench_u' || substring(m.tenant_id from 8),"
            " 'bench question ' || g,"
            " CASE WHEN mod(g, 50) = 0 THEN 'reconciliation_required' ELSE 'failed' END,"
            " 1, 'bench-model', 'bench-prompt-v1', 'bench-ranking-v1',"
            " jsonb_build_object("
            "   'policy_version', 'teaching-route/v1',"
            "   'answer_route', 'cloud',"
            "   'query_rewrite_status', CASE WHEN mod(g, 3) = 0 THEN 'disabled'"
            "      WHEN mod(g, 3) = 1 THEN 'applied' ELSE 'fallback' END,"
            "   'reason_code', CASE WHEN mod(g, 3) = 0 THEN 'local_not_configured'"
            "      WHEN mod(g, 3) = 1 THEN 'local_rewrite_accepted'"
            "      ELSE 'local_unavailable_or_invalid' END),"
            " jsonb_build_object("
            "   'policy_version', 'retrieval-route/v1',"
            "   'mode', CASE WHEN mod(g, 10) = 0 THEN 'degraded'"
            "      WHEN mod(g, 10) = 2 THEN 'hybrid' ELSE 'keyword' END,"
            "   'reason_code', CASE WHEN mod(g, 10) = 0 THEN 'vector_index_unavailable'"
            "      ELSE '' END,"
            "   'ranking_version', CASE WHEN mod(g, 10) = 0 THEN 'hybrid-rrf/v1'"
            "      WHEN mod(g, 10) = 2 THEN 'hybrid-rrf/v1' ELSE 'keyword/v1' END),"
            " CASE WHEN mod(g, 50) = 0 THEN 'RECONCILIATION_REQUIRED'"
            "      ELSE 'PROVIDER_TIMEOUT' END,"
            " 'bench'"
            " FROM generate_series(1, %s) AS g"
            " JOIN messages m ON m.message_id = 'bench_msg' || (g + %s)",
            (run_off, runs, msg_off),
        )
        conn.execute(
            "INSERT INTO provider_attempts (attempt_id, tenant_id, project_id, run_id,"
            " status, provider_request_id, result_payload, input_tokens, output_tokens,"
            " cost_micro, provider_family)"
            " SELECT 'bench_pa' || (g + %s), r.tenant_id, r.project_id, r.run_id,"
            " 'completed', 'bench-req-' || (g + %s),"
            " jsonb_build_object('provider_status',"
            "   CASE mod(g, 5) WHEN 0 THEN 'completed' WHEN 1 THEN 'timeout'"
            "     WHEN 2 THEN 'refused' WHEN 3 THEN 'malformed' ELSE 'truncated' END),"
            " 100 + mod(g, 900), 50 + mod(g, 950), 1000,"
            " CASE WHEN mod(g, 10) = 0 THEN 'unknown' ELSE 'openai' END"
            " FROM generate_series(1, %s) AS g"
            " JOIN teaching_runs r ON r.run_id = 'bench_run' || (g + %s)",
            (pa_off, pa_off, attempts, run_off),
        )

    state["tenant"] += new_tenants
    state["project"] += new_projects
    state["conv"] += new_convs
    state["source"] += jobs
    state["candidate"] += jobs
    state["job"] += jobs
    state["msg"] += runs
    state["run"] += runs
    state["pa"] += attempts
    state["tenants_total"] = tenants_total
    state["projects_total"] = projects_total

    # 0019 前置断言：retrieval_decision 必须全量非空且三种模式齐全，
    # 否则测到的是空 JSONB 分组，0019 容量结论不成立 —— 直接报错退出。
    total_runs = conn.execute("SELECT count(*) FROM public.teaching_runs").fetchone()[0]
    decided = conn.execute(
        "SELECT count(*) FROM public.teaching_runs WHERE retrieval_decision IS NOT NULL"
    ).fetchone()[0]
    if decided != total_runs:
        raise RuntimeError(
            "种子断言失败：retrieval_decision 非空行数"
            f" {decided} != teaching_runs 总数 {total_runs}"
        )
    distribution = dict(
        conn.execute(
            "SELECT retrieval_decision->>'mode', count(*) FROM public.teaching_runs"
            " WHERE retrieval_decision IS NOT NULL GROUP BY 1"
        ).fetchall()
    )
    missing = {"keyword", "hybrid", "degraded"} - set(distribution)
    if missing:
        raise RuntimeError(
            f"种子断言失败：retrieval_decision 缺少模式 {sorted(missing)}"
            f"（当前分布 {distribution}）"
        )
    print(f"# retrieval_decision 断言通过: 非空 {decided}/{total_runs} | 分布 {distribution}")
    return {
        "jobs": jobs,
        "observations": observations,
        "runs": runs,
        "attempts": attempts,
    }


def table_counts(conn: psycopg.Connection) -> dict[str, int]:
    counts = {}
    for table in FACT_TABLES:
        counts[table] = conn.execute(f"SELECT count(*) FROM public.{table}").fetchone()[0]
    return counts


def measure_snapshot(app_conn: psycopg.Connection, repeat: int) -> list[float]:
    """连续调用快照函数，返回每次的毫秒延迟。"""
    app_conn.execute(SNAPSHOT_SQL).fetchone()  # 预热
    timings: list[float] = []
    for _ in range(repeat):
        started = perf_counter()
        app_conn.execute(SNAPSHOT_SQL).fetchone()
        timings.append((perf_counter() - started) * 1000.0)
    return timings


def explain(conn: psycopg.Connection, sql: str) -> dict[str, float]:
    rows = conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql).fetchall()
    text = "\n".join(row[0] for row in rows)
    exec_ms = [float(m) for m in re.findall(r"Execution Time: ([0-9.]+) ms", text)]
    return {
        "execution_ms": exec_ms[-1] if exec_ms else 0.0,
        "shared_hit": float(sum(int(m) for m in re.findall(r"shared hit=(\d+)", text))),
        "shared_read": float(sum(int(m) for m in re.findall(r"shared read=(\d+)", text))),
    }


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    rank = max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))
    return sorted_values[rank]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scales", type=int, nargs="+", default=[10_000, 100_000, 1_000_000],
        help="累计事实规模（默认 1万 10万 100万）",
    )
    parser.add_argument("--repeat", type=int, default=20, help="每规模连续快照调用次数")
    args = parser.parse_args()

    if not reachable():
        print("本机 PostgreSQL 不可达（127.0.0.1:5432），无法运行容量实验。", file=sys.stderr)
        raise SystemExit(2)

    with temp_test_database() as database:
        # 计划要求：写入前显式校验库名前缀（temp_test_database 内部也会校验）。
        require_test_database(database.migration_dsn)
        print(f"# 临时库: {database.name}")
        state = {
            "tenant": 0, "project": 0, "conv": 0, "source": 0, "candidate": 0,
            "job": 0, "msg": 0, "run": 0, "pa": 0,
            "tenants_total": 0, "projects_total": 0,
        }
        with psycopg.connect(database.migration_dsn) as admin_conn, \
                psycopg.connect(database.app_dsn) as app_conn:
            version = admin_conn.execute("SELECT version()").fetchone()[0]
            print(f"# {version}")
            print(f"# python: {platform.platform()} | cpu: {platform.processor() or 'unknown'}"
                  f" | cores: {__import__('os').cpu_count()}")
            for setting in ("shared_buffers", "work_mem", "effective_cache_size",
                            "block_size", "max_parallel_workers_per_gather"):
                value = admin_conn.execute(f"SHOW {setting}").fetchone()[0]
                print(f"# {setting} = {value}")

            current = 0
            for target in sorted(args.scales):
                delta = target - current
                if delta <= 0:
                    continue
                require_test_database(database.migration_dsn)
                seeded = seed_phase(admin_conn, delta, state)
                admin_conn.execute("ANALYZE")
                current = target
                counts = table_counts(admin_conn)
                print(f"\n=== 规模 {target:,}（本次新增 {seeded}）===")
                print(f"事实行数: {counts} | 合计 {sum(counts.values()):,}")

                timings = measure_snapshot(app_conn, args.repeat)
                ordered = sorted(timings)
                print(
                    f"快照延迟(ms): min={ordered[0]:.1f} p50={percentile(ordered, 0.5):.1f}"
                    f" p95={percentile(ordered, 0.95):.1f} max={ordered[-1]:.1f}"
                    f" | 连续采样={[round(t, 1) for t in timings]}"
                )
                print(f"EXPLAIN 函数整体: {explain(app_conn, SNAPSHOT_SQL)}")
                for name, sql in EXPLAIN_QUERIES.items():
                    stats = explain(admin_conn, sql)
                    print(f"EXPLAIN {name}: {stats}")


if __name__ == "__main__":
    main()
