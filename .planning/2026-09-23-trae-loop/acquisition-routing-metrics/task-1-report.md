# R9 Task 1 — Acquisition and routing metrics

**Status: NEEDS_CONTEXT**
**Current phase: A — read-only survey complete; implementation and tests not started.**

The survey found no existing end-to-end metrics collection path. In particular, the required fetch duration is not persisted, while the acquisition worker runs in a separate process. A real, restart-safe aggregate therefore needs a cross-process transport/storage decision. The plausible choices below each add a database migration, a shared-file mechanism, or a deployed log-metrics collector. The brief says to stop and request 6sol review if those are required, so I stopped before changing code or tests.

## Read-only findings

### What is persisted and what is process-local

- The PostgreSQL `acquisition_jobs` rows persist job status (`queued`, `running`, `succeeded`, `failed`, `unknown`), durable `attempt_count`, lease data, and safe error codes/details. Claims increment `attempt_count`; worker retries can reclaim an expired job.
- The existing `source_fetch_artifacts` table stores raw response bytes, content type/hash, parser version, and wall-clock `fetched_at`. Its body size can be derived for saved artifacts, but no fetch duration or per-attempt outcome history is stored. The schema is present in the uncommitted R9 migration `0016` in this checkout; this task must not alter it without review.
- `FetchResult` is process-local and contains final target, response content bytes, content type, and redirects. `fetch_url` uses a monotonic clock for its timeout budget, but does not return elapsed time. `workers/acquisition.py` creates an in-memory `Outcome`; neither that outcome nor fetch elapsed time is available to the web process.
- `load_artifact(job)` is the recovery path after a worker restart. When an artifact exists, the worker skips `fetch_url` and continues ingestion. That path must not produce a fetch latency/bytes sample.
- Teaching rows persist run status, `attempt_count`, and (in the uncommitted R9 migration `0017`) a closed `RoutingDecision` snapshot. `provider_attempts` persists attempt lifecycle status and authoritative token columns; a missing usage remains SQL `NULL`. Teaching run status and the attempt state preserve `reconciliation_required`/unknown state.
- Provider result objects and local route decisions begin in worker memory. The successful provider payload contains a provider status, but failure/unknown paths do not uniformly persist every underlying `ProviderStatus` as a dedicated field. Some outcomes would need safe mapping from existing closed state/error codes or careful instrumentation; raw error text is not suitable for metrics.
- The memory repositories retain their data only in the current process. There is no shared metrics registry today.

### Process and deployment boundary

- FastAPI is the Web process (`app.main:app`). Acquisition fetches run only in `app.workers.acquisition`, a separate CLI/worker process. Teaching and ingestion also have separate worker modules and production systemd units.
- Production deployment files configure Web, teaching, and ingestion systemd services. There is currently no acquisition-worker unit. The documented deployment is one ECS host with a shared `/var/lib/study-plan` runtime volume and systemd journald, but no Prometheus/OpenTelemetry collector, log-metrics parser, or metrics endpoint is configured.
- `pyproject.toml` has no Prometheus/OpenTelemetry metrics dependency. The Web process is intentionally limited to one Uvicorn worker because its current file audit sink is process-local and multi-writer unsafe.
- The application database roles are split: Web uses `study_app`, workers use `study_worker`. Existing row-level security and grants do not provide the Web process with an approved global cross-tenant metrics query path.

### Candidate metric contract

This is a proposed contract for review, not implemented or exposed yet. A Prometheus text endpoint such as `/metrics` could expose the following names, with a scraper polling the Web process:

| Name | Type / unit | Closed labels and values |
|---|---|---|
| `study_acquisition_fetch_duration_seconds` | Histogram, seconds; one observation around each real `fetch_url` call using `time.monotonic()` | `outcome`: `succeeded`, `failed`, `unknown` |
| `study_acquisition_response_body_bytes` | Histogram, bytes returned to the worker as response body | Same `outcome` set; define explicitly that this is body bytes consumed, not headers or wire bytes |
| `study_acquisition_claims_total` | Counter, claims/attempts | No labels |
| `study_acquisition_jobs_total` | Counter, terminal jobs | `outcome`: `succeeded`, `failed`, `unknown` |
| `study_acquisition_jobs` | Gauge, jobs | `status`: `queued`, `running`, `succeeded`, `failed`, `unknown` |
| `study_teaching_route_decisions_total` | Counter, decisions | `query_rewrite_status`: `disabled`, `applied`, `fallback`; `reason_code`: `local_not_configured`, `local_rewrite_accepted`, `local_unavailable_or_invalid`; `answer_route`: `cloud` |
| `study_teaching_fallbacks_total` | Counter, decisions | `reason_code`: `local_unavailable_or_invalid` |
| `study_provider_attempts_total` | Counter, attempts | `provider`: `openai`, `unknown`; `outcome`: `completed`, `refused`, `dispatch_failed`, `timeout`, `malformed`, `truncated`, `unknown` |
| `study_provider_tokens_total` | Counter, tokens | `provider`: `openai`, `unknown`; `direction`: `input`, `output`; increment only from authoritative `TokenUsage` |
| `study_provider_usage_total` | Counter, attempts | `provider`: `openai`, `unknown`; `state`: `reported`, `missing`, `unknown` |
| `study_reconciliation_entries_total` | Counter, entries | `domain`: `acquisition`, `teaching` |
| `study_reconciliation_pending` | Gauge, pending records | `domain`: `acquisition`, `teaching` |

