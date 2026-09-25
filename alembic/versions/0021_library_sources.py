"""用户级共享知识库：主体级资料与库内原文。

第二十一份迁移，为「用户级持久共享知识库」补两张表。

链序说明：本迁移接在 `0020`（learning_tasks 任务详情列）之后，两者由不同
工作流并行开发、集成时串成单头 `0019 → 0020 → 0021`。

## 为什么是「主体级」而不是「项目级」

知识库要回答的问题是「**我**上传过哪些资料」，而不是「**这个项目**里有哪些资料」。
它必须跨项目复用：在知识库面板登记一次，之后所有项目直接关联，不重复上传。

因此这两张表**没有 `project_id`**，策略只约束 `tenant_id + principal_id`：

| 表 | 回答的问题 | study_app 权限 |
|---|---|---|
| `library_sources` | 这个用户登记过哪份材料 | SELECT, INSERT |
| `library_documents` | 这份材料在库里存了哪一版原文 | SELECT, INSERT |

「关联到项目」**不是**在这两张表上写 `project_id`，而是由服务端在目标项目内
**复用同一份材料**生成项目级 `sources` 行（同 `identity_hash`）—— 见
`app/api/library_routes.py`。这样检索、引用与项目级 RLS 的不变量**零改动**：
项目里看到的仍然是一条普通的 `sources`，知识库只是它的来源。

## RLS：本人可见，跨主体不可见

谓词与 0002 的 `PRINCIPAL_SCOPED` 模板同构：

```sql
tenant_id = current_setting('app.tenant_id', true)
AND principal_id = current_setting('app.principal_id', true)
```

`app.principal_id` 在既有认证路径上早已设置（`db/session.py` 的
`principal_transaction` / `set_principal_context`），主体来自已验签的会话声明，
**客户端无法通过任何请求字段改变它**。缺上下文时两个 `current_setting(..., true)`
都返回 NULL，`= NULL` 恒假 —— 忘记设置上下文退化成"查不到"，而不是"查到全部"。

## 组合外键（铁律 36 的延续）

`library_documents` 带 `(tenant_id, principal_id, library_source_id)` 组合外键指向
`library_sources` 的同名组合唯一键。「主体 A 的原文挂在主体 B 的资料上」
在物理上无法成立 —— 不是靠应用层记得传对。

## 与 0001-0019 一致的约定

迁移自包含（不共享会演进的 helper）；角色不存在时显式失败；downgrade 真还原。
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"

#: 本迁移新建、且启用 FORCE RLS 的表（按外键依赖顺序）。
TABLES: list[str] = ["library_sources", "library_documents"]

#: 主体级：读写上下文必须同时含 tenant 与 principal（**没有** project 维度）。
PRINCIPAL_SCOPED = {"library_sources", "library_documents"}

#: append-only：应用角色只有 SELECT / INSERT。
#: 资料登记与库内原文都是**已经发生的事实**，改它们等于让"我上传过什么"失真。
APPEND_ONLY = {"library_sources", "library_documents"}


def _assert_app_role_exists() -> None:
    """角色不存在就显式失败 —— 与 0001-0019 一致。"""
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
        # -------------------------------------------------------------- 资料登记
        # 去重键是 `(tenant_id, principal_id, identity_hash)`：同一主体内
        # 同一份材料重复登记返回既有记录（幂等成功，不是报错）。
        # `(tenant_id, principal_id, library_source_id)` 是子表的组合外键父键。
        #
        # `identity_hash` 由服务端从 acquisition 的规范化 JSON 派生
        # （`core/hashing.acquisition_identity_hash`）—— 客户端声称的"同一资料"
        # 不算数，规范化 JSON 的哈希才算。它与项目级 `sources.identity_hash`
        # **同一算法**，所以关联时能命中同一条项目资料。
        """
