from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pg_support
import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]

PG_ONLY = pytest.mark.skipif(
    not pg_support.reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)


def _snapshot() -> dict:
    return {
        "acquisition_claims_total": 4,
        "acquisition_jobs_total": {"succeeded": 1, "failed": 0, "unknown": 1},
        "acquisition_jobs": {
            "queued": 0,
            "running": 1,
            "succeeded": 1,
            "failed": 0,
            "unknown": 1,
        },
        "fetch_duration": [
            {
                "outcome": "succeeded",
                "bucket_counts": {"0.1": 0, "0.25": 1, "0.5": 1, "1": 1, "2.5": 1,
                                   "5": 1, "10": 1, "30": 1, "+Inf": 1},
                "count": 1,
                "sum": 0.2,
            },
            {
                "outcome": "unknown",
                "bucket_counts": {"0.1": 0, "0.25": 0, "0.5": 0, "1": 0, "2.5": 1,
                                   "5": 1, "10": 1, "30": 1, "+Inf": 1},
                "count": 1,
                "sum": 1.3,
            },
        ],
        "response_body_bytes": [
            {
                "outcome": "succeeded",
                "bucket_counts": {"1024": 1, "4096": 1, "16384": 1, "65536": 1,
                                   "262144": 1, "1048576": 1, "+Inf": 1},
                "count": 1,
                "sum": 42,
            }
        ],
        "route_decisions": [
            {"query_rewrite_status": "disabled", "reason_code": "local_not_configured", "count": 3},
            {"query_rewrite_status": "applied", "reason_code": "local_rewrite_accepted", "count": 2},
            {"query_rewrite_status": "fallback", "reason_code": "local_unavailable_or_invalid", "count": 1},
            {"query_rewrite_status": "fallback", "reason_code": "private prompt sentinel", "count": 99},
        ],
        "retrieval_decisions": [
            {"mode": "keyword", "reason_code": "", "count": 3},
            {"mode": "hybrid", "reason_code": "", "count": 2},
            {"mode": "degraded", "reason_code": "vector_index_unavailable", "count": 1},
            {"mode": "degraded", "reason_code": "private retrieval sentinel", "count": 99},
        ],
        "provider_attempts": [
            {"provider": "openai", "outcome": "completed", "count": 1},
            {"provider": "openai", "outcome": "timeout", "count": 1},
            {"provider": "unknown", "outcome": "unknown", "count": 1},
        ],
        "provider_tokens": [
            {"provider": "openai", "direction": "input", "count": 42},
            {"provider": "openai", "direction": "output", "count": 0},
        ],
        "provider_usage": [
            {"provider": "openai", "state": "reported", "count": 1},
            {"provider": "openai", "state": "missing", "count": 1},
            {"provider": "unknown", "state": "unknown", "count": 1},
        ],
        "reconciliation_pending": {"acquisition": 1, "teaching": 2},
        "untrusted_scope": "tenant/project/prompt sentinel",
    }


def _metric_value(lines: set[str], prefix: str) -> int:
    """从整行集合里取 `prefix value` 的数值；缺失或重复都直接失败。"""
    matches = [line for line in lines if line.startswith(prefix + " ")]
    assert len(matches) == 1, f"expected exactly one line for {prefix!r}, got {matches}"
    return int(matches[0].split()[-1])


def test_metrics_store_reuses_recent_snapshot_and_refreshes_after_ttl(monkeypatch) -> None:
    from app.db import metrics_store

    now = [100.0]
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def execute(self, sql):
            calls.append(sql)
            return self

        def fetchone(self):
            return ({"acquisition_claims_total": len(calls)},)

    monkeypatch.setattr(metrics_store, "connect", lambda _dsn: Connection())
    store = metrics_store.PostgresMetricsStore(monotonic=lambda: now[0], cache_seconds=15)
    assert store.snapshot()["acquisition_claims_total"] == 1
    now[0] += 14
    assert store.snapshot()["acquisition_claims_total"] == 1
    now[0] += 2
    assert store.snapshot()["acquisition_claims_total"] == 2
    assert len(calls) == 2


