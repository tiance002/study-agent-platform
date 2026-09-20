"""worker 的跨租户能力必须绑在**数据库角色**上，不能绑在自定义变量上（R4-01）。

## 被锁住的性质

`app.worker_id` 是一个**自定义 GUC**：任何能连上库的角色都能
`SELECT set_config('app.worker_id', 'x', true)`。0007 的 worker 策略只判
`current_setting('app.worker_id', true) IS NOT NULL`，于是它把"能连上库"
当成了"是 worker"。

只读实测（0007，库里有 177 条任务）：

| 连接 | 设置 | 可见行数 |
|---|---|---|
| `study_app` | 无 | 0 |
| `study_app` | `app.worker_id = 'x'` | **177** |
| `study_app` | `app.worker_id = ''` | **177**（`IS NOT NULL` 对空串为真） |

所以本文件的断言是"**普通应用角色**无论怎么设置这个变量都读不到跨租户队列"，
而不是"普通客户端发不出这种请求" —— 后者依赖调用方自觉，不是防线。

## 三层分别验证（审查要求的第 4 条执行约束）

| 层 | 验证位置 |
|---|---|
| HTTP 授权 | `test_round4_postgres_e2e.py` / `test_knowledge_search.py`（越权读不到） |
| 普通应用 SQL 角色 | **本文件**（自设变量、越权 UPDATE、项目内可见性） |
| worker SQL 角色 | **本文件**（能认领、但仍不能读别的租户） |

每条否定断言都配**阳性对照**：先证明该能力对 worker 角色**确实**存在，
"应用角色没有"才有意义 —— 否则"什么都读不到"在策略整个失效时也会通过。
"""

from __future__ import annotations

import pg_support
import psycopg
import pytest

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.invariant,
    pytest.mark.skipif(
        not pg_support.reachable(),
        reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）",
    ),
]


def _visible_jobs(
    dsn: str,
    *,
    worker_id: str | None = None,
    tenant: str | None = None,
    project: str | None = None,
) -> int:
    """以 `dsn` 的身份数一数队列表里看得见几行。

    这是**纯 SQL 探针**，不经过任何应用代码：应用层的过滤会掩盖数据库层的越权，
    而这里要问的正是"数据库自己挡不挡得住"。
    """
    with psycopg.connect(dsn) as conn, conn.transaction():
        if worker_id is not None:
            conn.execute("SELECT set_config('app.worker_id', %s, true)", (worker_id,))
        if tenant is not None and project is not None:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
            conn.execute("SELECT set_config('app.project_id', %s, true)", (project,))
        return conn.execute("SELECT count(*) FROM ingestion_jobs").fetchone()[0]


@pytest.fixture
def sentinel():
    """一条真实的排队任务（含完整父链），用完删掉。

    用**迁移角色**（超级用户）播种：本文件问的是"各角色能看见什么"，
    播种本身不该受被测策略影响。
    """
    seeded = pg_support.seed_job(pg_support.migration_dsn())
    try:
        yield seeded
    finally:
        pg_support.cleanup_job(pg_support.migration_dsn(), seeded)


# ------------------------------------------------- 一、应用角色不能自开权限


def test_app_role_cannot_open_the_queue_with_a_self_set_variable(sentinel):
    """`study_app` 自设 `app.worker_id` 之后**仍然**看不到跨租户队列。

    三个取值都试：`'x'`（正常值）、`''`（空串 —— `IS NOT NULL` 会放行它）、
    以及不设。全部必须是 0（该角色在无租户上下文时看不到任何行）。
    """
    app = pg_support.app_dsn()
    assert _visible_jobs(app) == 0
    assert _visible_jobs(app, worker_id="x") == 0, (
        "应用角色自设 app.worker_id 之后能读到队列 —— 自定义变量又变成凭据了"
    )
    assert _visible_jobs(app, worker_id="") == 0, (
        "空串被当成了'有 worker 上下文'：谓词必须用 NULLIF 说清空串就是没有"
    )

    # 阳性对照：同一条任务，**worker 角色**确实看得见。
    # 没有这一条，"应用角色看不到"在策略整个失效时也会通过
    # （那时它测的是"队列里什么都没有"）。
    assert _visible_jobs(pg_support.worker_dsn(), worker_id="x") >= 1, (
        "worker 角色也看不到队列 —— 那说明策略被改坏了，上面的断言没有鉴别力"
    )
    # worker 角色不设上下文时同样看不到：承诺是"能看见队列"，
    # 但那条可见性是**显式建立**的，不是默认打开的。
    assert _visible_jobs(pg_support.worker_dsn()) == 0


