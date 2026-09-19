# Round 1: Persistence and Product Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the development-only in-memory product path with a PostgreSQL-backed, invitation-only product foundation that survives process restarts and exposes stable APIs for the later user-facing application.

**Architecture:** Keep the existing FastAPI modular monolith and domain rules. Add explicit repository ports under the owning modules, PostgreSQL adapters under `app/db`, and a product facade in `app/api`; every project-scoped transaction sets tenant and project context before accessing RLS-protected rows. Authentication moves from pasted bearer tokens to one-time invitation exchange plus an HTTP-only signed session cookie, while bearer authentication remains available only for compatibility tests and operations.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, psycopg 3, PostgreSQL 16, Alembic, pytest, Ruff, mypy.

## Global Constraints

- Do not modify the frontend in this round; the existing demo remains only as a developer diagnostic surface.
- PostgreSQL is the source of truth for product state. In-memory adapters remain injectable test doubles, not production fallbacks.
- Every project-level row contains `tenant_id` and `learning_project_id` or `project_id` and is protected by forced RLS.
- Application connections use the `study_app` role with `NOSUPERUSER` and `NOBYPASSRLS`; migration connections use a separate DDL-capable role.
- Authentication identities come only from a verified session. Request bodies never accept `tenant_id`, `principal_id`, or project ownership claims.
- Invitations are single-use, expire, and are stored only as hashes. Raw invitation tokens never enter logs, audit payloads, or database rows.
- Mutating product endpoints require `Idempotency-Key`; `http_idempotency` is the only layer that decides replay versus changed-body conflict. Business tables never interpret a client key.
- Existing Policy Gateway, confirmation, audit, taint, evidence, and tool-boundary invariants remain unchanged.
- No model provider, hybrid retrieval, reranker, React UI, public signup, billing, or sandbox work belongs in this round.

---

## File Map

- `alembic/versions/0002_product_foundation.py`: product tables, indexes, grants, forced RLS, append-only constraints.
- `backend/app/identity/ports.py`: membership, invitation, and session repository protocols.
- `backend/app/identity/models.py`: the single canonical `LearningProject` identity/ownership model, alongside `Principal`.
- `backend/app/product/models.py`: conversation, message, plan, milestone, task, and source metadata contracts; it does not define another project model.
- `backend/app/product/ports.py`: product repository protocols used by API/application services.
- `backend/app/product/service.py`: product commands and queries; no SQL and no HTTP types.
- `backend/app/db/identity_store.py`: PostgreSQL membership, invitation, and session adapter.
- `backend/app/db/product_store.py`: PostgreSQL product-state adapter.
- `backend/app/db/idempotency_store.py`: atomic claim/result persistence for HTTP commands.
- `backend/app/identity/cookie_auth.py`: cookie extraction and signed-session authentication.
- `backend/app/api/auth_routes.py`: invitation exchange and logout routes.
- `backend/app/api/product_routes.py`: projects, conversations, plans, and sources facade.
- `backend/app/main.py`: adapter selection and dependency composition.
- `backend/tests/test_product_models.py`: pure contract tests.
- `backend/tests/test_product_api.py`: API behavior using in-memory test adapters.
- `backend/tests/test_invitation_auth.py`: invitation/session boundary tests.
- `backend/tests/test_product_postgres.py`: RLS, concurrency, and restart tests.
- `backend/tests/test_round1_recovery.py`: process reconstruction and persisted-state recovery.

---

### Task 1: Freeze Product Contracts and Migration

**Files:**
- Create: `backend/app/product/__init__.py`
- Create: `backend/app/product/models.py`
- Create: `backend/app/product/ports.py`
- Create: `backend/tests/test_product_models.py`
- Create: `alembic/versions/0002_product_foundation.py`
- Modify: `backend/app/identity/models.py`
- Modify: `backend/app/identity/membership.py`
- Modify: `tools/skills/gen_contracts.py`（`sql_schema` 渲染器；计划原写的 `gen_sql_schema.py` 不存在）

**Interfaces:**
- Produce one canonical `LearningProject` in `identity/models.py`; migrate `MembershipStore` from its private `ProjectRecord` to that type and remove `ProjectRecord` after all callers are updated.
- Produce `Conversation`, `Message`, `LearningPlan`, `Milestone`, `LearningTask`, `SourceRecord`, `Invitation`, and `UserSession` immutable contracts. `product/models.py` must not define `Project` or duplicate project identity/name fields.
- Produce repository protocols with create/get/list/update methods that always receive a verified `Principal` or explicit trusted tenant/project context.

