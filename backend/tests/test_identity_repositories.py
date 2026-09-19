"""身份仓储的**契约测试**：内存与 PostgreSQL 适配器跑同一套断言。

退出标准（本轮计划）：「内存适配器和 PostgreSQL 适配器行为一致」。
这句话的可验证形式就是本文件 —— 用参数化夹具把两个适配器分别灌进
同一批测试函数，任何一侧语义漂移（"内存版拦截、数据库版放行"）
都会在对应的参数上变红。

⚠️ **PostgreSQL 参数被 skip 就等于这一关没过** ——
退出门不允许"跳过的测试也算通过"。
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import psycopg
import pytest
from app.core.errors import ErrorCode, PlatformError
from app.identity.memory_store import (
    InMemoryInvitationRepository,
    InMemorySessionRepository,
)
from app.identity.models import Principal
from app.identity.ports import SystemContext

MIGRATION_DSN = os.environ.get(
    "STUDY_PLATFORM_MIGRATION_DSN",
    "postgresql://postgres@127.0.0.1:5432/study_platform",
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(MIGRATION_DSN, connect_timeout=2):
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
    with psycopg.connect(MIGRATION_DSN) as conn:
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
