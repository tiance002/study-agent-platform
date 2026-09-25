"""身份仓储的**契约测试**：内存与 PostgreSQL 适配器跑同一套断言。

退出标准（本轮计划）：「内存适配器和 PostgreSQL 适配器行为一致」。
这句话的可验证形式就是本文件 —— 用参数化夹具把两个适配器分别灌进
同一批测试函数，任何一侧语义漂移（"内存版拦截、数据库版放行"）
都会在对应的参数上变红。

⚠️ **PostgreSQL 参数被 skip 就等于这一关没过** ——
退出门不允许"跳过的测试也算通过"。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pg_support
import psycopg
import pytest
from app.core.errors import ErrorCode, PlatformError
from app.identity.memory_store import InMemorySessionRepository
from app.identity.models import Principal
from app.identity.ports import SystemContext


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(pg_support.migration_dsn(), connect_timeout=2):
            return True
    except Exception:
        return False


# ------------------------------------------------------------ 夹具

TENANT = "t_rep_pg"
OTHER_TENANT = "t_rep_pg_other"
ALICE = "u_rep_pg_alice"
BOB = "u_rep_pg_bob"
OUTSIDER = "u_rep_pg_out"
CRED_ALICE = "cred_rep_pg_alice"
CRED_BOB = "cred_rep_pg_bob"
#: 只用于播种；CHECK 只要求 `$argon2id$` 前缀，密码学有效性不是本文件的对象。
SEED_HASH = "$argon2id$v=19$m=19456,t=2,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture(scope="module")
def pg_seed() -> None:
    """种子（租户、主体、凭据）用超级用户写：这是运维动作，不是应用路径。

    库不可达时**静默跳过**：postgres 参数上的 skipif 会负责跳过 PG 用例，
    内存用例不得因为库没开而被牵连 —— 两种适配器的可用性互相独立。
    """
    if not _postgres_reachable():
        return
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            for tenant in (TENANT, OTHER_TENANT):
                conn.execute(
                    "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)"
                    " ON CONFLICT (tenant_id) DO NOTHING",
                    (tenant, tenant),
                )
            for principal, tenant in ((ALICE, TENANT), (BOB, TENANT), (OUTSIDER, OTHER_TENANT)):
                conn.execute(
                    "INSERT INTO principals (principal_id, tenant_id) VALUES (%s, %s)"
                    " ON CONFLICT (principal_id) DO NOTHING",
                    (principal, tenant),
                )
            # 会话必须关联真实凭据（组合外键 + 账号生命周期语义）。
            for credential, principal, username in (
                (CRED_ALICE, ALICE, "alice_rep"),
                (CRED_BOB, BOB, "bob_rep"),
            ):
                conn.execute(
                    "INSERT INTO account_credentials"
                    " (credential_id, tenant_id, principal_id, username,"
                    "  username_normalized, password_hash, hash_version, security_generation)"
                    " VALUES (%s, %s, %s, %s, %s, %s, 1, 1)"
                    " ON CONFLICT (credential_id) DO NOTHING",
                    (credential, TENANT, principal, username, username, SEED_HASH),
                )


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _reset_pg_identity_state() -> None:
    """清掉本模块租户下的会话（超级用户连接）。

    为什么必须清：`test_revoke_all_for_contract` 断言的是**绝对条数**
    （"只撤掉本次创建的 2 条"），而会话 id 唯一、库跨用例与跨运行长期保留。
    上一次残留的存活会话会被下一次 `revoke_all_for` 一并撤掉 ——
    实测报出 `assert 66 == 2`。那不是产品缺陷（RLS 确实只圈住了
    「该租户 + 该主体」，没有越过边界），是测试没有隔离自己。
    """
    if not _postgres_reachable():
        return
    with psycopg.connect(pg_support.migration_dsn()) as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM user_sessions WHERE tenant_id IN (%s, %s)",
                (TENANT, OTHER_TENANT),
            )


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not _postgres_reachable(),
                    reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）",
                ),
            ],
            id="postgres",
        ),
    ]
)
def repos(request, pg_seed):
    """两个适配器，一套契约。返回 `(membership, sessions)`。"""
    if request.param == "memory":
        from app.identity.membership import MembershipStore

        return MembershipStore(), InMemorySessionRepository()

    from app.db.identity_store import (
        PostgresMembershipRepository,
        PostgresSessionRepository,
    )

    # 每个 PG 用例都从干净状态开始：本模块有绝对计数断言，不能受
    # 其他用例或上一次运行残留的会话影响（见 `_reset_pg_identity_state`）。
    _reset_pg_identity_state()
    return PostgresMembershipRepository(), PostgresSessionRepository()


def _alice(tenant: str = TENANT) -> Principal:
    return Principal(principal_id=ALICE, tenant_id=tenant)


def _bob() -> Principal:
    return Principal(principal_id=BOB, tenant_id=TENANT)


NOW_TIMDELTA = timedelta(days=1)


def _alice_session(session_id: str, *, tenant: str = TENANT, principal: str = ALICE, credential: str = CRED_ALICE):
    from datetime import datetime, timezone

    from app.product.models import UserSession

    now = datetime.now(timezone.utc)
    return UserSession(
        session_id=session_id,
        tenant_id=tenant,
        principal_id=principal,
        issued_at=now,
        expires_at=now + NOW_TIMDELTA,
        credential_id=credential,
        security_generation=1,
    )


# ------------------------------------------------------------ 成员关系


@pytest.mark.invariant
def test_granted_project_visible_and_ungranted_invisible(repos):
    membership, _ = repos
    context = SystemContext(TENANT, "契约测试")
    project = membership.create_project(context, project_id=_unique("proj"), name="契约项目")
    membership.grant_project(context, principal_id=ALICE, project_id=project.project_id)

    alice_projects = {p.project_id for p in membership.list_for(_alice())}
    bob_projects = {p.project_id for p in membership.list_for(_bob())}

    assert project.project_id in alice_projects, "被授予者必须能看到项目"
    assert project.project_id not in bob_projects, "未授予者看不到 —— 不可见，而非可见但拒绝"


@pytest.mark.invariant
def test_get_denies_ungranted_with_unified_error(repos):
    membership, _ = repos
    context = SystemContext(TENANT, "契约测试")
    project = membership.create_project(context, project_id=_unique("proj"), name="契约项目")
    membership.grant_project(context, principal_id=ALICE, project_id=project.project_id)

    for actor, label in ((_bob(), "同租户未授予"), (Principal(OUTSIDER, OTHER_TENANT), "跨租户")):
        with pytest.raises(PlatformError) as excinfo:
            membership.get(actor, project.project_id)
        # 统一出口：不存在 / 跨租户 / 未授予是同一种拒绝 ——
        # 差异化等于把"别的租户有哪些项目"变成可探测信息。
        assert excinfo.value.code is ErrorCode.CROSS_TENANT_DENIED, label
        assert str(excinfo.value.message) == "无权访问该项目", label

    # 正向：被授予者拿得到完整契约。
    assert membership.get(_alice(), project.project_id).project_id == project.project_id


@pytest.mark.invariant
def test_duplicate_project_is_rejected(repos):
    membership, _ = repos
    context = SystemContext(TENANT, "契约测试")
    project_id = _unique("proj")
    membership.create_project(context, project_id=project_id, name="第一份")

    with pytest.raises(PlatformError) as excinfo:
        membership.create_project(context, project_id=project_id, name="第二份")
    assert excinfo.value.code is ErrorCode.BUDGET_TREE_INVALID


@pytest.mark.invariant
def test_cross_tenant_grant_is_rejected(repos):
    """授权行指向别的租户的项目 —— 内存版显式判定，数据库版组合外键。"""
    membership, _ = repos
    context = SystemContext(TENANT, "契约测试")
    # 先建一个**属于其他租户**的项目（真实存在，才能证明防的是"错配"而非"不存在"）。
    other = membership.create_project(
        SystemContext(OTHER_TENANT, "契约测试"), project_id=_unique("proj"), name="别家项目"
    )

    with pytest.raises(PlatformError) as excinfo:
        membership.grant_project(context, principal_id=ALICE, project_id=other.project_id)
    assert excinfo.value.code is ErrorCode.CROSS_TENANT_DENIED

    # 被拒绝的授予不得留下任何痕迹。
    assert all(
        p.project_id != other.project_id for p in membership.list_for(_alice())
    ), "失败的授予不能有半份残留"


# ------------------------------------------------------------ 会话撤销


@pytest.mark.invariant
def test_session_revocation_lifecycle(repos):
    _, sessions = repos
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    session_id = _unique("sess")
    sessions.create(_alice_session(session_id))
    assert sessions.get_live(_alice(), session_id) is not None

    # 别的主体的会话**不可见**：既读不到也撤不掉。
    assert sessions.get_live(_bob(), session_id) is None
    assert sessions.revoke(_bob(), session_id, at=now) is False
    assert sessions.get_live(_alice(), session_id) is not None

    # 撤销生效；重复撤销返回 False（不是报错，也不是再次 True）。
    assert sessions.revoke(_alice(), session_id, at=now) is True
    assert sessions.get_live(_alice(), session_id) is None
    assert sessions.revoke(_alice(), session_id, at=now) is False


@pytest.mark.invariant
def test_revoke_all_for_contract(repos):
    """集中失效在两个适配器上语义一致：只撤自己租户+主体的存活会话。"""
    _, sessions = repos
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    keep, drop1, drop2 = _unique("sess"), _unique("sess"), _unique("sess")
    bobs = _unique("sess")
    sessions.create(_alice_session(keep))
    sessions.create(_alice_session(drop1))
    sessions.create(_alice_session(drop2))
    sessions.create(_alice_session(bobs, principal=BOB, credential=CRED_BOB))

    # 「退出其余设备」：保留当前会话。
    assert sessions.revoke_all_for(_alice(), at=now, except_session_id=keep) == 2
    assert sessions.get_live(_alice(), keep) is not None
    assert sessions.get_live(_alice(), drop1) is None
    assert sessions.get_live(_alice(), drop2) is None
    # 同租户别的主体不受影响。
    assert sessions.get_live(_bob(), bobs) is not None
    # 这次撤掉保留的那一条；之后幂等为 0。
    assert sessions.revoke_all_for(_alice(), at=now) == 1
    assert sessions.revoke_all_for(_alice(), at=now) == 0
    # BOB 自己撤自己。
    assert sessions.revoke_all_for(_bob(), at=now) == 1


# ------------------------------------------------------ 创建即授予 / 乐观锁


@pytest.mark.invariant
def test_create_project_for_grants_creator_immediately(repos):
    """创建即授予：同一行为里创建者必须立刻可见新项目。

    PostgreSQL 实现把 projects 与 project_grants 两条 INSERT 放进同一事务 ——
    不存在"建了项目但自己看不见"的窗口。
    """
    membership, _ = repos
    project = membership.create_project_for(
        _alice(), project_id=_unique("proj"), name="自建项目", goal="学会审计"
    )

    assert project.version == 1
    assert {p.project_id for p in membership.list_for(_alice())} >= {project.project_id}
    assert membership.get(_alice(), project.project_id).goal == "学会审计"


@pytest.mark.invariant
def test_update_with_expected_version_increments(repos):
    membership, _ = repos
    project = membership.create_project_for(
        _alice(), project_id=_unique("proj"), name="初版", goal=""
    )

    updated = membership.update(
        _alice(), project.project_id, name="改名", goal=None, expected_version=1
    )
    assert updated.version == 2
    assert updated.name == "改名"
    assert updated.goal == "", "name=None 表示不动 goal，不是清空"


@pytest.mark.invariant
def test_update_with_stale_version_conflicts(repos):
    membership, _ = repos
    project = membership.create_project_for(
        _alice(), project_id=_unique("proj"), name="初版", goal=""
    )
    membership.update(_alice(), project.project_id, name="第二版", goal=None, expected_version=1)

    with pytest.raises(PlatformError) as excinfo:
        membership.update(_alice(), project.project_id, name="迟到的编辑", goal=None, expected_version=1)
    assert excinfo.value.code is ErrorCode.VERSION_CONFLICT
    # 冲突不产生部分改动。
    assert membership.get(_alice(), project.project_id).name == "第二版"


@pytest.mark.invariant
def test_update_denies_ungranted_like_any_access(repos):
    membership, _ = repos
    project = membership.create_project_for(
        _alice(), project_id=_unique("proj"), name="甲的项目", goal=""
    )

    with pytest.raises(PlatformError) as excinfo:
        membership.update(_bob(), project.project_id, name="抢改", goal=None, expected_version=1)
    # 统一拒绝：未授予者的更新失败与"项目不存在"同码同话术。
    assert excinfo.value.code is ErrorCode.CROSS_TENANT_DENIED
    assert membership.get(_alice(), project.project_id).name == "甲的项目"


# ------------------------------------------------------------ 重启恢复


@pytest.mark.postgres
@pytest.mark.skipif(
    not _postgres_reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)
def test_pg_session_state_survives_adapter_restart(pg_seed):
    """状态在**适配器实例之外** —— 新建的仓储对象看到的是同一份数据。

    内存版做不到这一点（进程重启即失），这正是两者的本质差别。
    """
    _reset_pg_identity_state()
    from app.db.identity_store import PostgresSessionRepository

    session_id = _unique("sess")
    first = PostgresSessionRepository()
    first.create(_alice_session(session_id))

    # 「重启」：全新对象看到同一份持久状态，登录态不丢。
    second = PostgresSessionRepository()
    assert second.get_live(_alice(), session_id) is not None