CREATE TABLE library_sources (
    library_source_id text PRIMARY KEY,
    tenant_id         text NOT NULL REFERENCES tenants (tenant_id),
    principal_id      text NOT NULL,
    display_name      text NOT NULL,
    media_type        text NOT NULL DEFAULT '',
    identity_hash     text NOT NULL,
    acquisition       jsonb NOT NULL DEFAULT '{}'::jsonb,
    registered_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, principal_id, identity_hash),
    UNIQUE (tenant_id, principal_id, library_source_id)
)
""",
        # -------------------------------------------------------------- 库内原文
        # 一版一行，互不覆盖（与 `source_documents` 同一语义）：改内容 = 存新版本。
        # `library_source_id` 上的最新版本就是"库里有原文"的判据 ——
        # 不另设 `has_content` 布尔列：那需要成对更新，是另一处 check-then-act。
        #
        # `observed_at` 刻意**不给 DEFAULT**：忘记传时间就报错，
        # 而不是静默填上"入库那一刻"。
        """
CREATE TABLE library_documents (
    library_document_id text PRIMARY KEY,
    tenant_id           text NOT NULL REFERENCES tenants (tenant_id),
    principal_id        text NOT NULL,
    library_source_id   text NOT NULL,
    version             integer NOT NULL CHECK (version > 0),
    content             text NOT NULL
        CHECK (octet_length(content) <= 1048576),
    content_hash        text NOT NULL,
    parser_version      text NOT NULL,
    observed_at         timestamptz NOT NULL,
    UNIQUE (library_source_id, version),
    FOREIGN KEY (tenant_id, principal_id, library_source_id)
        REFERENCES library_sources (tenant_id, principal_id, library_source_id)
)
""",
    ]


def _index_statements() -> list[str]:
    return [
        # 主体知识库列表（按登记时间稳定排序）。
        "CREATE INDEX library_sources_owner_idx"
        " ON library_sources (tenant_id, principal_id, registered_at)",
        # 取某份资料的最新版本原文。`UNIQUE (library_source_id, version)`
        # 已经是可用的索引，但它按 `(library_source_id, version)` 升序；
        # 这里显式再建一条降序索引没有额外收益，因此不建 ——
        # 取最新版本走唯一索引的反向扫描即可。
    ]


def _rls_statements(table: str) -> list[str]:
    """租户 + 主体谓词。与 0002 的 `_standard_rls(principal_scoped=True)` 同构，自包含副本。"""
    predicate = (
        "tenant_id = current_setting('app.tenant_id', true)\n"
        "       AND principal_id = current_setting('app.principal_id', true)"
    )
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_isolation ON {table}",
        f"CREATE POLICY {table}_isolation ON {table}\n"
        f"    USING ({predicate})\n"
        f"    WITH CHECK ({predicate})",
    ]


def _grant_statements() -> list[str]:
    # 两张表都在 APPEND_ONLY 里：只有 SELECT / INSERT，没有 UPDATE / DELETE。
    # worker 角色**不需要**任何权限 —— 知识库只由认证请求读写，
    # 摄取与抓取仍发生在项目级表上。
    return [f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}" for table in TABLES]


def upgrade() -> None:
    _assert_app_role_exists()

    for statement in _create_tables():
        op.execute(statement)

    for table in TABLES:
        for statement in _rls_statements(table):
            op.execute(statement)

    for statement in _index_statements():
        op.execute(statement)

    for statement in _grant_statements():
        op.execute(statement)


def downgrade() -> None:
    """与 upgrade 严格逆序，每一处都真的还原。

    策略必须显式 DROP：`DROP TABLE` 会带走它们，但显式删写在 review 时
    能一眼看出"这两张表的策略都被处理了"。
    """
    op.execute("DROP INDEX IF EXISTS library_sources_owner_idx")

    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")

    op.execute("DROP TABLE IF EXISTS library_documents CASCADE")
    op.execute("DROP TABLE IF EXISTS library_sources CASCADE")