def test_app_role_sees_only_its_own_project(sentinel):
    """有租户 + 项目上下文时，应用角色只看得到自己项目的任务。

    即使它同时自设了 `app.worker_id`：标准策略与 worker 策略是 OR 关系，
    而 worker 策略**不适用于这个角色**，所以越权那半边不会生效。
    """
    app = pg_support.app_dsn()
    assert (
        _visible_jobs(
            app, worker_id="x", tenant=sentinel.tenant_id, project=sentinel.project_id
        )
        == 1
    )
    assert (
        _visible_jobs(
            app, worker_id="x", tenant="t_不存在", project="proj_不存在"
        )
        == 0
    )


def test_app_role_cannot_update_the_queue(sentinel):
    """推进任务状态的能力已从应用角色收回（0008 的 REVOKE）。

    为什么必须收回而不是"约定不用"：`ingestion_jobs` 是唯一的状态机，
    应用角色留着 UPDATE 就等于"应用路径随时能改队列"。下一个为了修线上问题
    临时用它的人，会顺手把这条边界抹掉。
    """
    app = pg_support.app_dsn()
    with psycopg.connect(app) as conn, conn.transaction():
        conn.execute(
            "SELECT set_config('app.tenant_id', %s, true)", (sentinel.tenant_id,)
        )
        conn.execute(
            "SELECT set_config('app.project_id', %s, true)", (sentinel.project_id,)
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE ingestion_jobs SET attempt_count = attempt_count + 1"
                " WHERE job_id = %s",
                (sentinel.job_id,),
            )

    # 阳性对照：worker 角色能改（在会回滚的事务里改，不留痕迹）。
    class _Rollback(Exception):
        pass

    with psycopg.connect(pg_support.worker_dsn()) as conn:
        with pytest.raises(_Rollback):
            with conn.transaction():
                conn.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    (sentinel.tenant_id,),
                )
                conn.execute(
                    "SELECT set_config('app.project_id', %s, true)",
                    (sentinel.project_id,),
                )
                conn.execute(
                    "UPDATE ingestion_jobs SET attempt_count = attempt_count + 1"
                    " WHERE job_id = %s",
                    (sentinel.job_id,),
                )
                raise _Rollback

    assert pg_support.job_state(pg_support.migration_dsn(), sentinel.job_id)[
        "attempt_count"
    ] == 0, "阳性对照留下了痕迹：回滚没生效"


# ----------------------------------------------------- 二、边界本身写在哪


def test_worker_policy_is_scoped_to_the_worker_role():
    """策略必须在**角色**上限定，而不是只靠谓词。

    这条断言直接查 `pg_policies`（铁律 34：守卫要查数据库，不读声明）——
    迁移里的常量、docstring 与契约文档都可能说"已限定角色"，而真正生效的是
    这一行 `roles`。写成人工声明的话，下一次迁移重建策略时漏掉 `TO ...`
    不会有任何东西报警。
    """
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        rows = conn.execute(
            "SELECT policyname, roles::text[], qual FROM pg_policies"
            " WHERE tablename = 'ingestion_jobs' ORDER BY policyname"
        ).fetchall()

    policies = {name: (roles, qual) for name, roles, qual in rows}
    assert set(policies) == {"ingestion_jobs_isolation", "ingestion_jobs_worker"}
    worker_roles, worker_qual = policies["ingestion_jobs_worker"]
    assert worker_roles == ["study_worker"], (
        f"worker 策略的角色集是 {worker_roles} —— 必须是 study_worker 一个，"
        "否则任何角色都能自己设变量打开它"
    )
    assert "NULLIF" in worker_qual, (
        "谓词必须把空串当成'没有上下文'（is_local 的设置事务结束后会被清掉）"
    )
    # 标准策略保持"对所有角色生效"，与其它项目级表逐字一致。
    assert policies["ingestion_jobs_isolation"][0] == ["public"]


