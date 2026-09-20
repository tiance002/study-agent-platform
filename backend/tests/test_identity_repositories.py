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
from app.identity.memory_store import (
    InMemoryInvitationRepository,
    InMemorySessionRepository,
)
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


@pytest.fixture(scope="module")
def pg_seed() -> None:
    """种子（租户与主体）用超级用户写：这是运维动作，不是应用路径。

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


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _reset_pg_identity_state() -> None:
    """清掉本模块租户下的会话与邀请（超级用户连接）。

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
            conn.execute(
                "DELETE FROM invitations WHERE tenant_id IN (%s, %s)",
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
    """两个适配器，一套契约。返回 (membership, invitations, sessions)。"""
    if request.param == "memory":
        sessions = InMemorySessionRepository()
        invitations = InMemoryInvitationRepository(sessions=sessions)
        from app.identity.membership import MembershipStore

        return MembershipStore(), invitations, sessions

    from app.db.identity_store import (
        PostgresInvitationRepository,
        PostgresMembershipRepository,
        PostgresSessionRepository,
    )

    # 每个 PG 用例都从干净状态开始：本模块有绝对计数断言，不能受
    # 其他用例或上一次运行残留的会话影响（见 `_reset_pg_identity_state`）。
    _reset_pg_identity_state()
    return (
        PostgresMembershipRepository(),
        PostgresInvitationRepository(),
        PostgresSessionRepository(),
    )


def _alice(tenant: str = TENANT) -> Principal:
    return Principal(principal_id=ALICE, tenant_id=tenant)


def _bob() -> Principal:
    return Principal(principal_id=BOB, tenant_id=TENANT)


NOW_TIMDELTA = timedelta(days=1)


def _issue_args(token_hash: str, invitee: str):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return dict(
        invitation_id=_unique("inv"),
        token_hash=token_hash,
        issued_by=ALICE,
        invitee_principal_id=invitee,
        issued_at=now,
        expires_at=now + NOW_TIMDELTA,
    )


# ------------------------------------------------------------ 成员关系


@pytest.mark.invariant
def test_granted_project_visible_and_ungranted_invisible(repos):
    membership, _, _ = repos
    context = SystemContext(TENANT, "契约测试")
    project = membership.create_project(context, project_id=_unique("proj"), name="契约项目")
    membership.grant_project(context, principal_id=ALICE, project_id=project.project_id)

    alice_projects = {p.project_id for p in membership.list_for(_alice())}
    bob_projects = {p.project_id for p in membership.list_for(_bob())}

    assert project.project_id in alice_projects, "被授予者必须能看到项目"
    assert project.project_id not in bob_projects, "未授予者看不到 —— 不可见，而非可见但拒绝"


@pytest.mark.invariant
def test_get_denies_ungranted_with_unified_error(repos):
    membership, _, _ = repos
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
    membership, _, _ = repos
    context = SystemContext(TENANT, "契约测试")
    project_id = _unique("proj")
    membership.create_project(context, project_id=project_id, name="第一份")

    with pytest.raises(PlatformError) as excinfo:
        membership.create_project(context, project_id=project_id, name="第二份")
    assert excinfo.value.code is ErrorCode.BUDGET_TREE_INVALID


@pytest.mark.invariant
def test_cross_tenant_grant_is_rejected(repos):
    """授权行指向别的租户的项目 —— 内存版显式判定，数据库版组合外键。"""
    membership, _, _ = repos
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


# ------------------------------------------------------------ 邀请兑换


@pytest.mark.invariant
def test_invitation_exchange_lifecycle(repos):
    _, invitations, sessions = repos
    token_hash = "sha256:" + uuid.uuid4().hex
    invitations.issue(SystemContext(TENANT, "契约测试"), **_issue_args(token_hash, ALICE))

    session_id = _unique("sess")
    from datetime import datetime, timezone

    expires = datetime.now(timezone.utc) + NOW_TIMDELTA
    session = invitations.exchange(token_hash, session_id=session_id, session_expires_at=expires)

    assert session is not None
    assert session.tenant_id == TENANT
    assert session.principal_id == ALICE, "会话必须属于邀请预绑定的主体"
    # 兑换出的会话必须能被会话仓储查到（数据库版由函数写入 user_sessions）。
    assert sessions.get_live(_alice(), session_id) is not None

    # 单次消费：第二次兑换同一 token 必须失败。
    assert (
        invitations.exchange(token_hash, session_id=_unique("sess"), session_expires_at=expires)
        is None
    )
    # 未知 token 与已消费 token 返回**同一个**结果：None。
    assert (
        invitations.exchange("sha256:" + uuid.uuid4().hex, session_id="x", session_expires_at=expires)
        is None
    )


@pytest.mark.invariant
def test_expired_invitation_returns_none(repos):
    _, invitations, _ = repos
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    args = _issue_args("sha256:" + uuid.uuid4().hex, ALICE)
    # 已过期：签发在两天前、到期在昨天（满足 CHECK expires_at > issued_at）。
    args["issued_at"] = now - timedelta(days=2)
    args["expires_at"] = now - timedelta(days=1)
    invitations.issue(SystemContext(TENANT, "契约测试"), **args)

    assert (
        invitations.exchange(
            args["token_hash"], session_id=_unique("sess"), session_expires_at=now + NOW_TIMDELTA
        )
        is None
    )


@pytest.mark.invariant
def test_duplicate_token_hash_is_rejected(repos):
    _, invitations, _ = repos
    token_hash = "sha256:" + uuid.uuid4().hex
    invitations.issue(SystemContext(TENANT, "契约测试"), **_issue_args(token_hash, ALICE))

    with pytest.raises(PlatformError) as excinfo:
        invitations.issue(SystemContext(TENANT, "契约测试"), **_issue_args(token_hash, BOB))
    assert excinfo.value.code is ErrorCode.PARAMS_INVALID


# ------------------------------------------------------------ 会话撤销


@pytest.mark.invariant
def test_session_revocation_lifecycle(repos):
    _, _, sessions = repos
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    session_id = _unique("sess")
    from app.product.models import UserSession

    sessions.create(
        UserSession(
            session_id=session_id,
            tenant_id=TENANT,
            principal_id=ALICE,
            issued_at=now,
            expires_at=now + NOW_TIMDELTA,
        )
    )
    assert sessions.get_live(_alice(), session_id) is not None

    # 别的主体的会话**不可见**：既读不到也撤不掉。
    assert sessions.get_live(_bob(), session_id) is None
    assert sessions.revoke(_bob(), session_id, at=now) is False
    assert sessions.get_live(_alice(), session_id) is not None

    # 撤销生效；重复撤销返回 False（不是报错，也不是再次 True）。
    assert sessions.revoke(_alice(), session_id, at=now) is True
    assert sessions.get_live(_alice(), session_id) is None
    assert sessions.revoke(_alice(), session_id, at=now) is False


# ------------------------------------------------------------ 重启恢复


@pytest.mark.postgres
@pytest.mark.skipif(
    not _postgres_reachable(), reason="本地 PostgreSQL 未运行（scripts\\pg_start.cmd）"
)
def test_pg_state_survives_adapter_restart(pg_seed):
    """状态在**适配器实例之外** —— 新建的仓储对象看到的是同一份数据。

    内存版做不到这一点（进程重启即失），这正是两者的本质差别；
    但内存版可以用注入的共享字典模拟同样的可见性，作为对照。
    """
    from datetime import datetime, timezone

    from app.db.identity_store import (
        PostgresInvitationRepository,
        PostgresSessionRepository,
    )

    now = datetime.now(timezone.utc)
    token_hash = "sha256:" + uuid.uuid4().hex
    session_id = _unique("sess")

    # 进程 A：签发 + 兑换。
    first_invitations = PostgresInvitationRepository()
    first_invitations.issue(SystemContext(TENANT, "重启恢复"), **_issue_args(token_hash, ALICE))
    session = first_invitations.exchange(
        token_hash, session_id=session_id, session_expires_at=now + NOW_TIMDELTA
    )
    assert session is not None

    # 进程 B：全新对象（模拟服务重启后的新进程）。
    second_invitations = PostgresInvitationRepository()
    second_sessions = PostgresSessionRepository()

    # 消费状态持久：重放返回 None。
    assert (
        second_invitations.exchange(
            token_hash, session_id=_unique("sess"), session_expires_at=now + NOW_TIMDELTA
        )
        is None
    )
    # 会话仍然存活：重启没有丢登录态。
    assert second_sessions.get_live(_alice(), session_id) is not None


@pytest.mark.invariant
def test_revoke_all_for_contract(repos):
    """集中失效在两个适配器上语义一致：只撤自己租户+主体的存活会话。"""
    _, _, sessions = repos
    from datetime import datetime, timezone

    from app.product.models import UserSession

    now = datetime.now(timezone.utc)

    def _make(sid: str, principal: str) -> None:
        sessions.create(
            UserSession(
                session_id=sid,
                tenant_id=TENANT,
                principal_id=principal,
                issued_at=now,
                expires_at=now + NOW_TIMDELTA,
            )
        )

    keep, drop1, drop2 = _unique("sess"), _unique("sess"), _unique("sess")
    bobs = _unique("sess")
    _make(keep, ALICE)
    _make(drop1, ALICE)
    _make(drop2, ALICE)
    _make(bobs, BOB)

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


@pytest.mark.invariant
def test_exchange_rejects_session_window_beyond_ttl_cap(repos):
    """会话期限硬上限在两个适配器上同一拒绝方向（100 年会话必须建不出来）。"""
    _, invitations, sessions = repos
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    token_hash = "sha256:" + uuid.uuid4().hex
    invitations.issue(SystemContext(TENANT, "契约测试"), **_issue_args(token_hash, ALICE))

    with pytest.raises(PlatformError) as excinfo:
        invitations.exchange(
            token_hash,
            session_id=_unique("sess"),
            session_expires_at=now + timedelta(days=36500),
        )
    assert excinfo.value.code is ErrorCode.INTERNAL_CONSISTENCY_ERROR

    # 邀请没有被悬空消费：合法期限内兑换成功且会话可查。
    ok_id = _unique("sess")
    session = invitations.exchange(
        token_hash, session_id=ok_id, session_expires_at=now + NOW_TIMDELTA
    )
    assert session is not None
    assert sessions.get_live(_alice(), ok_id) is not None


@pytest.mark.invariant
def test_memory_state_visible_through_second_instance():
    """内存对照：共享同一份字典的两个实例看到同样的状态。

    这条是上一条的内存版对照 —— 它证明"注入共享字典"这个测试基建
    本身可用，且内存适配器的行为差异只来自存储位置，不来自语义。
    """
    shared_sessions: dict = {}
    shared_hash: dict = {}

    sessions_a = InMemorySessionRepository(_sessions=shared_sessions)
    invitations_a = InMemoryInvitationRepository(
        sessions=sessions_a, _by_hash=shared_hash
    )

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    token_hash = "sha256:" + uuid.uuid4().hex
    session_id = _unique("sess")
    invitations_a.issue(SystemContext(TENANT, "对照"), **_issue_args(token_hash, ALICE))
    assert (
        invitations_a.exchange(
            token_hash, session_id=session_id, session_expires_at=now + NOW_TIMDELTA
        )
        is not None
    )

    sessions_b = InMemorySessionRepository(_sessions=shared_sessions)
    invitations_b = InMemoryInvitationRepository(sessions=sessions_b, _by_hash=shared_hash)
    assert sessions_b.get_live(_alice(), session_id) is not None
    assert (
        invitations_b.exchange(
            token_hash, session_id=_unique("sess"), session_expires_at=now + NOW_TIMDELTA
        )
        is None
    )

# ------------------------------------------------------ 创建即授予 / 乐观锁


@pytest.mark.invariant
def test_create_project_for_grants_creator_immediately(repos):
    """创建即授予：同一行为里创建者必须立刻可见新项目。

    PostgreSQL 实现把 projects 与 project_grants 两条 INSERT 放进同一事务 ——
    不存在"建了项目但自己看不见"的窗口。
    """
    membership, _, _ = repos
    project = membership.create_project_for(
        _alice(), project_id=_unique("proj"), name="自建项目", goal="学会审计"
    )

    assert project.version == 1
    assert {p.project_id for p in membership.list_for(_alice())} >= {project.project_id}
    assert membership.get(_alice(), project.project_id).goal == "学会审计"


@pytest.mark.invariant
def test_update_with_expected_version_increments(repos):
    membership, _, _ = repos
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
    membership, _, _ = repos
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
    membership, _, _ = repos
    project = membership.create_project_for(
        _alice(), project_id=_unique("proj"), name="甲的项目", goal=""
    )

    with pytest.raises(PlatformError) as excinfo:
        membership.update(_bob(), project.project_id, name="抢改", goal=None, expected_version=1)
    # 统一拒绝：未授予者的更新失败与"项目不存在"同码同话术。
    assert excinfo.value.code is ErrorCode.CROSS_TENANT_DENIED
    assert membership.get(_alice(), project.project_id).name == "甲的项目"
