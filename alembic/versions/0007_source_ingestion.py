"""资料摄取：原文、摄取任务与可引用片段。

第七份迁移，为第 4 轮「资料摄取与可引用检索」补三张表。

## 三张表的分工

| 表 | 回答的问题 | study_app 权限 |
|---|---|---|
| `source_documents` | 用户上传的**原文**是什么 | SELECT, INSERT（不可变） |
| `ingestion_jobs` | 这一版处理到哪一步了 | SELECT, INSERT, UPDATE（唯一状态机） |
| `source_chunks` | 可引用的最小单元是哪一段 | SELECT, INSERT（不可变） |

「不可变」由 **GRANT** 保证，不靠代码自觉：`UPDATE` 权限一旦给出去，
第一个需要"临时改一下"的人就会改掉它，而证据链上被改过的文字
与它对应的 `content_hash` 会一起变得毫无意义。

## 为什么原文与片段要分两张表

原文是**输入**，片段是**派生物**。合成一张表就得用可空的列表达"这行是原文
还是片段"，于是一行既可以"没有 span"又可以"没有原文"，而没有一处 CHECK
能同时拒掉这两种残缺 —— 那正是半完成状态最喜欢藏身的地方。

## RLS：`ingestion_jobs` 有一条**显式的** worker 策略

`source_documents` / `source_chunks` 只有一条标准策略（租户 + 项目），
与其它项目级表完全一致。

`ingestion_jobs` 多一条，因为它被两类完全不同的调用方读：

1. **状态接口**：以某个用户的身份读自己项目的任务 —— 标准策略足矣；
2. **worker**：必须先发现"哪个租户有活干"，才可能建立任何租户上下文。
   要求它带租户身份，等于要求它在知道自己要处理谁之前就声称自己是谁。

第二条不是"给个方便"，而是一个真实的系统级路径，所以它写成
**另一条有名字的策略**（`ingestion_jobs_worker`），而不是往标准谓词里塞一个
`OR ...` —— 后者会让"这条表到底怎么隔离"在某次 review 里被读漏。
两条 permissive 策略在 PostgreSQL 里是 OR 关系：

```sql
ingestion_jobs_isolation : 租户 + 项目
ingestion_jobs_worker    : current_setting('app.worker_id', true) IS NOT NULL
```

`app.worker_id` 只由 worker 命令设置（`app/workers/ingestion.py`），
没有任何 HTTP 端点设置它。**认领之后的一切写入仍走租户 + 项目上下文**：
worker 拿到任务后从任务行读出 `tenant_id` / `project_id` 再建立上下文。
所以这条策略的净效果是"能看见队列里有什么活"，而不是"能随便改谁的数据"。

## 组合外键（铁律 36 的延续）

- `(tenant_id, project_id)` → `projects`
- `(tenant_id, project_id, source_id)` → `sources`（本迁移为它补组合唯一键）
- `(tenant_id, project_id, document_id)` → `source_documents`（本迁移建表时自带）

「租户 A 的片段挂在租户 B 的原文上」在物理上无法成立。

## 两处 SQL 与 Python 契约重复的校验

`ingestion_jobs` 上两条跨列 CHECK（租约 ↔ processing、错误码 ↔ failed）
与 `knowledge/models.py` 的 `__post_init__` 是**同一条规则的两个出口**，
这通常是坏的。这里保留的原因是操作后果不对称：

- 一条 `status='processing'` 却 `lease_until IS NULL` 的行**永远无法被回收**
  （认领查询靠 `lease_until < now()` 判定过期），任务就此静默卡死；
- 一条 `failed` 却没有错误码的行，用户界面只能显示一个无法解释的失败。

写入路径只有两个适配器，读回来时 Python 契约也会再校验一次。
两道都留着，是因为"卡死"与"无法解释"都是不可修复的状态。

## 与 0001-0006 一致的约定

迁移自包含（不共享会演进的 helper）；角色不存在显式失败；downgrade 真还原。
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"

#: 本迁移新建、且启用 FORCE RLS 的表。
TABLES: list[str] = ["source_documents", "ingestion_jobs", "source_chunks"]

#: 项目级：读写上下文必须同时含 tenant 与 project。
PROJECT_SCOPED = {"source_documents", "ingestion_jobs", "source_chunks"}

#: append-only：应用角色只有 SELECT / INSERT。
#: 原文与片段都是**已经发生的事实**，改写它们等于让已有的引用指向另一段文字。
APPEND_ONLY = {"source_documents", "source_chunks"}

#: 只给 SELECT / INSERT / UPDATE 的表：状态要推进，但**不该能删除历史**。
NO_DELETE = {"ingestion_jobs"}


def _assert_app_role_exists() -> None:
    """角色不存在就显式失败 —— 与 0001-0006 一致。"""
    op.execute(
        f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        RAISE EXCEPTION 'role {APP_ROLE} does not exist; '
            'create it first with scripts/sql/create_app_role.sql';
    END IF;
END
$$
"""
    )


