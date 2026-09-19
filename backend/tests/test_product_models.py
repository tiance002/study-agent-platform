"""产品契约：Round 1 冻结的类型层面不变量。

这一批测试守的第一件事不是某个字段，而是「**契约只有一份**」：

- 项目模型只允许有一个（`identity/models.py` 的 `LearningProject`），
  `app.product` 里不得出现第二个；
- `sources` 本轮**没有处理状态** —— 不允许出现 `status` / `processing` / `ready`。

为什么这两条要用测试来守：它们都不会报错，只会**慢慢分叉**。
两个模型表达同一行数据时，总有一个会先被改坏而另一个不会；
一个永不触发的状态字段，看起来就像"已经支持了"。
与 `EvidenceIssueCode` 的三级诚实标注是同一个道理。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest
from app.identity.membership import MembershipStore
from app.identity.models import LearningProject
from app.product.models import (
    Conversation,
    Invitation,
    LearningPlan,
    LearningTask,
    Message,
    MessageRole,
    Milestone,
    PlanStatus,
    SourceRecord,
    TaskStatus,
    UserSession,
)

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ 契约只有一份


@pytest.mark.invariant
def test_membership_returns_the_canonical_project_type():
    """成员存储返回的项目必须是 `LearningProject`，不是它自己的私有类型。"""
    store = MembershipStore()
    project = store.create_project("proj_1", tenant_id="tenant_1", name="示例项目")
    assert type(project) is LearningProject
    assert project.project_id == "proj_1"
    assert project.tenant_id == "tenant_1"
    assert project.name == "示例项目"


@pytest.mark.invariant
def test_access_check_returns_the_canonical_project_type():
    store = MembershipStore()
    store.create_project("proj_1", tenant_id="tenant_1", name="示例项目")
    store.grant_project("tenant_1", "user_1", "proj_1")

    from app.identity.models import Principal

    project = store.assert_can_access(
        Principal(principal_id="user_1", tenant_id="tenant_1"), "proj_1"
    )
    assert type(project) is LearningProject


@pytest.mark.invariant
def test_project_record_no_longer_exists():
    """旧的 `ProjectRecord` 必须被删除，**不留兼容别名**。

    两个类同时存在时，总有一个会先被改坏而另一个不会 ——
    别名只是把"已经统一了"这句话变成一句没人验证的断言。
    """
    import app.identity.membership as membership

    assert not hasattr(membership, "ProjectRecord"), (
        "ProjectRecord 应当已删除并统一为 LearningProject"
    )


@pytest.mark.invariant
def test_product_module_defines_no_project_model():
    """`app.product` 不得定义项目模型 —— 它只放项目的**从属**实体。"""
    import app.product.models as models

    project_like = {
        name
        for name, obj in vars(models).items()
        if dataclasses.is_dataclass(obj)
        and name.lower() in {"project", "projectrecord", "learningproject"}
    }
    assert not project_like, f"product 层出现了项目模型：{sorted(project_like)}"


@pytest.mark.invariant
def test_listed_projects_carry_full_product_fields():
    """列项目要能直接喂给产品接口：list 返回的不只是 id。"""
    store = MembershipStore()
    store.create_project("proj_1", tenant_id="tenant_1", name="甲", goal="学会 Harness")
    store.grant_project("tenant_1", "user_1", "proj_1")

    from app.identity.models import Principal

    projects = store.projects_for(Principal(principal_id="user_1", tenant_id="tenant_1"))
    assert [type(p) for p in projects] == [LearningProject]
    assert projects[0].goal == "学会 Harness"


# ------------------------------------------------------- 必填字段与未知字段拒绝


@pytest.mark.invariant
def test_ownership_fields_are_mandatory():
    """归属字段缺失必须构造失败，而不是留空等运行时出问题。"""
    with pytest.raises(TypeError):
        Conversation(conversation_id="c1", title="x")  # type: ignore[call-arg]


@pytest.mark.invariant
def test_unknown_fields_are_rejected():
    """未知字段必须被拒绝：静默忽略会让拼错的字段名变成沉默的 bug。"""
    with pytest.raises(TypeError):
        Conversation(  # type: ignore[call-arg]
            conversation_id="c1",
            tenant_id="t1",
            project_id="p1",
            title="x",
            last_message_seq=0,
            created_at=NOW,
            surprise=True,
        )


@pytest.mark.invariant
@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (lambda: Conversation("", "t1", "p1", "x", 0, NOW), "conversation_id"),
        (lambda: Conversation("c1", "  ", "p1", "x", 0, NOW), "tenant_id"),
        (lambda: Conversation("c1", "t1", "", "x", 0, NOW), "project_id"),
    ],
)
def test_blank_identifiers_are_rejected(factory, field):
    with pytest.raises(ValueError, match=field):
        factory()


@pytest.mark.invariant
def test_timestamps_must_be_timezone_aware():
    """naive 时间戳会让比较静默出错（跨时区、跨进程重放都会踩）。"""
    naive = datetime(2026, 9, 19, 12, 0, 0)
    with pytest.raises(ValueError, match="时区"):
        Conversation("c1", "t1", "p1", "x", 0, naive)


# ------------------------------------------------------------------ 域不变量


@pytest.mark.invariant
@pytest.mark.parametrize("seq", [0, -1])
def test_message_sequence_must_be_positive(seq):
    with pytest.raises(ValueError, match="seq"):
        Message("m1", "t1", "p1", "c1", seq, MessageRole.USER, "你好", NOW)


@pytest.mark.invariant
def test_message_rejects_unknown_role():
    with pytest.raises(ValueError, match="role"):
        Message("m1", "t1", "p1", "c1", 1, "robot", "你好", NOW)  # type: ignore[arg-type]


@pytest.mark.invariant
def test_message_role_enum_is_closed():
    assert {r.value for r in MessageRole} == {"user", "assistant", "system"}


@pytest.mark.invariant
def test_plan_version_must_be_positive():
    with pytest.raises(ValueError, match="version"):
        LearningPlan("pl1", "t1", "p1", 0, "目标", PlanStatus.DRAFT, NOW)


@pytest.mark.invariant
def test_plan_status_enum_is_closed():
    assert {s.value for s in PlanStatus} == {"draft", "active", "archived"}


@pytest.mark.invariant
def test_task_status_enum_is_closed():
    assert {s.value for s in TaskStatus} == {"pending", "in_progress", "done", "skipped"}


@pytest.mark.invariant
def test_milestone_order_must_be_non_negative():
    with pytest.raises(ValueError, match="order_index"):
        Milestone("ms1", "t1", "p1", "pl1", -1, "第一阶段", "")


@pytest.mark.invariant
def test_task_order_must_be_non_negative():
    with pytest.raises(ValueError, match="order_index"):
        LearningTask("tk1", "t1", "p1", "ms1", -1, "读文档", TaskStatus.PENDING)


# ------------------------------------------------------------------ 邀请与会话


@pytest.mark.invariant
def test_invitation_expiry_must_be_after_issue():
    with pytest.raises(ValueError, match="expires_at"):
        Invitation("inv1", "t1", "sha256:x", "user_1", NOW, NOW)


@pytest.mark.invariant
def test_invitation_carries_only_a_token_hash():
    """邀请只存哈希 —— 原始令牌不得出现在契约里。

    这条断言看起来像废话，但它挡住了"顺手加个 token 字段方便调试"这种改动。
    """
    fields = {f.name for f in dataclasses.fields(Invitation)}
    assert "token_hash" in fields
    assert not (fields & {"token", "raw_token", "secret"}), fields


@pytest.mark.invariant
def test_session_expiry_must_be_after_issue():
    with pytest.raises(ValueError, match="expires_at"):
        UserSession("s1", "t1", "user_1", NOW, NOW)


@pytest.mark.invariant
def test_session_is_revoked_only_by_timestamp():
    """撤销是**时间戳**而不是布尔：需要知道"什么时候撤的"。"""
    live = UserSession("s1", "t1", "user_1", NOW, NOW.replace(year=2027))
    assert live.revoked_at is None
    assert live.is_live(NOW)


# ------------------------------------------------------------------ 资料登记


@pytest.mark.invariant
def test_source_record_declares_no_processing_state():
    """`sources` 本轮只登记元数据，**没有任何处理状态**。

    这个测试不是"证明字段少"，而是让**将来加状态的人必须先改这里** ——
    强迫 Round 2 在引入摄取状态机时显式面对"那时才可能有这些状态"。
    一个永不触发的状态比缺失的状态更糟：它看起来已经实现。
    """
    fields = {f.name for f in dataclasses.fields(SourceRecord)}
    forbidden = {"status", "processing_state", "progress", "failure_reason", "error"}
    assert not (fields & forbidden), f"sources 不应有处理状态字段：{fields & forbidden}"


@pytest.mark.invariant
def test_source_record_requires_stable_identity_for_dedup():
    """同一项目内同一资料不重复登记 —— 靠 `identity_hash`，不是靠显示名。"""
    fields = {f.name for f in dataclasses.fields(SourceRecord)}
    assert "identity_hash" in fields
    with pytest.raises(ValueError, match="identity_hash"):
        SourceRecord(
            source_id="src1",
            tenant_id="t1",
            project_id="p1",
            display_name="讲义.pdf",
            media_type="application/pdf",
            identity_hash="",
            registered_at=NOW,
        )
