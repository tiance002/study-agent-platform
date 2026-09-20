# Round 4 Source Ingestion and Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让普通用户把纯文本/Markdown 资料加入项目，经可恢复的 PostgreSQL 摄取任务生成可核验片段，并用中文关键词搜索得到带原文引用的结果。

**Architecture:** PostgreSQL 是原文、任务、片段和状态的事实源；API 只登记内容与 durable intent，独立 worker 使用 `FOR UPDATE SKIP LOCKED` 认领任务。首版检索采用项目内标题路径加确定性关键词评分，所有过滤在 SQL/RLS 内完成，返回 `source_id + span + content_hash + parser_version`；向量、reranker、网页抓取和模型回答不进入本轮。

**Tech Stack:** Python 3.13, FastAPI, Pydantic, PostgreSQL 16, Alembic, psycopg 3, pytest.

## Global Constraints

- 复用现有 cookie 认证、严格 CSRF、RLS、HTTP 幂等和错误响应，不开第二套身份入口。
- 不改写证据原文；解析与切块只能增加结构元数据。
- `taint_sources[]` 使用 `TaintSource` 现有闭集；`acquisition_method` 与 `derived_from[]` 分开保存。
- API 不同步执行摄取重活；队列只传 `job_id`，任务内容留在 PostgreSQL。
- worker 认领使用 lease + `FOR UPDATE SKIP LOCKED`；崩溃后可重新认领，完成写入幂等。
- 搜索必须先在数据库内按 tenant/project 过滤，禁止把跨项目候选拉回应用层再筛。
- 本轮只支持 UTF-8 纯文本与 Markdown，单版本原文硬上限 1 MiB；不支持 URL、PDF、Office、OCR、embedding、reranker 或模型摘要。
- 每个任务完成后运行定向测试并提交；整轮退出门通过后才推送 GitHub。

---

### Task 12: Durable Ingestion Schema and Contracts

**Files:**
- Create: `alembic/versions/0007_source_ingestion.py`
- Create: `backend/app/knowledge/models.py`
- Create: `backend/app/knowledge/ports.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_source_ingestion_repositories.py`

**Interfaces:**
- Produces: `SourceDocument`, `IngestionJob`, `StoredChunk`, `IngestionRepository`.
- Produces tables: `source_documents`, `ingestion_jobs`, `source_chunks` with tenant/project composite foreign keys and FORCE RLS.
- `source_documents` and `source_chunks` are immutable to `study_app`; `ingestion_jobs` exposes only the columns needed for state transitions.

- [x] **Step 1: Write failing migration and repository contract tests**

```python
def test_enqueue_and_claim_are_project_scoped(env):
    document, job = env.ingestion.enqueue(actor, project_id, source_id, content="事务保证原子性")
    claimed = env.ingestion.claim_next(worker_id="worker-a", lease_seconds=30)
    assert claimed.job_id == job.job_id
    assert claimed.status is IngestionStatus.PROCESSING

def test_cross_project_worker_cannot_attach_chunks(env):
    with pytest.raises(PlatformError) as exc:
        env.ingestion.complete(actor_b, project_b, job_a.job_id, chunks=())
    assert exc.value.code is ErrorCode.CROSS_PROJECT_DENIED
```

- [x] **Step 2: Run the tests and confirm the missing contract failure**

Run: `.\.venv\Scripts\python.exe -m pytest backend/tests/test_source_ingestion_repositories.py -q`

Expected: collection fails because `app.knowledge.models` and `IngestionRepository` do not exist.

- [x] **Step 3: Add migration `0007`**

Create these columns and constraints exactly:

```text
source_documents:
  document_id PK, tenant_id, project_id, source_id, version,
  document_title, content, content_hash, media_type,
  language, parser_version, acquisition_method,
  taint_sources jsonb, derived_from jsonb, observed_at,
  UNIQUE(source_id, version), CHECK(octet_length(content) <= 1048576)

ingestion_jobs:
  job_id PK, tenant_id, project_id, source_id, document_id,
  status CHECK queued|processing|succeeded|failed,
  attempt_count, lease_owner, lease_until, error_code, error_detail,
  created_at, updated_at,
  UNIQUE(document_id)

source_chunks:
  chunk_id PK, tenant_id, project_id, source_id, document_id,
  chunk_index, heading_path jsonb, heading_level,
  span_start, span_end, content, content_hash,
  parser_version, display_policy, created_at,
  UNIQUE(document_id, chunk_index),
  CHECK(span_start >= 0 AND span_end > span_start)
```

Add composite foreign keys back to `projects`, `sources`, and `source_documents`; enable and force RLS on all three tables. Grant `source_documents` SELECT/INSERT, `source_chunks` SELECT/INSERT, and narrowly scoped `ingestion_jobs` SELECT/INSERT/UPDATE to `study_app`. Downgrade must remove all policies, tables, indexes, and grants in reverse order.

- [x] **Step 4: Implement domain models and the port**

```python
class IngestionRepository(Protocol):
    def enqueue(self, actor: Principal, project_id: str, source_id: str, *,
                document_id: str, job_id: str, title: str, content: str,
                media_type: str, language: str) -> tuple[SourceDocument, IngestionJob]: ...
    def get_job(self, actor: Principal, project_id: str, job_id: str) -> IngestionJob: ...
    def claim_next(self, *, worker_id: str, lease_seconds: int) -> IngestionJob | None: ...
    def complete(self, job: IngestionJob, chunks: tuple[StoredChunk, ...]) -> None: ...
    def fail(self, job: IngestionJob, *, error_code: str, safe_detail: str) -> None: ...
```

Validate enum values and aware timestamps in `__post_init__`; expose no update/delete method for documents or chunks.

- [x] **Step 5: Implement memory and PostgreSQL adapters under the same tests**

For PostgreSQL claim, use one transaction:

```sql
SELECT job_id
FROM ingestion_jobs
WHERE status = 'queued'
   OR (status = 'processing' AND lease_until < now())
ORDER BY created_at, job_id
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

Then conditionally update the selected row to `processing`, increment `attempt_count`, and set the lease. `complete()` must insert all chunks and transition the job to `succeeded` in one transaction; a duplicate delivery must return the existing succeeded state without duplicating chunks.

- [x] **Step 6: Run repository, RLS, migration, lint, and type gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_source_ingestion_repositories.py -q
.\.venv\Scripts\python.exe -m ruff check backend/app backend/tests/test_source_ingestion_repositories.py
.\.venv\Scripts\python.exe -m mypy backend/app
```

Expected: memory and PostgreSQL cases pass with no skips while local PG is running.

- [x] **Step 7: Commit Task 12**

```powershell
git add alembic/versions/0007_source_ingestion.py backend/app/knowledge backend/app/main.py backend/tests/test_source_ingestion_repositories.py
git commit -m "feat: add durable source ingestion storage"
```

---

### Task 13: Upload API and Deterministic Document Processor

**Files:**
- Create: `backend/app/knowledge/processor.py`
- Create: `backend/app/workers/ingestion.py`
- Modify: `backend/app/api/product_routes.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_document_processor.py`
- Test: `backend/tests/test_ingestion_api.py`

**Interfaces:**
- Consumes: `IngestionRepository.enqueue/claim_next/complete/fail` from Task 12.
- Produces: `DocumentProcessor.parse(document) -> tuple[StoredChunk, ...]`.
- Produces endpoints `POST /projects/{project_id}/sources/{source_id}/content` and `GET /projects/{project_id}/ingestion-jobs/{job_id}`.

- [x] **Step 1: Write processor boundary tests before implementation**