def test_worker_context_does_not_survive_the_transaction():
    """`is_local => true` 的上下文在事务结束后**不残留**。

    这是池化部署里最阴险的越权方式：连接归还池时还带着上一条连接的身份。
    谓词改成 NULLIF 之后，"清掉"与"没设过"给出同一个答案（都不可见）——
    而用 `IS NOT NULL` 时清掉后留下的是空串，恰好是**放行**。
    """
    worker = pg_support.worker_dsn()
    with psycopg.connect(worker) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.worker_id', %s, true)", ("w1",))
            inside = conn.execute("SELECT count(*) FROM ingestion_jobs").fetchone()[0]
        leftover = conn.execute(
            "SELECT NULLIF(current_setting('app.worker_id', true), '') IS NOT NULL"
        ).fetchone()[0]
        after = conn.execute("SELECT count(*) FROM ingestion_jobs").fetchone()[0]

    assert leftover is False, "事务结束后 worker 上下文仍然'有效'"
    assert after == 0, f"事务外的连接仍看得见 {after} 行队列（事务内是 {inside}）"


# ------------------------------------------- 三、worker 也不是"看得见一切"


def test_worker_role_still_cannot_read_another_tenants_source_text():
    """worker 的额外能力是**看见队列**，不是看见所有数据。

    这条最容易被写坏：把"跨租户"当成了一个整体开关。worker 读到任务行之后，
    读取原文仍然要按任务自带的租户 + 项目建立上下文 —— 拿这个上下文去读
    **别的租户**的原文必须一行都读不到。
    """
    mine = pg_support.seed_job(pg_support.migration_dsn(), tag="w_own")
    other = pg_support.seed_job(pg_support.migration_dsn(), tag="w_other")
    try:
        worker = pg_support.worker_dsn()
        with psycopg.connect(worker) as conn, conn.transaction():
            conn.execute(
                "SELECT set_config('app.tenant_id', %s, true)", (mine.tenant_id,)
            )
            conn.execute(
                "SELECT set_config('app.project_id', %s, true)", (mine.project_id,)
            )
            visible = conn.execute("SELECT document_id FROM source_documents").fetchall()
            visible_ids = {row[0] for row in visible}
            foreign = conn.execute(
                "SELECT count(*) FROM source_documents WHERE document_id = %s",
                (other.document_id,),
            ).fetchone()[0]

        # 阳性对照：自己的那份读得到。
        assert mine.document_id in visible_ids, (
            "worker 连自己项目的原文都读不到 —— 上下文没设对，下面的断言没有意义"
        )
        assert foreign == 0, "worker 用 A 项目的上下文读到了 B 租户的原文"
        assert other.document_id not in visible_ids
    finally:
        pg_support.cleanup_job(pg_support.migration_dsn(), mine)
        pg_support.cleanup_job(pg_support.migration_dsn(), other)


def test_worker_role_can_claim_through_the_repository(sentinel):
    """真实路径的阳性对照：worker 角色的适配器能认领到任务。

    前面几条都是 SQL 探针；这一条证明**装配层**用的是 worker 凭据 ——
    只改迁移不改装配的话，worker 会一条任务也认领不到，而症状是"队列空了"。
    """
    from app.db.ingestion_store import PostgresIngestionRepository
    from app.identity.membership import MembershipStore

    repository = PostgresIngestionRepository(membership=MembershipStore())
    claimed = repository.claim_next(worker_id="boundary-probe", lease_seconds=60)
    assert claimed is not None, "worker 角色的适配器认领不到任何任务"
    assert claimed.job_id == sentinel.job_id, "认领到的不是本用例播种的那条"
    assert claimed.lease_owner == "boundary-probe"
    assert claimed.attempt_count == 1
