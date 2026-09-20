"""worker 的跨租户凭据边界：策略按**数据库角色**限定，而不是按自定义变量。

第八份迁移。修的是第 4 轮审查的 R4-01（P1）。

## 缺陷：`app.worker_id` 不是身份凭据

0007 给 `ingestion_jobs` 加的 worker 策略是：

```sql
USING (current_setting('app.worker_id', true) IS NOT NULL)   -- 未限定角色
```

`app.worker_id` 是一个**自定义 GUC**：任何能连上库的角色都能自己
`SELECT set_config('app.worker_id', 'x', true)`。于是持有 `study_app`
连接串的人（也就是任何一个普通 HTTP 请求路径）只要设一个变量，就能在
**没有任何租户上下文**的情况下读到全部队列行。只读实测（177 行队列）：

| 连接 | 设置 | 可见行数 |
|---|---|---|
| `study_app` | 什么都不设 | 0 |
| `study_app` | `set_config('app.worker_id','x')` | **177** |
| `study_app` | `set_config('app.worker_id','')` | **177**（`IS NOT NULL` 对空串为真） |

「当前没有 HTTP 端点设置它」**不是**防线：防线一旦依赖"调用方自觉不做某件事"，
它就不再是防线。

## 修法：把凭据边界收回数据库角色

1. worker 策略加 `TO study_worker` —— 只有该角色匹配，其余角色只剩标准策略
   （租户 + 项目）；
2. 谓词收紧成 `NULLIF(current_setting('app.worker_id', true), '') IS NOT NULL`
   —— 空串是"没有上下文"，不是"有上下文"；
3. `REVOKE UPDATE ON ingestion_jobs FROM study_app`：应用角色不需要推进任务状态
   （`complete` / `fail` 是 worker 路径），收回它同时收掉了"应用角色能改队列"
   这件事本身；
4. worker 角色按最小集授权：队列表 `SELECT, UPDATE`，原文 `SELECT`，
   片段 `SELECT, INSERT`。

⚠️ 第 2 条**单独不解决问题**（自己设变量仍然通过），它只是让谓词说出它真正
想表达的意思。真正的边界是第 1 条 + 凭据分开部署
（`STUDY_PLATFORM_DSN` / `STUDY_PLATFORM_WORKER_DSN`）。

## 为什么 worker 也需要标准策略

`claim_next` 之外的三步（`load_document` / `complete` / `fail`）都带着任务自带的
租户 + 项目上下文，靠的正是 `ingestion_jobs_isolation`。两条 permissive 策略在
PostgreSQL 里是 OR 关系，worker 因此同时具备"看见队列"与"在项目内落定"两种能力 ——
而**得不到**"跨租户改别人数据"：那需要一条以租户为条件的写入，而它没有上下文。

## 为什么 `study_app` 不保留 UPDATE

`ingestion_jobs` 是唯一的状态机，推进它的只有 worker。应用角色保留 UPDATE
在功能上无用，在风险上却很具体：它让"应用路径改队列"成为一个随时可用的能力，
而下一次有人为了修一个线上问题临时用它，边界就消失了。

## 未做的两件事（诚实标注）

- **没有**新建独立的迁移角色：R4-01 的修改要求里提到"或只授予 worker 的窄接口"，
  本迁移选择窄授权（第 4 条），因为新建角色的收益（多一层隔离）不足以抵消
  两个角色在多库环境下的运维复杂度。
- **没有**枚举"哪些列可更新"：PostgreSQL 的列级 `GRANT UPDATE (col)` 与
  "整表 UPDATE"在 `information_schema` 里读起来是两回事，而这个表的状态推进
  需要同时改 status / attempt_count / lease_* / updated_at —— 列级授权在这里
  只会把规则拆成两处，且不能阻止"改错行"。行级边界由 RLS 承担。

与 0001–0007 一致的约定：迁移自包含（不复制 helper，也不引用会演进的函数）、
角色不存在显式失败、`downgrade` 真的还原（含把 0007 形态的策略**写回去**）。
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP_ROLE = "study_app"
WORKER_ROLE = "study_worker"

#: 应用角色被**收回**权限的表与其新权限集。
#: 契约生成器（`tools/skills/gen_contracts.py`）读这个常量，
#: 否则 `sql-schema.md` 会继续声称应用角色能 UPDATE 队列 —— 契约撒谎，
#: 而且没人会问"为什么这张表还留着这一层"（铁律 34 的同类问题）。
APP_ROLE_GRANTS_OVERRIDES: dict[str, str] = {
    "ingestion_jobs": "SELECT, INSERT",
}

#: worker 角色的权限（另一条凭据边界）。同样是给契约生成器读的人工声明 ——
#: 真正生效的是 `information_schema.table_privileges`，
#: 两者漂移由测试直接查数据库来发现。
WORKER_ROLE_GRANTS: dict[str, str] = {
    "ingestion_jobs": "SELECT, UPDATE",
    "source_documents": "SELECT",
    "source_chunks": "SELECT, INSERT",
}

#: 0007 形态的应用角色权限 —— `downgrade` 要**写回**它（而不是删掉权限了事）。
_LEGACY_APP_GRANTS: dict[str, str] = {
    "ingestion_jobs": "SELECT, INSERT, UPDATE",
}

#: worker 策略的谓词。空串表示"没有上下文"（`is_local => true` 的设置在事务
#: 结束后会被清掉，清掉后 `current_setting(..., true)` 给出的是空串而不是 NULL）。
_WORKER_PREDICATE = "NULLIF(current_setting('app.worker_id', true), '') IS NOT NULL"

#: 0007 形态的谓词 —— `downgrade` 要**写回**它，而不是删掉策略了事。
_LEGACY_WORKER_PREDICATE = "current_setting('app.worker_id', true) IS NOT NULL"


def _assert_roles_exist() -> None:
    """两个角色都必须存在，否则显式失败。

    与 0001–0007 一致：缺角色时给出一句能直接照做的指引，
    而不是让它以 `relation does not exist` 之类的形式在后面某处炸开。
    """
    op.execute(
        f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        RAISE EXCEPTION 'role {APP_ROLE} does not exist; '
            'create it first with scripts/sql/create_app_role.sql';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
        RAISE EXCEPTION 'role {WORKER_ROLE} does not exist; '
            'create it first with scripts/sql/create_app_role.sql '
            '(it needs -v worker_password=... as well)';
    END IF;
END
$$
"""
    )