- [ ] Write failing tests proving `MembershipStore.create_project()` and product API/service code return the same `LearningProject` type, with no second project dataclass in `app.product`; also prove IDs, timestamps, enum values, and ownership fields are mandatory and unknown fields are rejected.
- [ ] Run `\.venv\Scripts\python -m pytest backend/tests/test_product_models.py -q`; expect failures because `app.product` does not exist.
- [ ] Implement frozen dataclasses or strict Pydantic models with `extra="forbid"` and explicit status enums.
- [ ] Add migration `0002_product_foundation.py` for `invitations`, `user_sessions`, `conversations`, `messages`, `learning_plans`, `milestones`, `learning_tasks`, `sources`, and `http_idempotency`. Do not add `source_chunks` or ingestion-job state in Round 1; those belong to the Round 2 document-processing migration.
- [ ] Give `http_idempotency` the single client-command uniqueness constraint `(tenant_id, principal_id, command_scope, client_key)`. Store nullable `project_id` for scope/audit filtering and `request_hash` for replay-versus-conflict comparison, but do not put `client_key` on business tables.
- [ ] Add only domain uniqueness constraints to business tables: project grants by member/project, message sequence by conversation, plan version by project, milestone/task position by parent, and source identity by project. These constraints report invariant violations; they never decide HTTP replay semantics.
- [ ] Enable and force RLS on every tenant table. Project tables read both `app.tenant_id` and `app.project_id`; project listing uses a membership-aware policy rather than accepting a client project context.
- [ ] Revoke direct update/delete on append-only message and idempotency history where applicable; expose only the minimum grants required by `study_app`.
- [ ] Update SQL contract generation so the generated contract reflects migrations rather than the current AST placeholder.
- [ ] Run model tests, `alembic upgrade head`, and `python tools/skills/gen_contracts.py --target sql-schema --check`; expect all to pass.
- [ ] Commit as `feat: add product foundation schema and contracts`.

**Exit gate:** A fresh database migrates from zero to head and back to the previous revision in development; every new project row is tenant scoped and RLS protected.

### Task 2: PostgreSQL Identity and Product Repositories

**Files:**
- Create: `backend/app/identity/ports.py`
- Create: `backend/app/db/identity_store.py`
- Create: `backend/app/db/product_store.py`
- Create: `backend/app/product/memory_store.py`
- Create: `backend/tests/test_product_postgres.py`
- Modify: `backend/app/identity/membership.py`
- Modify: `backend/app/db/session.py`

**Interfaces:**
- Produce `MembershipRepository`, `InvitationRepository`, `SessionRepository`, and `ProductRepository` protocols.
- `PostgresProductStore` methods open transactions through `tenant_transaction`; no caller receives a raw connection.
- Preserve `MembershipStore` as an in-memory implementation of the new protocol.

- [ ] Write failing contract tests that run the same membership and product behavior against memory and PostgreSQL adapters.
- [ ] Add a tenant-only transaction helper for project listing and project creation; keep project-scoped reads on `tenant_transaction`.
- [ ] Implement atomic invitation claim with `UPDATE ... WHERE consumed_at IS NULL AND expires_at > now RETURNING`.
- [ ] Implement project create/list/get, conversation append/list, plan replace/read, and source metadata create/list/status methods.
- [ ] Bind every SQL parameter; do not interpolate tenant, project, principal, or token values into SQL.
- [ ] Add cross-tenant and cross-project tests using two application-role connections.
- [ ] Add concurrency tests proving one invitation can be claimed only once. HTTP idempotency concurrency belongs to Task 4 so it has one implementation and one test owner.
- [ ] Run `\.venv\Scripts\python -m pytest backend/tests/test_product_postgres.py -q`; expect pass or the existing explicit PostgreSQL skip when the database is unavailable.
- [ ] Commit as `feat: add postgres identity and product stores`.

**Exit gate:** Memory and PostgreSQL adapters satisfy the same behavior; application-role SQL cannot bypass RLS.

### Task 3: Invitation Exchange and Cookie Session

**Files:**
- Create: `backend/app/identity/cookie_auth.py`
- Create: `backend/app/api/auth_routes.py`
- Create: `backend/tests/test_invitation_auth.py`
- Modify: `backend/app/identity/auth.py`
- Modify: `backend/app/identity/session.py`
- Modify: `backend/app/main.py`
- Modify: `.env.example`

**Interfaces:**
- `POST /auth/invitations/exchange` consumes `{token}` and sets `study_session` as `HttpOnly`, `SameSite=Lax`, and `Secure` outside development.
- `POST /auth/logout` revokes the session and clears the cookie.
- Existing `GET /me` reads the cookie first; bearer tokens remain an explicit compatibility adapter for tests and operations.

