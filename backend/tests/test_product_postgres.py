"""组合外键（Task 2 · 0003）的迁移级测试。

这一组测试对应审查确认的一个真实漏洞：

- **跨租户组合引用**：审查实测过，`tenant X` 的行指向 `tenant Y` 的项目，
  超级用户与应用角色**都能插进去** —— 单列外键 + 各自带 `tenant_id` 列
  挡不住"子行与父行属于不同租户"。修法是组合外键。

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
    yield
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM user_sessions WHERE tenant_id = %s", (TENANT,))
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
                " (session_id, tenant_id, principal_id, issued_at, expires_at,"
                " auth_method, credential_id, security_generation)"
                " VALUES (%s, %s, %s, %s, %s, 'password', %s, 1)",
                (
                    "sess_0003_bad",
                    TENANT,
                    OUTSIDER,  # 属于 OTHER_TENANT 的主体
                    NOW,
                    NOW + timedelta(days=1),
                    "cred_0003_bad",
                ),
            )
        conn.rollback()


# ---------------------------------------------------------- 邀请引导
#
# 邀请码全链路已移除：兑换函数、邀请表与相关引导语义不再存在于数据库基线与
# 运行时代码中。原先此处的「无上下文兑换 / 预绑定主体 / 单次消费 / 并发兑换」
# 用例随功能一起删除 —— 认证引导现由 `POST /auth/register`（见
# `test_round2/3/4_postgres_e2e.py` 与 `test_session_cookie_auth.py`）承担。