```python
def test_markdown_chunks_preserve_exact_source_spans():
    text = "# 事务\n\n提交成功。\n\n## 回滚\n\n失败时回滚。"
    chunks = DocumentProcessor().parse(document(text))
    for chunk in chunks:
        assert text[chunk.span_start:chunk.span_end] == chunk.content
        assert chunk.content_hash == content_hash(chunk.content)
    assert chunks[1].heading_path == ("事务", "回滚")

def test_oversized_section_splits_on_paragraph_boundaries():
    chunks = DocumentProcessor(max_chars=400, overlap_chars=40).parse(long_document())
    assert all(len(chunk.content) <= 400 for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
```

- [x] **Step 2: Run tests and confirm `DocumentProcessor` is missing**

Run: `.\.venv\Scripts\python.exe -m pytest backend/tests/test_document_processor.py -q`

Expected: FAIL at import/collection.

- [x] **Step 3: Implement Markdown/plain-text structural parsing**

Recognize ATX headings (`#` through `######`), blank-line paragraphs, fenced code blocks, lists, and tables without rewriting their text. Build heading paths with a stack. Split only sections over 4,000 characters, prefer paragraph boundaries, use at most 200 characters of overlap, and compute spans against the original document. Reject invalid UTF-8 before this boundary; do not guess titles or language.

- [x] **Step 4: Write API tests for enqueue, replay, status, and input limits**

```python
def test_cookie_user_enqueues_content_idempotently(cookie_user, source):
    first = cookie_user.post(path, headers=key("upload-1"), json=body)
    replay = cookie_user.post(path, headers=key("upload-1"), json=body)
    assert first.status_code == 202
    assert replay.json() == first.json()
    assert replay.headers["X-Idempotent-Replay"] == "true"

def test_content_over_one_mib_is_rejected_before_enqueue(cookie_user, source):
    response = cookie_user.post(path, headers=key("too-large"), json={"content": "x" * 1_048_577})
    assert response.status_code == 422
```

- [x] **Step 5: Implement enqueue and status endpoints**

Request model:

```python
class SourceContentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=1_048_576)
    media_type: Literal["text/plain", "text/markdown"]
    language: str = Field(default="zh", pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]+)*$")
```

The POST command must use `idempotent_write`, return `202`, and never call `DocumentProcessor`. Status returns only stable state, attempt count, safe error code/detail, and timestamps.

- [x] **Step 6: Implement one-shot worker command**

`python -m app.workers.ingestion --once` claims at most one job, loads the immutable document, parses it, and calls `complete`. Expected parse/input failures call `fail` with a stable code and safe detail; unexpected exceptions release only by lease expiry and exit nonzero so supervision can detect the crash.

- [x] **Step 7: Verify and commit Task 13**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_document_processor.py backend/tests/test_ingestion_api.py -q
.\.venv\Scripts\python.exe -m ruff check backend/app backend/tests/test_document_processor.py backend/tests/test_ingestion_api.py
.\.venv\Scripts\python.exe -m mypy backend/app
```

Then commit:

```powershell
git add backend/app/knowledge/processor.py backend/app/workers/ingestion.py backend/app/api/product_routes.py backend/app/main.py backend/tests/test_document_processor.py backend/tests/test_ingestion_api.py
git commit -m "feat: process uploaded learning sources"
```

---

### Task 14: Project-Scoped Keyword Retrieval and Verifiable Citations

**Files:**
- Create: `backend/app/knowledge/store.py`
- Modify: `backend/app/knowledge/retrieval.py`
- Modify: `backend/app/api/product_routes.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_knowledge_search.py`
- Test: `backend/tests/test_round4_postgres_e2e.py`
- Create: `backend/tests/fixtures/retrieval_v1.json`

**Interfaces:**
- Produces: `KnowledgeRepository.search(actor, project_id, query, limit) -> tuple[ScoredChunk, ...]` and `read_span(...) -> StoredChunk | None`.
- Produces endpoints `POST /projects/{project_id}/knowledge/search` and `GET /projects/{project_id}/sources/{source_id}/span?start=&end=`.
- Search response contains `retrieval_health`, conservative `evidence_assessment`, hits, and `ArtifactRef` citations.

- [x] **Step 1: Write isolation, ranking, and citation tests**

```python
def test_search_filters_scope_inside_repository(pg_knowledge):
    pg_knowledge.seed(project_a, "事务提交和回滚")
    pg_knowledge.seed(project_b, "只属于另一个项目的秘密词")
    hits = pg_knowledge.search(alice, project_a, "秘密词", limit=10)
    assert hits == ()