- [ ] Write failing tests for valid exchange, expired invitation, replay, malformed token, revoked session, logout, cookie flags, and absence of raw tokens in responses.
- [ ] Store only `sha256(raw_token)`; compare fixed-length digests and return the same public error for unknown, expired, and consumed invitations.
- [ ] Issue a database-backed session record and a signed opaque cookie referencing its session ID.
- [ ] Add CSRF origin checks for cookie-authenticated unsafe methods; bearer-authenticated compatibility calls remain unaffected.
- [ ] Ensure request and audit logs redact invitation and session material.
- [ ] Run `\.venv\Scripts\python -m pytest backend/tests/test_invitation_auth.py backend/tests/test_confirmation_and_auth.py -q`.
- [ ] Commit as `feat: add invitation cookie authentication`.

**Exit gate:** A user can enter through a one-time invite without a terminal or pasted bearer token; replay and revoked cookies fail closed.

### Task 4: Durable Command Idempotency and Project Product API

**Files:**
- Create: `backend/app/product/service.py`
- Create: `backend/app/api/product_routes.py`
- Create: `backend/app/db/idempotency_store.py`
- Create: `backend/app/api/idempotency.py`
- Create: `backend/tests/test_product_api.py`
- Create: `backend/tests/test_http_idempotency_postgres.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- `GET /projects` lists projects granted to the authenticated principal.
- `POST /projects` accepts `{name, goal?}` and atomically creates the project plus owner grant.
- `GET /projects/{project_id}` returns product-facing fields only.
- `PATCH /projects/{project_id}` accepts a version and editable fields; stale versions return a conflict.
- `IdempotencyStore.claim(tenant_id, principal_id, command_scope, key, request_hash, project_id=None)` returns owner, replay, changed-body conflict, or in-progress.
- `IdempotencyStore.complete(claim_id, status_code, response_body)` atomically stores the bounded reusable response.

- [ ] Write failing tests for empty project list, create, list, detail, rename, exact replay, changed-body key reuse, stale version, and cross-tenant concealment.
- [ ] Write failing PostgreSQL tests for simultaneous claims from two application instances, project creation before a `project_id` exists, and the same client key reused under a different principal or command scope.
- [ ] Add strict request/response models with length bounds and no identity fields.
- [ ] Implement atomic insert-on-conflict claim in `http_idempotency`. Compare `request_hash` only after the unique command identity matches: equal hash means replay/in-progress; unequal hash means conflict.
- [ ] Persist only bounded response payloads and hashes. A database business-constraint violation is an internal invariant error unless the idempotency record already classifies the request as a replay.
- [ ] Implement project commands in `ProductService`; routes perform authentication and serialization only.
- [ ] Use one database transaction for project creation and owner grant.
- [ ] Record low-risk audit events with tenant, project, principal, request, and idempotency references.
- [ ] Run `\.venv\Scripts\python -m pytest backend/tests/test_http_idempotency_postgres.py backend/tests/test_product_api.py backend/tests/test_api.py -q`.
- [ ] Commit as `feat: add durable command and project api`.

**Exit gate:** An invited user can create and recover a project through stable product APIs without knowing tenant IDs; every duplicate command is classified only by `http_idempotency` before business insertion.

### Task 5: Conversations, Plans, and Source Metadata API

**Files:**
- Modify: `backend/app/api/product_routes.py`
- Modify: `backend/app/product/service.py`
- Modify: `backend/app/db/product_store.py`
- Modify: `backend/app/product/memory_store.py`
- Modify: `backend/tests/test_product_api.py`

**Interfaces:**
- `GET/POST /projects/{project_id}/conversations`
- `GET /projects/{project_id}/conversations/{conversation_id}/messages`
- `POST /projects/{project_id}/conversations/{conversation_id}/messages`
- `GET/PUT /projects/{project_id}/plan`
- `GET/POST /projects/{project_id}/sources`
- `GET /projects/{project_id}/sources/{source_id}`

- [ ] Write failing tests for conversation order, append-only message sequence, plan version replacement, milestone/task ordering, source metadata registration, and project-boundary rejection.
- [ ] Require `Idempotency-Key` on message, plan, and source mutations.
- [ ] Persist user messages before dispatching later model work; this round records assistant messages only when supplied by a trusted application service.
- [ ] Store only source registration metadata (`source_id`, project ownership, display name, media type, acquisition metadata, and `registered_at`). Do not define or return `processing`/`ready`; Round 2 adds ingestion jobs, chunks, and their state machine when those transitions can actually occur.
- [ ] Return public DTOs that omit taint endorsements, internal storage paths, policy snapshots, and audit internals.
- [ ] Run `\.venv\Scripts\python -m pytest backend/tests/test_product_api.py -q`.
- [ ] Commit as `feat: add conversation plan and source apis`.

**Exit gate:** The future frontend can render projects, conversations, plans, and registered source metadata using product APIs only; it makes no claim that a source has been processed.

### Task 6: Integrate Existing Interactions with Durable Command Idempotency

**Files:**
- Modify: `backend/app/api/product_routes.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/app/workflow/runtime.py`
- Modify: `backend/tests/test_http_idempotency_postgres.py`

**Interfaces:**
- Consume the Task 4 `IdempotencyStore`; product mutations and `/interactions` share the same command identity and replay contract.
- Runtime tool-call idempotency remains a separate lower layer because it protects external side effects, not HTTP response replay.

- [ ] Extend the Task 4 tests to `/interactions`: exact replay, changed-body conflict, failed pre-dispatch validation, abandoned in-progress claim, and a runtime action ending in `unknown`.
- [ ] Route `/interactions` through the same command scope and request-hash rules before invoking the workflow runtime.
- [ ] Define retention metadata without adding a deletion worker in this round.
- [ ] Make validation failures before claim completion safely retryable; never replay an unknown external side effect as a fresh action.
- [ ] Preserve existing runtime tool-call idempotency semantics beneath the HTTP command layer.
- [ ] Run `\.venv\Scripts\python -m pytest backend/tests/test_http_idempotency_postgres.py backend/tests/test_idempotency_and_tracing.py -q`.
- [ ] Commit as `feat: persist command idempotency`.

**Exit gate:** Duplicate requests across workers or restarts do not create duplicate projects, messages, plans, sources, confirmations, or interactions; HTTP replay and tool-side-effect idempotency remain distinct and ordered layers.

### Task 7: Production Adapter Composition and Restart Recovery

**Files:**
- Create: `backend/app/config.py`
- Create: `backend/tests/test_round1_recovery.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/tests/conftest.py`
- Modify: `.env.example`

**Interfaces:**
- `STUDY_PLATFORM_STORAGE=memory|postgres`; development tests may inject memory, while deployment configuration must explicitly select PostgreSQL.
- `build_platform(settings=...)` composes compatible adapters without importing PostgreSQL dependencies in memory-only installations.

- [ ] Write failing tests that create invite, session, project, conversation, message, plan, and source; rebuild the app; then read all state through public APIs.
- [ ] Add validated application settings and fail startup when PostgreSQL mode lacks a usable DSN or required migration revision.
- [ ] Replace unconditional demo seeding with an explicit development seed command; production startup never creates demo tenants or users.
- [ ] Update `/healthz` to report configured adapter types and migration readiness without exposing DSNs or credentials.
- [ ] Keep memory-mode fixtures fast and deterministic; mark only database integration tests as `postgres`.
- [ ] Run recovery tests once in memory mode and once in PostgreSQL mode; the PostgreSQL run must survive application reconstruction.
- [ ] Commit as `feat: compose persistent production adapters`.

**Exit gate:** PostgreSQL mode starts without demo state, reports readiness accurately, and restores product data after app reconstruction.

### Task 8: Round 1 Release Gate and Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/skills/contracts/protocol.md`
- Modify: `docs/skills/contracts/sql-schema.md`
- Modify: `docs/superpowers/plans/2026-09-18-study-agent-platform-implementation-plan.md`
- Create: `docs/reports/2026-09-19-round-1-verification.md`