def test_render_metrics_emits_fixed_names_units_and_closed_labels_only() -> None:
    try:
        from app.metrics import render_metrics
    except ImportError as exc:
        pytest.fail(f"metrics renderer is not implemented yet: {exc}")

    exposition = render_metrics(_snapshot())

    assert "# TYPE study_acquisition_fetch_duration_seconds histogram" in exposition
    assert 'study_acquisition_fetch_duration_seconds_bucket{outcome="succeeded",le="0.25"} 1' in exposition
    assert 'study_acquisition_fetch_duration_seconds_count{outcome="unknown"} 1' in exposition
    assert 'study_acquisition_response_body_bytes_sum{outcome="succeeded"} 42' in exposition
    assert "study_acquisition_claims_total 4" in exposition
    assert 'study_acquisition_jobs_total{outcome="succeeded"} 1' in exposition
    assert 'study_acquisition_jobs{status="running"} 1' in exposition
    assert (
        'study_teaching_route_decisions_total{query_rewrite_status="fallback",'
        'reason_code="local_unavailable_or_invalid",answer_route="cloud"} 1'
    ) in exposition
    assert 'study_teaching_fallbacks_total{reason_code="local_unavailable_or_invalid"} 1' in exposition
    # 整行精确比较：子串断言会让 `...} 1` 误匹配 `...} 100`。
    exposition_lines = set(exposition.splitlines())
    assert 'study_teaching_retrieval_modes_total{mode="keyword"} 3' in exposition_lines
    assert 'study_teaching_retrieval_modes_total{mode="hybrid"} 2' in exposition_lines
    assert 'study_teaching_retrieval_modes_total{mode="degraded"} 1' in exposition_lines
    assert (
        'study_teaching_retrieval_degraded_total{reason_code="vector_index_unavailable"} 1'
        in exposition_lines
    )
    assert (
        'study_teaching_retrieval_degraded_total{reason_code="embedding_provider_failed"} 0'
        in exposition_lines
    )
    assert (
        'study_teaching_retrieval_degraded_total{reason_code="embedding_model_version_mismatch"} 0'
        in exposition_lines
    )
    assert "private retrieval sentinel" not in exposition
    # 闭环不变量：模式总数 == 各合法原因之和（污染行不得抬高降级计数）。
    degraded_mode_total = _metric_value(
        exposition_lines, 'study_teaching_retrieval_modes_total{mode="degraded"}'
    )
    degraded_reason_sum = sum(
        _metric_value(
            exposition_lines,
            f'study_teaching_retrieval_degraded_total{{reason_code="{reason}"}}',
        )
        for reason in (
            "embedding_provider_failed",
            "vector_index_unavailable",
            "embedding_model_version_mismatch",
        )
    )
    assert degraded_mode_total == degraded_reason_sum
    assert 'study_provider_attempts_total{provider="openai",outcome="timeout"} 1' in exposition
    assert 'study_provider_tokens_total{provider="openai",direction="input"} 42' in exposition
    assert 'study_provider_usage_total{provider="openai",state="missing"} 1' in exposition
    assert 'study_reconciliation_pending{domain="teaching"} 2' in exposition
    assert "study_reconciliation_entries_total" not in exposition
    assert "private prompt sentinel" not in exposition
    assert "tenant/project/prompt sentinel" not in exposition


def test_metrics_endpoint_is_default_deny_and_requires_configured_bearer_token() -> None:
    from app.deployment import DeploymentSettings
    from app.main import build_platform, create_app

    class StaticReader:
        calls = 0

        def snapshot(self) -> dict:
            self.calls += 1
            return _snapshot()

    disabled_settings = DeploymentSettings.load({})
    disabled_app = create_app(platform=build_platform(settings=disabled_settings))
    disabled_reader = StaticReader()
    disabled_app.state.metrics_reader = disabled_reader
    disabled_response = TestClient(disabled_app).get("/metrics")
    assert disabled_response.status_code == 404
    assert disabled_reader.calls == 0

    settings = DeploymentSettings.load({"STUDY_PLATFORM_METRICS_TOKEN": "test-only-metrics-token"})
    app = create_app(platform=build_platform(settings=settings))
    reader = StaticReader()
    app.state.metrics_reader = reader
    client = TestClient(app)

    assert client.get("/metrics").status_code == 404
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong-token"}).status_code == 404
    assert reader.calls == 0

    response = client.get(
        "/metrics",
        headers={"Authorization": "Bearer test-only-metrics-token"},
    )
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "study_provider_tokens_total" in response.text
    assert "test-only-metrics-token" not in response.text
    assert "tenant/project/prompt sentinel" not in response.text
    assert reader.calls == 1