No URL, domain, source/project/tenant/user/request/run/acquisition IDs, configured model text, exception text, query, prompt, response, or content would be a label. Missing/unknown usage would not be emitted as zero. Provider timeouts and unknowns would remain in reconciliation and would not trigger redispatch. These names and semantics need review, especially whether bytes means consumed response body bytes and how provider outcomes should be mapped where the durable attempt table currently stores only lifecycle state.

## Minimum architecture options for 6sol review

### Option 1 — PostgreSQL-backed per-attempt metric facts, Web exposition

- **Collection:** worker measures duration with a monotonic clock and body bytes, records one safe metric fact keyed by the durable acquisition attempt; provider, routing, and reconciliation facts are read/recorded from existing domain state. Web exposes aggregate counters/histograms/gauges at `/metrics`; a scraper polls that endpoint.
- **Cross-process:** PostgreSQL is the shared source, so worker restarts and multiple worker processes aggregate consistently. Per-attempt uniqueness and transaction placement can prevent worker retry/replay double-counting. Web cannot currently perform unrestricted cross-tenant reads with `study_app`, so the metric fact/query role boundary must be designed explicitly.
- **Likely files:** new `backend/app/metrics.py`; `backend/app/workers/acquisition.py` and `backend/app/knowledge/fetcher.py`; `backend/app/teaching/service.py` and/or teaching store adapters; `backend/app/db/acquisition_store.py` and `backend/app/db/teaching_store.py`; `backend/app/main.py`; a new `alembic/versions/0018_...py`; focused acquisition, teaching, exposition, and privacy tests.
- **Dependencies and risks:** no metrics library is strictly necessary for text exposition, but a new database table/role/RLS/grants migration is needed for exact cross-process fetch observations and idempotent aggregation. This is outside the approved first-round boundary and needs review.

### Option 2 — Shared runtime-volume append-only spool, Web exposition

- **Collection:** worker appends privacy-safe, bounded JSON metric samples to a dedicated file under the existing `/var/lib/study-plan` runtime volume. Web reads/deduplicates/aggregates those samples and serves `/metrics`; a scraper polls Web. Existing PG state remains the source for durable pending gauges.
- **Cross-process:** the documented single-host production setup gives systemd services access to the same runtime volume. This would not aggregate across hosts and depends on file permissions, atomic append/locking, retention/rotation, and deduplication keys. It is a new shared-file transport despite avoiding schema changes.
- **Likely files:** new `backend/app/metrics.py`; acquisition worker/fetcher instrumentation; teaching service instrumentation; `backend/app/main.py`; tests for restart, concurrency, deduplication, and privacy. Deployment permissions/rotation and the missing acquisition service may also need changes, which are outside the initial file range.
- **Dependencies and risks:** no Python dependency or database migration is required. The spool is operational state shared across processes, has weaker portability than the DB, and still requires a reviewed cross-process sharing mechanism and lifecycle policy.

### Option 3 — Structured worker events in systemd journal, log-based collection

- **Collection:** emit only closed labels and numeric measurements as structured stdout events; systemd journald already captures service output. A journal-to-metrics parser/exporter would aggregate these and provide scrape/alert functionality.
- **Cross-process:** journald can collect Web and worker output on the documented host, but there is no parser/exporter configured today. Manual `journalctl` inspection is not an aggregate metrics/alert path. Multi-host deployment would need log forwarding.
- **Likely files:** new `backend/app/metrics.py`; acquisition/teaching instrumentation; tests that prove private data never enters events; plus parser/exporter configuration and likely deployment files.
- **Dependencies and risks:** can avoid a database migration, but without an actual log-metrics collector this is only logging, not the requested operational metrics. Adding a collector/parser and service configuration expands deployment scope.

## File scope and stop decision

No implementation file is currently changed by this task. For any approved option, the anticipated code scope is limited to a metrics module, acquisition/fetch and teaching instrumentation, Web exposure/aggregation, and focused privacy/behavior tests. Option 1 additionally needs a database migration; Option 2 needs a shared spool policy; Option 3 needs a configured collector. None can be selected safely by this executor without 6sol design review because each touches an explicit stop condition or has an unresolved production collection path.

The metric survey also found existing R9 migrations and many application changes already uncommitted. I did not edit, stage, reset, clean, format, or otherwise alter them. The starting branch was `codex/round8-residual-20260922`.

## Commands and verification

- Read the task brief, `findings.md`, R9 plan task 6, acquisition worker/fetcher/artifact and repository code, teaching service/repository/model code, migrations `0014`–`0017`, `main.py`, `deployment.py`, `pyproject.toml`, and production systemd/deployment documentation.
- `rg` searches for metrics libraries/endpoints, acquisition artifacts/attempt state, provider usage/outcomes, routing, reconciliation, and deployment entrypoints confirmed the findings above.
- `git status --short --branch` confirmed the shared branch and existing uncommitted workspace changes.
- No test command was run: implementation was deliberately stopped at the architecture gate, so there are no new tests to report as passing.

## Outstanding review

6sol must select or revise the cross-process collection architecture and approve any required schema, filesystem-sharing, or deployment boundary. After that, implementation can resume with TDD and privacy regression tests. Until then, fetch latency/bytes cannot be truthfully exposed by process-local counters, and no global aggregation is claimed.
