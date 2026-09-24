"""Privacy-safe Prometheus exposition for persisted, low-cardinality metrics."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

ACQUISITION_OUTCOMES = ("succeeded", "failed", "unknown")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "unknown")
PROVIDERS = ("openai", "unknown")
PROVIDER_OUTCOMES = (
    "completed",
    "refused",
    "dispatch_failed",
    "timeout",
    "malformed",
    "truncated",
    "unknown",
)
REWRITE_DECISIONS = (
    ("disabled", "local_not_configured"),
    ("applied", "local_rewrite_accepted"),
    ("fallback", "local_unavailable_or_invalid"),
)
DURATION_BUCKETS = ("0.1", "0.25", "0.5", "1", "2.5", "5", "10", "30", "+Inf")
BODY_BYTES_BUCKETS = ("1024", "4096", "16384", "65536", "262144", "1048576", "+Inf")


def _number(value: object, *, integer: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "0"
    if not math.isfinite(float(value)) or value < 0:
        return "0"
    if integer:
        return str(int(value)) if int(value) == value else "0"
    return format(float(value), ".15g")


def _rows(value: object) -> Sequence[Mapping[str, Any]]:
    if not isinstance(value, list):
        return ()
    return tuple(row for row in value if isinstance(row, Mapping))


def _count_map(value: object, allowed: Sequence[str]) -> dict[str, int]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, int] = {}
    for label in allowed:
        raw = source.get(label, 0)
        result[label] = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0
    return result


def _row_count(rows: Sequence[Mapping[str, Any]], labels: Mapping[str, str]) -> int:
    return sum(
        int(row.get("count", 0))
        for row in rows
        if all(row.get(key) == expected for key, expected in labels.items())
        and isinstance(row.get("count", 0), int)
        and not isinstance(row.get("count", 0), bool)
        and row.get("count", 0) >= 0
    )


def _histogram_lines(
    snapshot: Mapping[str, Any],
    *,
    key: str,
    metric: str,
    buckets: Sequence[str],
    outcomes: Sequence[str],
    integer: bool,
) -> list[str]:
    observations = {
        str(row.get("outcome")): row
        for row in _rows(snapshot.get(key))
        if row.get("outcome") in outcomes
    }
    lines: list[str] = []
    for outcome in outcomes:
        row = observations.get(outcome, {})
        bucket_counts = row.get("bucket_counts", {})
        if not isinstance(bucket_counts, Mapping):
            bucket_counts = {}
        for bucket in buckets:
            value = bucket_counts.get(bucket, 0)
            rendered = _number(value, integer=True)
            lines.append(f'{metric}_bucket{{outcome="{outcome}",le="{bucket}"}} {rendered}')
        lines.append(
            f'{metric}_sum{{outcome="{outcome}"}} '
            f'{_number(row.get("sum", 0), integer=integer)}'
        )
        lines.append(
            f'{metric}_count{{outcome="{outcome}"}} '
            f'{_number(row.get("count", 0), integer=True)}'
        )
    return lines


def render_metrics(snapshot: Mapping[str, Any]) -> str:
    """Render a PostgreSQL aggregate snapshot without accepting dynamic labels.

    Only fixed names, bucket bounds, and the closed values declared above are
    ever interpolated into the exposition. Unexpected rows are ignored.
    """

    if not isinstance(snapshot, Mapping):
        raise TypeError("metrics snapshot must be a mapping")
    lines = [
        "# HELP study_acquisition_fetch_duration_seconds Duration of observed external fetch calls in seconds.",
        "# TYPE study_acquisition_fetch_duration_seconds histogram",
        *_histogram_lines(
            snapshot,
            key="fetch_duration",
            metric="study_acquisition_fetch_duration_seconds",
            buckets=DURATION_BUCKETS,
            outcomes=ACQUISITION_OUTCOMES,
            integer=False,
        ),
        "# HELP study_acquisition_response_body_bytes Worker-consumed response body bytes.",
        "# TYPE study_acquisition_response_body_bytes histogram",
        *_histogram_lines(
            snapshot,
            key="response_body_bytes",
            metric="study_acquisition_response_body_bytes",
            buckets=BODY_BYTES_BUCKETS,
            outcomes=ACQUISITION_OUTCOMES,
            integer=True,
        ),
        "# HELP study_acquisition_claims_total Durable acquisition worker claims.",
        "# TYPE study_acquisition_claims_total counter",
        f"study_acquisition_claims_total {_number(snapshot.get('acquisition_claims_total', 0), integer=True)}",
        "# HELP study_acquisition_jobs_total Durable terminal acquisition jobs by outcome.",
        "# TYPE study_acquisition_jobs_total counter",
    ]
    for outcome, count in _count_map(snapshot.get("acquisition_jobs_total"), ACQUISITION_OUTCOMES).items():
        lines.append(f'study_acquisition_jobs_total{{outcome="{outcome}"}} {count}')

    lines.extend(
        [
            "# HELP study_acquisition_jobs Current durable acquisition jobs by status.",
            "# TYPE study_acquisition_jobs gauge",
        ]
    )
    for status, count in _count_map(snapshot.get("acquisition_jobs"), JOB_STATUSES).items():
        lines.append(f'study_acquisition_jobs{{status="{status}"}} {count}')

    route_rows = _rows(snapshot.get("route_decisions"))
    lines.extend(
        [
            "# HELP study_teaching_route_decisions_total Durable teaching route decisions.",
            "# TYPE study_teaching_route_decisions_total counter",
        ]
    )
    for rewrite_status, reason_code in REWRITE_DECISIONS:
        count = _row_count(
            route_rows,
            {"query_rewrite_status": rewrite_status, "reason_code": reason_code},
        )
        lines.append(
            "study_teaching_route_decisions_total{"
            f'query_rewrite_status="{rewrite_status}",reason_code="{reason_code}",'
            f'answer_route="cloud"}} {count}'
        )
    fallback_count = _row_count(
        route_rows,
        {"query_rewrite_status": "fallback", "reason_code": "local_unavailable_or_invalid"},
    )
    lines.extend(
        [
            "# HELP study_teaching_fallbacks_total Durable local rewrite fallbacks.",
            "# TYPE study_teaching_fallbacks_total counter",
            "study_teaching_fallbacks_total{reason_code=\"local_unavailable_or_invalid\"} "
            f"{fallback_count}",
            "# HELP study_provider_attempts_total Durable provider attempts by closed outcome.",
            "# TYPE study_provider_attempts_total counter",
        ]
    )
    attempts = _rows(snapshot.get("provider_attempts"))
    for provider in PROVIDERS:
        for outcome in PROVIDER_OUTCOMES:
            count = _row_count(attempts, {"provider": provider, "outcome": outcome})
            lines.append(f'study_provider_attempts_total{{provider="{provider}",outcome="{outcome}"}} {count}')

    token_rows = _rows(snapshot.get("provider_tokens"))
    lines.extend(
        [
            "# HELP study_provider_tokens_total Authoritative provider-reported token usage.",
            "# TYPE study_provider_tokens_total counter",
        ]
    )
    for provider in PROVIDERS:
        for direction in ("input", "output"):
            count = _row_count(token_rows, {"provider": provider, "direction": direction})
            lines.append(f'study_provider_tokens_total{{provider="{provider}",direction="{direction}"}} {count}')

    usage_rows = _rows(snapshot.get("provider_usage"))
    lines.extend(
        [
            "# HELP study_provider_usage_total Provider attempts grouped by authoritative usage state.",
            "# TYPE study_provider_usage_total counter",
        ]
    )
    for provider in PROVIDERS:
        for state in ("reported", "missing", "unknown"):
            count = _row_count(usage_rows, {"provider": provider, "state": state})
            lines.append(f'study_provider_usage_total{{provider="{provider}",state="{state}"}} {count}')

    pending = _count_map(snapshot.get("reconciliation_pending"), ("acquisition", "teaching"))
    lines.extend(
        [
            "# HELP study_reconciliation_pending Current durable rows awaiting reconciliation.",
            "# TYPE study_reconciliation_pending gauge",
        ]
    )
    for domain, count in pending.items():
        lines.append(f'study_reconciliation_pending{{domain="{domain}"}} {count}')
    return "\n".join(lines) + "\n"