def test_provider_family_metric_label_is_openai_only_for_known_adapter() -> None:
    from app.teaching.openai_provider import OpenAIResponsesProvider
    from app.teaching.service import _provider_family_label

    known = OpenAIResponsesProvider.__new__(OpenAIResponsesProvider)
    assert _provider_family_label(known) == "openai"
    assert _provider_family_label(object()) == "unknown"


def test_baseline_migration_carries_closed_metrics_facts() -> None:
    """新基线（0001）必须自带闭集事实表、受限聚合函数与检索决策约束。

    这些性质原先分散在 0018/0019 两条迁移上（各自的源码扫描用例）；
    压成单条基线后合到一处，并把"快照函数只有一版（0019 形态）"、
    "基线不含任何邀请码对象"作为回归锁。
    """
    migration = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0001_initial_schema.py"
    )
    assert migration.is_file(), "baseline migration is not implemented"
    sql = migration.read_text(encoding="utf-8").lower()

    assert 'revision = "0001"' in sql
    assert "down_revision = none" in sql

    # ---- 抓取指标事实表与受限聚合函数（原 0018） ------------------------
    assert "acquisition_fetch_observations" in sql
    assert "unique (tenant_id, project_id, acquisition_id, attempt_number)" in sql
    assert "force row level security" in sql
    assert "grant select, insert, update on public.acquisition_fetch_observations to study_worker" in sql
    assert "provider_family in ('openai', 'unknown')" in sql
    assert "security definer" in sql
    assert "set search_path = pg_catalog" in sql
    assert "revoke all on function public.study_metrics_snapshot() from public" in sql
    assert "grant execute on function public.study_metrics_snapshot() to study_app" in sql
    assert "grant select on acquisition_fetch_observations to study_app" not in sql

    # ---- 检索决策闭集 CHECK（原 0019） ----------------------------------
    # 列 + 闭集 CHECK：模式三元、降级原因三闭集、非降级原因必须为空串。
    assert "add column retrieval_decision jsonb" in sql
    # 字段类型约束：null / 数值型字段在 `->>` 比较下会静默通过，必须显式 typeof。
    assert "jsonb_typeof(retrieval_decision->'reason_code') = 'string'" in sql
    assert "jsonb_typeof(retrieval_decision->'ranking_version') = 'string'" in sql
    assert "retrieval_decision->>'mode' in ('keyword', 'hybrid', 'degraded')" in sql
    assert "'embedding_model_version_mismatch'" in sql
    assert "retrieval_decision->>'ranking_version' <> ''" in sql

    # ---- 快照函数只保留 0019 版（压扁后不再有 0018 双函数体模板） -------
    assert "create or replace function public.study_metrics_snapshot()" in sql
    assert "'retrieval_decisions'" in sql
    assert "_metrics_function_sql" not in sql

    # ---- 基线不得含任何邀请码对象（新基线的不变量） ---------------------
    assert "invitations" not in sql
    assert "exchange_invitation" not in sql
    assert "invitee_principal_id" not in sql
    assert "auth_method = 'invitation'" not in sql