def _create_tables() -> list[str]:
    return [
        # 0002 只给 sources 建了 (project_id, identity_hash) 唯一键；
        # (tenant_id, project_id, source_id) 是下面组合外键的父键，在这里补。
        """
ALTER TABLE sources ADD CONSTRAINT sources_scope_key
    UNIQUE (tenant_id, project_id, source_id)
""",
        # ---------------------------------------------------------------- 原文
        # 一版一行，互不覆盖：改内容 = 上传新版本。
        # UNIQUE (source_id, version) 让并发上传的版本分配有兜底
        # （应用层算 max+1，冲突时翻译成可重试的 VERSION_CONFLICT）。
        #
        # observed_at 刻意**不给 DEFAULT**：忘记传时间就报错，
        # 而不是静默填上"入库那一刻" —— 后者会让"资料是什么时候看到的"
        # 与"什么时候上传的"混为一谈，而引用与新鲜度判定依赖前者的真实性。
        """
CREATE TABLE source_documents (
    document_id        text PRIMARY KEY,
    tenant_id          text NOT NULL REFERENCES tenants (tenant_id),
    project_id         text NOT NULL,
    source_id          text NOT NULL,
    version            integer NOT NULL CHECK (version > 0),
    document_title     text NOT NULL,
    content            text NOT NULL
        CHECK (octet_length(content) <= 1048576),
    content_hash       text NOT NULL,
    media_type         text NOT NULL
        CHECK (media_type IN ('text/plain', 'text/markdown')),
    language           text NOT NULL,
    parser_version     text NOT NULL,
    acquisition_method text NOT NULL,
    taint_sources      jsonb NOT NULL DEFAULT '[]'::jsonb,
    derived_from       jsonb NOT NULL DEFAULT '[]'::jsonb,
    observed_at        timestamptz NOT NULL,
    UNIQUE (source_id, version),
    UNIQUE (tenant_id, project_id, document_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, source_id)
        REFERENCES sources (tenant_id, project_id, source_id)
)
""",
        # ------------------------------------------------------------ 摄取任务
        # UNIQUE (document_id)：一版原文至多一个任务。重复入队因此不会
        # 悄悄产生两个 worker 同时处理同一份原文（那会写出两套片段）。
        """
CREATE TABLE ingestion_jobs (
    job_id        text PRIMARY KEY,
    tenant_id     text NOT NULL REFERENCES tenants (tenant_id),
    project_id    text NOT NULL,
    source_id     text NOT NULL,
    document_id   text NOT NULL,
    status        text NOT NULL
        CHECK (status IN ('queued', 'processing', 'succeeded', 'failed')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner   text,
    lease_until   timestamptz,
    error_code    text NOT NULL DEFAULT '',
    error_detail  text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, source_id)
        REFERENCES sources (tenant_id, project_id, source_id),
    FOREIGN KEY (tenant_id, project_id, document_id)
        REFERENCES source_documents (tenant_id, project_id, document_id),
    -- 见模块 docstring：这两条与 Python 契约重复是刻意的，
    -- 因为「卡死的任务」与「无法解释的失败」都是不可修复的状态。
    -- 写成单行是因为契约生成器按"行首关键字"识别表级约束，
    -- 跨行的布尔表达式会让它把续行当成列名。
    CHECK ((status = 'processing') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK ((status = 'failed') = (error_code <> ''))
)
""",
        # ---------------------------------------------------------------- 片段
        # span 是**原文的字符区间**，与 content 严丝合缝
        # （Python 契约里还有一条 len(content) == span_end - span_start）。
        # heading_path 用 jsonb 而不是 text：它是有结构的路径，
        # 拼成 "A / B" 之后就没法机械地按层级做前缀匹配了。
        """
CREATE TABLE source_chunks (
    chunk_id       text PRIMARY KEY,
    tenant_id      text NOT NULL REFERENCES tenants (tenant_id),
    project_id     text NOT NULL,
    source_id      text NOT NULL,
    document_id    text NOT NULL,
    chunk_index    integer NOT NULL CHECK (chunk_index >= 0),
    heading_path   jsonb NOT NULL DEFAULT '[]'::jsonb,
    heading_level  integer NOT NULL DEFAULT 0 CHECK (heading_level >= 0),
    span_start     integer NOT NULL,
    span_end       integer NOT NULL,
    content        text NOT NULL,
    content_hash   text NOT NULL,
    parser_version text NOT NULL,
    display_policy text NOT NULL DEFAULT 'full'
        CHECK (display_policy IN ('full', 'summary', 'citation_only')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index),
    CHECK (span_start >= 0 AND span_end > span_start),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, source_id)
        REFERENCES sources (tenant_id, project_id, source_id),
    FOREIGN KEY (tenant_id, project_id, document_id)
        REFERENCES source_documents (tenant_id, project_id, document_id)
)
""",
    ]