def _worker_policy_statements() -> list[str]:
    """重建 worker 策略，把凭据边界限到角色上。

    `FOR ALL`（默认）是必需的：认领走的是 `SELECT ... FOR UPDATE`，
    它同时要求行通过 SELECT 与 UPDATE 两条策略 —— 少了任何一条都拿不到行锁，
    而错误会以"认领不到任务"的形式出现，看起来像队列空了。
    """
    return [
        "DROP POLICY IF EXISTS ingestion_jobs_worker ON ingestion_jobs",
        "CREATE POLICY ingestion_jobs_worker ON ingestion_jobs"
        f"\n    TO {WORKER_ROLE}"
        f"\n    USING ({_WORKER_PREDICATE})"
        f"\n    WITH CHECK ({_WORKER_PREDICATE})",
    ]


def _grant_statements() -> list[str]:
    statements = [
        # worker 要能拿到行锁（SELECT ... FOR UPDATE）并推进状态。
        f"GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}",
    ]
    for table, privileges in WORKER_ROLE_GRANTS.items():
        statements.append(f"GRANT {privileges} ON {table} TO {WORKER_ROLE}")
    for table, privileges in APP_ROLE_GRANTS_OVERRIDES.items():
        statements.append(f"REVOKE UPDATE ON {table} FROM {APP_ROLE}")
        # 撤权后把"应用角色应该有什么"显式写一遍：只写 REVOKE 的话，
        # 读迁移的人得自己去 0007 里减，而 0007 的常量还写着 SELECT,INSERT,UPDATE。
        statements.append(f"GRANT {privileges} ON {table} TO {APP_ROLE}")
    return statements


def _revoke_worker_statements() -> list[str]:
    statements = []
    for table in WORKER_ROLE_GRANTS:
        statements.append(f"REVOKE ALL ON {table} FROM {WORKER_ROLE}")
    statements.append(f"REVOKE ALL ON SCHEMA public FROM {WORKER_ROLE}")
    return statements


def upgrade() -> None:
    _assert_roles_exist()

    for statement in _worker_policy_statements():
        op.execute(statement)

    for statement in _grant_statements():
        op.execute(statement)


def downgrade() -> None:
    """与 upgrade 严格逆序，且**每一处都真的还原**。

    ⚠️ 策略不能只 `DROP`：那会留下一张"有 FORCE RLS 却没有 worker 策略"的表 ——
    worker 从此认领不到任何任务，而这不是"无保护"，是**整个摄取停摆**
    （铁律 30 的同型缺陷）。所以这里把 0007 形态的策略原样写回去。
    """
    # 先把权限还原成 0007 的形态：应用角色拿回 UPDATE，worker 角色的授权收回。
    # 写成显式的字面量而不是"在新权限后面拼一个 UPDATE"：还原值必须是
    # **独立写下的事实**，否则它与 upgrade 里的常量一起漂移，而漂移后
    # downgrade 出来的库与 0007 不同 —— 那时没人会去比这两处。
    for table, privileges in _LEGACY_APP_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {APP_ROLE}")

    for statement in _revoke_worker_statements():
        op.execute(statement)

    op.execute("DROP POLICY IF EXISTS ingestion_jobs_worker ON ingestion_jobs")
    op.execute(
        "CREATE POLICY ingestion_jobs_worker ON ingestion_jobs"
        f"\n    USING ({_LEGACY_WORKER_PREDICATE})"
        f"\n    WITH CHECK ({_LEGACY_WORKER_PREDICATE})"
    )
