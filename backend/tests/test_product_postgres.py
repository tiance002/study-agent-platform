"""组合外键与邀请引导（Task 2 · 0003）的迁移级测试。

这一组测试对应审查确认的两个真实漏洞：

- **跨租户组合引用**：审查实测过，`tenant X` 的行指向 `tenant Y` 的项目，
  超级用户与应用角色**都能插进去** —— 单列外键 + 各自带 `tenant_id` 列
  挡不住"子行与父行属于不同租户"。修法是组合外键。
- **邀请兑换没有引导通道**：兑换时还不知道租户，而邀请表受租户 RLS 保护，
  `study_app` 无上下文查它是 **0 行**；同时邀请没绑定被邀请主体，
  只能让客户端自报身份。修法是预绑定 + `SECURITY DEFINER` 兑换函数。

需要本地 PostgreSQL；**这组测试被 skip 就等于这一关没过** ——
退出门不允许"跳过的测试也算通过"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pg_support
import psycopg
import pytest

NOW = datetime.now(timezone.utc)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(pg_support.migration_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not _postgres_reachable(),
        reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）",
    ),
]

TENANT = "t_0003"
OTHER_TENANT = "t_0003_other"
ALICE = "u_0003_alice"
BOB = "u_0003_bob"
OUTSIDER = "u_0003_other"
PROJECT = "p_0003"
#: 真实存在、但属于**另一个租户**的项目。组合外键的鉴别力全靠它：
#: 若引用一个不存在的项目，单列外键也会拒绝 —— 那证明不了任何事。
OTHER_PROJECT = "p_0003_other"
TOKEN_HASH = "sha256:" + "a" * 64
TOKEN_HASH_2 = "sha256:" + "b" * 64
TOKEN_CONCURRENT = "sha256:" + "c" * 64


@pytest.fixture(scope="module")
def seeded() -> None:
    """种子用**超级用户**写：种子是运维动作，不受应用角色 RLS 限制。"""
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            for tenant, name in ((TENANT, "T0003"), (OTHER_TENANT, "Other")):
                conn.execute(
                    "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                    " ON CONFLICT (tenant_id) DO NOTHING",
                    (tenant, name),
                )
            for principal, tenant in ((ALICE, TENANT), (BOB, TENANT), (OUTSIDER, OTHER_TENANT)):
                conn.execute(
                    "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                    " ON CONFLICT (principal_id) DO NOTHING",
                    (principal, tenant),
                )
            conn.execute(
                "INSERT INTO projects (project_id, tenant_id, name) VALUES (%s, %s, %s)"
                " ON CONFLICT (project_id) DO NOTHING",
                (PROJECT, TENANT, "T0003 project"),
            )
            conn.execute(
                "INSERT INTO projects (project_id, tenant_id, name) VALUES (%s, %s, %s)"
                " ON CONFLICT (project_id) DO NOTHING",
                (OTHER_PROJECT, OTHER_TENANT, "Other tenant project"),
            )
            conn.execute(
                "INSERT INTO project_grants (tenant_id, principal_id, project_id)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (TENANT, ALICE, PROJECT),
            )
            conn.execute(
                "INSERT INTO invitations"
                " (invitation_id, tenant_id, token_hash, issued_by,"
                "  invitee_principal_id, issued_at, expires_at)"
                " VALUES ('inv_0003_live', %s, %s, %s, %s, %s, %s),"
                "        ('inv_0003_second', %s, %s, %s, %s, %s, %s),"
                "        ('inv_0003_concurrent', %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (invitation_id) DO NOTHING",
                (
                    TENANT, TOKEN_HASH, ALICE, ALICE,
                    NOW, NOW + timedelta(days=1),
                    TENANT, TOKEN_HASH_2, ALICE, ALICE,
                    NOW, NOW + timedelta(days=1),
                    TENANT, TOKEN_CONCURRENT, ALICE, ALICE,
                    NOW, NOW + timedelta(days=1),
                ),
            )
    yield
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM user_sessions WHERE tenant_id = %s", (TENANT,))
            conn.execute("DELETE FROM invitations WHERE tenant_id IN (%s, %s)", (TENANT, OTHER_TENANT))
            conn.execute("DELETE FROM project_grants WHERE tenant_id = %s", (TENANT,))
            conn.execute(
                "DELETE FROM projects WHERE project_id IN (%s, %s)",
                (PROJECT, OTHER_PROJECT),
            )
            conn.execute(
                "DELETE FROM principals WHERE principal_id IN (%s, %s, %s)",
                (ALICE, BOB, OUTSIDER),
            )
            conn.execute("DELETE FROM tenants WHERE tenant_id IN (%s, %s)", (TENANT, OTHER_TENANT))


def _app_connect() -> psycopg.Connection:
    return psycopg.connect(pg_support.app_dsn())


def _exchange(conn: psycopg.Connection, token_hash: str) -> list[tuple]:
    """应用角色调用兑换函数。**不设任何租户/主体上下文** —— 这正是引导场景。"""
    rows = conn.execute(
        "SELECT session_id, tenant_id, principal_id, expires_at"
        " FROM exchange_invitation(%s, %s, %s)",
        (token_hash, "sess_" + token_hash[-12:], NOW + timedelta(days=1)),
    ).fetchall()
    conn.commit()
    return rows


# ---------------------------------------------------------- 组合外键
#
# ⚠️ 为什么不拿应用角色去插 conversations？项目级表的 WITH CHECK
# 同时校验租户与项目维度，跨租户组合会被 **RLS 先拦下** —— 轮不到外键。
# 组合外键真正的防线在 RLS 看不见的两处：超级用户/运维连接，
# 以及只有"租户+主体"策略的 `http_idempotency`（它插错配组合时
# RLS 拦不住，外键是唯一防线）。测试按这两个真实场景写。


@pytest.mark.invariant
def test_cross_tenant_project_reference_is_rejected_physically(seeded):
    """超级用户绕过 RLS 时，组合外键是**唯一**的防线 —— 它必须独立成立。"""
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO conversations"
                " (conversation_id, tenant_id, project_id, title)"
                " VALUES (%s, %s, %s, %s)",
                ("conv_0003_bad", TENANT, OTHER_PROJECT, "mismatched"),
            )
        conn.rollback()


@pytest.mark.invariant
def test_idempotency_project_reference_is_tenant_bound(seeded):
    """`http_idempotency` 只有"租户+主体"策略：RLS 拦不住项目错配，
    组合外键必须在应用路径上真的拒掉它。

    这条是审查发现 2 在**正常应用路径**上的直接验证：
    上下文全部合法（租户对、主体对），只有 project_id 指向别的租户。
    """
    with _app_connect() as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT,))
        conn.execute("SELECT set_config('app.principal_id', %s, true)", (ALICE,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO http_idempotency"
                " (claim_id, tenant_id, principal_id, command_scope, client_key,"
                "  request_hash, project_id, state)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    "claim_0003_bad",
                    TENANT,
                    ALICE,
                    "POST /projects/p_x/sources",
                    "key_0003",
                    "sha256:probe",
                    OTHER_PROJECT,  # 属于别的租户的项目
                    "pending",
                ),
            )
        conn.rollback()


@pytest.mark.invariant
def test_cross_tenant_principal_reference_is_rejected_physically(seeded):
    """会话的 principal 必须真的属于该租户（超级用户视角，纯外键语义）。"""
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO user_sessions"
                " (session_id, tenant_id, principal_id, issued_at, expires_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    "sess_0003_bad",
                    TENANT,
                    OUTSIDER,  # 属于 OTHER_TENANT 的主体
                    NOW,
                    NOW + timedelta(days=1),
                ),
            )
        conn.rollback()


# ---------------------------------------------------------- 邀请引导


@pytest.mark.invariant
def test_exchange_works_without_any_context(seeded):
    """兑换发生在**没有任何身份**的时刻 —— 不能要求先有租户上下文。

    用应用角色、不设任何 set_config。改前这甚至查不到邀请行（RLS 0 行），
    只能靠客户端自报身份；函数必须自己搞定引导。
    """
    with _app_connect() as conn:
        rows = _exchange(conn, TOKEN_HASH)
    assert len(rows) == 1
    session_id, tenant_id, principal_id, _expires = rows[0]
    assert tenant_id == TENANT
    assert principal_id == ALICE, "兑换出的会话必须属于邀请预绑定的主体"


@pytest.mark.invariant
def test_invitation_is_bound_to_its_invitee(seeded):
    """客户端不能通过兑换决定自己是谁 —— 主体在签发时已绑定。"""
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        row = conn.execute(
            "SELECT invitee_principal_id FROM invitations WHERE token_hash = %s",
            (TOKEN_HASH_2,),
        ).fetchone()
    assert row is not None and row[0] == ALICE


@pytest.mark.invariant
def test_exchange_consumes_exactly_once(seeded):
    """第二次兑换同一 token 必须失败 —— 单次消费是原子语义。"""
    with _app_connect() as conn:
        first = _exchange(conn, TOKEN_HASH_2)
        second = _exchange(conn, TOKEN_HASH_2)
    assert len(first) == 1
    assert second == [], "已消费的邀请必须不能再兑换"


@pytest.mark.invariant
def test_unknown_and_consumed_return_the_same_empty_result(seeded):
    """未知 / 已消费 / 过期返回**同一个**公开结果 —— 不给探针留缝。

    这里用「都不存在可区分的行」来表达统一：兑换结果为空即唯一可见的事实。
    """
    with _app_connect() as conn:
        unknown = _exchange(conn, "sha256:" + "f" * 64)
        consumed = _exchange(conn, TOKEN_HASH_2)
    assert unknown == [] and consumed == []


# ---------------------------------------------------------- 并发兑换


@pytest.mark.invariant
def test_concurrent_exchanges_produce_exactly_one_session(seeded):
    """并发兑换同一 token：行锁让其余请求看到"已消费"，**只产生一条会话**。

    为什么这条不能省：顺序兑换两次只证明 `WHERE consumed_at IS NULL`
    在"事后"有效，证明不了两个事务**同时**到达时的行为 ——
    原子性恰恰只在那一个瞬间成立。函数的 UPDATE 走单行，
    行锁会把并发串行化；但"应该如此"要测过才算数，
    本项目的并发教训（默认 GIL 间隔下无锁实现也能全过）已经交过学费。
    """
    import threading

    attempts = 6
    barrier = threading.Barrier(attempts)
    results: list[object] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            with _app_connect() as conn:
                rows = conn.execute(
                    "SELECT session_id, tenant_id, principal_id"
                    " FROM exchange_invitation(%s, %s, %s)",
                    (TOKEN_CONCURRENT, "sess_concurrent_" + f"{threading.get_ident()}", NOW + timedelta(days=1)),
                ).fetchall()
                conn.commit()
                outcome = rows
        except Exception as exc:  # noqa: BLE001 — 收集全部异常用于断言
            outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [
        r for r in results
        if isinstance(r, list) and len(r) == 1
    ]
    assert len(successes) == 1, (
        f"期望恰好一次兑换成功，实际 {len(successes)} 次：{results!r}"
    )
    assert successes[0][0][2] == ALICE, "兑换出的会话必须属于预绑定主体"

    # 每个成功兑换都写了**自己**的会话行；失败者什么都没留下。
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        sessions = conn.execute(
            "SELECT count(*) FROM user_sessions"
            " WHERE principal_id = %s AND session_id LIKE %s",
            (ALICE, "sess_concurrent_%"),
        ).fetchone()[0]
    assert sessions == 1, f"并发兑换应只落一条会话，实际 {sessions} 条"