def _index_statements() -> list[str]:
    return [
        # 项目的原文列表（按时间倒序）。
        "CREATE INDEX source_documents_scope_idx"
        " ON source_documents (tenant_id, project_id, observed_at DESC)",
        # 认领查询只关心这两种状态。部分索引把索引体积压在"队列中的活"上，
        # 而不是随历史任务一起膨胀。
        "CREATE INDEX ingestion_jobs_claim_idx"
        " ON ingestion_jobs (created_at, job_id)"
        " WHERE status IN ('queued', 'processing')",
        # 检索的候选集按项目取。
        "CREATE INDEX source_chunks_scope_idx"
        " ON source_chunks (tenant_id, project_id, source_id, chunk_index)",
    ]


def _rls_statements(table: str) -> list[str]:
    """租户 + 项目谓词。与 0002 的 _standard_rls 同构，自包含副本。"""
    predicate = (
        "tenant_id = current_setting('app.tenant_id', true)\n"
        "       AND project_id = current_setting('app.project_id', true)"
    )
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_isolation ON {table}",
        f"CREATE POLICY {table}_isolation ON {table}\n"
        f"    USING ({predicate})\n"
        f"    WITH CHECK ({predicate})",
    ]


def _worker_policy() -> list[str]:
    """`ingestion_jobs` 的第二条策略：worker 的系统级可见性。

    写成独立的有名策略而不是往标准谓词里塞 `OR ...`：标准谓词读起来必须
    与其它项目级表**逐字一致**，多出来的那条权限才有机会被单独看见与审阅。

    `FOR ALL` 是必需的：认领走的是 `SELECT ... FOR UPDATE`，
    它同时要求行通过 SELECT 与 UPDATE 两条策略（否则拿不到行锁）。
    """
    escape = "current_setting('app.worker_id', true) IS NOT NULL"
    return [
        "DROP POLICY IF EXISTS ingestion_jobs_worker ON ingestion_jobs",
        "CREATE POLICY ingestion_jobs_worker ON ingestion_jobs\n"
        f"    USING ({escape})\n"
        f"    WITH CHECK ({escape})",
    ]


def _grant_statements() -> list[str]:
    statements: list[str] = []
    for table in TABLES:
        if table in APPEND_ONLY:
            statements.append(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
        elif table in NO_DELETE:
            statements.append(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {APP_ROLE}")
        else:  # pragma: no cover - 本迁移的表都在上面两类里
            statements.append(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"
            )
    return statements


def upgrade() -> None:
    _assert_app_role_exists()

    for statement in _create_tables():
        op.execute(statement)

    for table in TABLES:
        for statement in _rls_statements(table):
            op.execute(statement)

    # worker 的系统级可见性（只加在队列表上，见模块 docstring）。
    for statement in _worker_policy():
        op.execute(statement)

    for statement in _index_statements():
        op.execute(statement)

    for statement in _grant_statements():
        op.execute(statement)


def downgrade() -> None:
    """与 upgrade 严格逆序，且每一处都真的还原。

    ⚠️ 策略必须显式 DROP：`DROP TABLE` 会带走它，但显式删写在
    review 时能一眼看出"这条表的两条策略都被处理了"，
    而不是"大概随表一起没了"。`sources` 上补的组合唯一键同理 ——
    留着它会改变父表的结构语义（downgrade 后不该有任何 0007 的痕迹）。
    """
    op.execute("DROP INDEX IF EXISTS source_chunks_scope_idx")
    op.execute("DROP INDEX IF EXISTS ingestion_jobs_claim_idx")
    op.execute("DROP INDEX IF EXISTS source_documents_scope_idx")

    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
    op.execute("DROP POLICY IF EXISTS ingestion_jobs_worker ON ingestion_jobs")

    op.execute("DROP TABLE IF EXISTS source_chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS ingestion_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS source_documents CASCADE")

    op.execute("ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_scope_key")
