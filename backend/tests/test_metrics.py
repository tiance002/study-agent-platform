from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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


def test_metrics_migration_uses_closed_facts_and_restricted_aggregate_function() -> None:
    migration = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0018_acquisition_metrics.py"
    assert migration.is_file(), "metrics migration is not implemented yet"
    sql = migration.read_text(encoding="utf-8").lower()

    assert 'revision = "0018"' in sql
    assert 'down_revision = "0017"' in sql
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