**Interfaces:**
- Document one supported developer startup path and one PostgreSQL-backed pilot path.
- Record exact test counts, skips, migration revision, and known deferrals.

- [ ] Update README to remove the stale `.venv` statement and pasted-token instructions from the user path.
- [ ] Regenerate protocol and SQL contracts and run their `--check` modes.
- [ ] Run `\.venv\Scripts\python -m pytest -q`; expect all non-environmental tests to pass and only documented PostgreSQL tests to skip when the database is stopped.
- [ ] Start PostgreSQL and run `\.venv\Scripts\python -m pytest -m postgres -q`; expect all PostgreSQL tests to pass.
- [ ] Run `\.venv\Scripts\python -m ruff check .`, `\.venv\Scripts\python -m mypy backend/app`, manifest validation, contract checks, and `git diff --check`.
- [ ] Execute the restart-recovery scenario and record evidence in the verification report.
- [ ] Update the master implementation plan statuses using measured results, not optimistic labels.
- [ ] Commit as `docs: record round one persistence verification`.

**Round exit gate:** An invitation-only user can establish a cookie session and create/read projects, conversations, messages, plans, and source metadata through PostgreSQL-backed APIs; data survives app restart; cross-tenant access, invitation replay, and duplicate commands are mechanically rejected.

## Deferred to Round 2

- Model provider and generated learning plans.
- Ingestion jobs and source processing states, document parsing, title hierarchy, semantic chunking, embeddings, hybrid retrieval, and reranking.
- Assistant response generation and citation rendering.
- Tool-call repair loops beyond the already specified runtime boundaries.
- React product UI and Playwright browser workflow.