def test_each_hit_can_be_verified_against_exact_span(pg_knowledge):
    hit = pg_knowledge.search(alice, project, "回滚", limit=5)[0]
    exact = pg_knowledge.read_span(alice, project, hit.source_id, hit.span)
    assert exact.content == hit.content
    assert content_hash(exact.content) == hit.content_hash
```

- [x] **Step 2: Run the tests and confirm no PostgreSQL knowledge adapter exists**

Run: `.\.venv\Scripts\python.exe -m pytest backend/tests/test_knowledge_search.py -q`

Expected: FAIL at missing `PostgresKnowledgeRepository`.

> **实施偏差（保留原文以便对照）**：计划假设存在一个按后端分叉的知识适配器，实际落地时**没有**
> `PostgresKnowledgeRepository`。检索这一层是纯 Python 整数打分，没有任何后端相关逻辑；
> 真正按后端分叉的是**隔离**，而它已经住在 `IngestionRepository` 里（内存版靠类内判定，
> PostgreSQL 版靠 FORCE RLS + 组合外键）。硬写第二份实现只能把同一段排序代码再抄一遍，
> 于是"两个适配器行为一致"从「结构相同」退化成「两份代码碰巧一样」。
> 现在只有 `KnowledgeRepository`（`app/knowledge/store.py`），
> 隔离与回读断言在**两个** `IngestionRepository` 实现上各跑一遍。
> 理由写在 `store.py` 的模块 docstring 里。

- [x] **Step 3: Implement deterministic baseline ranking**

Normalize Unicode with NFKC and lowercasing. Preserve the original query and derive additive terms from whitespace/punctuation runs plus Chinese 2-grams. SQL must include tenant/project predicates under FORCE RLS before scoring. Score exact phrase, title/heading matches, and content term matches with fixed integer weights; tie-break by `source_id`, `chunk_index`. Do not label the result `supported` merely because hits exist.

- [x] **Step 4: Add search and exact-span APIs**

Search request: `query` 1..2,000 chars and `limit` 1..20. Build `ArtifactRef` directly from stored hash/span/parser/display policy. Pass deterministic signals to `assess_retrieval`; because this round has no frozen claim set, return `insufficient + MISSING_SUPPORT` alongside a healthy retrieval process. Exact span must return 404 for missing, cross-project, and cross-tenant rows with identical public wording.

- [x] **Step 5: Add a frozen retrieval fixture and baseline gate**

`retrieval_v1.json` must contain at least 20 Chinese queries across paragraph, heading, code, and no-hit cases with expected source/chunk ids. Add a test calculating recall@5 and MRR; freeze the initial observed baseline as the minimum. Any tokenizer, chunker, parser, or ranking version change must rerun this fixture.

- [x] **Step 6: Add PostgreSQL restart and failure recovery exit test**

The test must execute:

```text
cookie login -> create project -> register source -> upload Markdown -> API returns queued
-> fresh worker claims and completes -> restart entire PlatformState
-> Chinese query finds expected chunk -> exact span/hash verifies original text
-> another project and tenant cannot retrieve or read it
-> forced worker crash leaves processing lease -> expired lease is reclaimed once
-> repeated completion produces no duplicate chunks
```

- [x] **Step 7: Run all release gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
.\.venv\Scripts\python.exe -m ruff check backend/app backend/tests
.\.venv\Scripts\python.exe -m mypy backend/app
.\.venv\Scripts\python.exe tools/skills/gen_contracts.py --all --check
git diff --check
```