def _run_alembic(dsn: str, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env={**os.environ, "STUDY_PLATFORM_MIGRATION_DSN": dsn},
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError((result.stdout or "") + (result.stderr or ""))
    return result


def _seed_teaching_run(dsn: str, suffix: str, retrieval_decision) -> None:
    """造一条带 retrieval_decision 的 teaching_runs（含最小父链）。"""
    tenant = f"t_r19_{suffix}"
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)", (tenant, tenant)
        )
        conn.execute(
            "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)",
            (f"u_r19_{suffix}", tenant),
        )
        conn.execute(
            "INSERT INTO projects (project_id, tenant_id, name) VALUES (%s, %s, %s)",
            (f"proj_r19_{suffix}", tenant, "r19"),
        )
        conn.execute(
            "INSERT INTO conversations (conversation_id, tenant_id, project_id, title)"
            " VALUES (%s, %s, %s, %s)",
            (f"conv_r19_{suffix}", tenant, f"proj_r19_{suffix}", "r19"),
        )
        conn.execute(
            "INSERT INTO messages (message_id, tenant_id, project_id, conversation_id,"
            " seq, role, content) VALUES (%s, %s, %s, %s, 1, 'user', 'q')",
            (
                f"msg_r19_{suffix}",
                tenant,
                f"proj_r19_{suffix}",
                f"conv_r19_{suffix}",
            ),
        )
        conn.execute(
            "INSERT INTO teaching_runs (run_id, tenant_id, project_id, conversation_id,"
            " user_message_id, principal_id, answer_seq, question, status, model_id,"
            " prompt_version, ranking_version, retrieval_decision)"
            " VALUES (%s, %s, %s, %s, %s, %s, 2, 'q', 'queued', 'm', 'p', 'keyword/v1', %s)",
            (
                f"run_r19_{suffix}",
                tenant,
                f"proj_r19_{suffix}",
                f"conv_r19_{suffix}",
                f"msg_r19_{suffix}",
                f"u_r19_{suffix}",
                psycopg.types.json.Json(retrieval_decision) if retrieval_decision is not None else None,
            ),
        )


_VALID_DECISION = {
    "policy_version": "retrieval-route/v1",
    "mode": "degraded",
    "reason_code": "vector_index_unavailable",
    "ranking_version": "hybrid-rrf/v1",
}


@PG_ONLY
def test_retrieval_decision_check_rejects_malformed_json_and_guards_baseline_downgrade() -> None:
    """CHECK 必须拒绝字段类型错误的 JSON；有存证时基线降级必须失败；应用角色可读快照。

    `->>'x' = '...'` 对 JSON null / 数值会得到 NULL（CHECK 视为通过），
    所以 typeof 约束不是冗余 —— 这条测试就是它的反例锁。
    压扁后"降级到 0018"已不存在，改验**基线自身的降级护栏**：库里有业务行就不许清库。
    """
    from psycopg import errors as pg_errors

    with pg_support.temp_test_database() as database:
        dsn = database.migration_dsn

        # 合法决策可写入（对照组：不是约束过严把合法值也拦了）。
        _seed_teaching_run(dsn, "ok", _VALID_DECISION)

        # 字段类型非法的变体必须被 CHECK 拒绝。
        for index, override in enumerate(
            [
                {"reason_code": None},  # JSON null
                {"ranking_version": 1},  # 数值型
                {"mode": ["degraded"]},  # 数组
            ]
        ):
            malformed = {**_VALID_DECISION, **override}
            with pytest.raises(pg_errors.CheckViolation):
                _seed_teaching_run(dsn, f"bad{index}", malformed)

        # 库里已有业务行时，基线降级必须拒绝（不是静默清掉用户数据）。
        result = _run_alembic(dsn, "downgrade", "base", check=False)
        assert result.returncode != 0
        assert "cannot downgrade the baseline" in (result.stdout + result.stderr)

        # 应用角色（study_app）能执行快照函数并看到聚合行 —— 无需表级 SELECT。
        with psycopg.connect(database.app_dsn) as conn:
            snapshot = conn.execute(
                "SELECT public.study_metrics_snapshot()"
            ).fetchone()[0]
        assert snapshot["retrieval_decisions"] == [
            {"mode": "degraded", "reason_code": "vector_index_unavailable", "count": 1}
        ]


def test_postgres_metrics_store_reads_only_the_aggregate_snapshot(monkeypatch) -> None:
    from app.db import metrics_store

    expected = {"acquisition_claims_total": 3}

    class Cursor:
        def fetchone(self):
            return (expected,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query: str):
            assert query == "SELECT public.study_metrics_snapshot()"
            return Cursor()

    monkeypatch.setattr(metrics_store, "connect", lambda _dsn: Connection())
    assert metrics_store.PostgresMetricsStore(dsn="safe-test-dsn").snapshot() == expected