Additionally create a temporary database and prove `upgrade 0006 -> 0007 -> downgrade 0006 -> upgrade 0007`; PostgreSQL tests may not skip.

- [x] **Step 8: Update records, commit, and push Round 4**

Mark Round 4 complete in `task_plan.md`, append exact commands/results and test counts to `progress.md`, and record retrieval limitations in `findings.md`. Then:

```powershell
git add alembic backend docs backend/tests task_plan.md progress.md findings.md
git commit -m "feat: deliver source ingestion and retrieval loop"
.\.venv\Scripts\python.exe tools/push_via_api.py
```

Verify the returned remote commit using `gh api /repos/tiance002/study-agent-platform/commits/main` and record its SHA. Do not start model-generated teaching responses in this round.

## Round 4 Exit Gate

- A cookie user can register a source, upload UTF-8 plain text/Markdown, observe queued/processing/succeeded/failed states, and search it after worker completion.
- Restarting API and worker loses no document, job, chunk, idempotency result, or citation.
- Every hit round-trips through exact span and SHA-256 verification; display policy and taint metadata survive storage.
- Cross-tenant and cross-project search, ID guessing, and exact-span reads return no data.
- Worker crash/lease expiry/retry creates one immutable chunk set and one terminal job result.
- Frozen Chinese retrieval fixture meets its recorded recall@5 and MRR floors.
- Full tests, Ruff, mypy, generated contracts, migration round-trip, and diff checks pass with PostgreSQL tests actually executed.

## Explicit Deferrals

- Round 5: provider/model routing, cited teaching answers, streaming, prompt/version evaluation, cost budgets.
- Round 6: React ordinary-user UI, accessibility and browser workflow tests.
- Release round: deployment, backup/restore drill, observability, rate/load gates, user onboarding and operations runbooks.

## Post-delivery review fixes (2026-09-20)

The independent review at
`docs/superpowers/plans/2026-09-20-round-4-review.md` (baseline `9f358de`)
raised 7 findings (4×P1 + 3×P2). **All 7 were reproduced and confirmed**, and all
are now fixed. The plan above was executed as written; the findings were missed
by it, so the fix list lives here rather than as edits to the steps:

| Finding | Fix commit | Substance |
|---|---|---|
| R4-04 | `e89627c` | Tests run on a random `study_test_*` database; database-name gate before any destructive write |
| R4-01 | `7c2ec11` | Cross-tenant queue access bound to the `study_worker` **role** (migration `0008`), not to the `app.worker_id` GUC |
| R4-02 | `89b3786` | `claim_token` fencing: a stale lease holder can no longer settle a reclaimed job (migration `0009`) |
| R4-03 | `5e6a80f` | Citations carry `document_id`; exact reads go by identifier; search defaults to the latest successful version |
| R4-05 | `5ffac18` | Chunks are verified against the **persisted** source text before any write |
| R4-06 | `83b365c` | `text/plain` gets its own parse path — no more whole-document loss |
| R4-07 | `83b365c` | Fences compare length, not just character |

Also fixed along the way (same root causes, found while fixing the above):

- the composition root ignored `STUDY_PLATFORM_DSN` and fell back to a hard-coded
  DSN — the exact route by which tests reached the business database;
- the contract generator could not read `f"ALTER TABLE {TABLE} ADD COLUMN ..."`,
  so `claim_token` was missing from the generated schema contract **silently**;
- the substring-based mechanical guard fired on docstrings that merely mention
  `claim_next`, which is the kind of false positive that grows an allowlist
  (now an AST reference scan).

Deviations from the step list: the fixes are committed **per risk unit** (6 commits
instead of one), and the migration is split into `0008_worker_role_boundary` +
`0009_ingestion_claim_fencing` instead of the single `0008_ingestion_integrity`
that the round-5 plan assumed.
