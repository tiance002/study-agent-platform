"""针对代码审查发现的缺陷的回归测试。

每个测试对应一次**真实发现**，作用是防止同类问题重现。
审查结论与修复记录见 `progress.md`。
"""

from __future__ import annotations

import sys
import uuid

import pytest
from app.audit.sink import AuditSink, RiskLevel
from app.budget.ledger import BudgetLedger, Dimension
from app.core.clock import utc
from app.core.errors import ErrorCode, PlatformError
from app.identity.ports import SystemContext
from app.knowledge.retrieval import Chunk, ChunkIndex
from app.learning.evidence import (
    ComponentVerdict,
    Direction,
    EvidenceKind,
    IndependenceLevel,
    ObservationStrength,
    Validity,
)
from app.main import build_platform
from app.policy.taint import TaintSource
from app.tenancy.context import TenantContext, tenant_scope

PROJECT = "proj_demo"
MONEY = str(Dimension.CURRENCY_MICROS)
TOOL_CALLS = str(Dimension.TOOL_CALLS)


@pytest.fixture(autouse=True)
def _registered_project(monkeypatch, demo):
    """认证身份是真实注册得到的：把 PROJECT 换成该账号的默认项目。"""
    monkeypatch.setattr(sys.modules[__name__], "PROJECT", demo["project"])


# ------------------------------------- 1. 身份不再由请求体承载（比上一轮更彻底）


@pytest.mark.invariant
def test_request_body_carries_no_identity_fields():
    """身份的载体已从请求体彻底移除。

    这比「校验请求体与路径一致」更彻底：**没有字段可填，就没有伪造空间**。
    上一轮修的是"不一致就拒绝"，这一轮修的是"根本不给填的机会"。
    """
    from app.api.routes import ConfirmationBody, IngestBody, InteractionBody

    for model in (InteractionBody, IngestBody, ConfirmationBody):
        fields = set(model.model_fields)
        for forbidden in ("tenant_id", "principal_id", "learning_project_id", "confirmed_tools"):
            assert forbidden not in fields, f"{model.__name__} 不应接受 {forbidden}"


@pytest.mark.invariant
def test_cross_tenant_path_access_is_rejected(client, auth_headers, platform):
    """用 A 租户的令牌访问 B 租户的项目 → 404（不暴露存在性）。"""
    platform.membership.create_project(
        SystemContext("tenant_b"), project_id="proj_of_tenant_b"
    )
    response = client.get(
        "/projects/proj_of_tenant_b/mastery", headers=auth_headers(tenant_id="tenant_a")
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.invariant
def test_matching_project_is_accepted(client, auth_headers):
    response = client.post(
        f"/projects/{PROJECT}/interactions",
        json={"node_id": "intake_goal", "user_input": "x", "params": {}},
        headers={**auth_headers(), "Idempotency-Key": "regress-" + uuid.uuid4().hex},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"


# ------------------------------------------------------- 2. 审计读取按作用域过滤


@pytest.mark.invariant
def test_audit_reads_are_scoped(tmp_path):
    """审查发现：/audit 没有租户上下文，返回进程级全量记录。"""
    sink = AuditSink(tmp_path / "audit")
    sink.append("event_a", {"v": 1}, risk=RiskLevel.LOW, tenant_id="t1", project_id="p1")
    sink.append("event_b", {"v": 2}, risk=RiskLevel.LOW, tenant_id="t2", project_id="p2")
    sink.append("system_event", {"v": 3}, risk=RiskLevel.LOW)  # 无租户标记

    assert len(sink.read_scoped(tenant_id="t1", project_id="p1")) == 1
    assert len(sink.read_scoped(tenant_id="t2", project_id="p2")) == 1
    assert sink.read_scoped(tenant_id="t9") == []
    # 系统级事件不对项目接口暴露
    assert sink.read_scoped(tenant_id="t1", project_id="p1")[0]["event_type"] == "event_a"
    # 项目过滤生效：同租户不同项目互不可见
    assert sink.read_scoped(tenant_id="t1", project_id="p2") == []


@pytest.mark.invariant
def test_tenant_scope_is_covered_by_hash_chain(tmp_path):
    """租户/项目字段参与链哈希，因此不能事后补写。"""
    sink = AuditSink(tmp_path / "audit")
    sink.append("event_a", {"v": 1}, risk=RiskLevel.LOW, tenant_id="t1", project_id="p1")
    assert sink.verify_chain()

    path = sink.path
    tampered = path.read_text(encoding="utf-8").replace('"tenant_id":"t1"', '"tenant_id":"t9"')
    path.write_text(tampered, encoding="utf-8")
    assert not sink.verify_chain(), "改写租户字段必须被链校验发现"


@pytest.mark.invariant
def test_audit_api_is_scoped_to_authenticated_tenant(client, auth_headers):
    """审计读取的作用域来自**认证身份**，不来自查询参数。

    查询参数里已经没有 tenant_id 可传 —— 就算硬塞一个也不会改变身份。
    """
    headers = auth_headers()
    client.post(
        f"/projects/{PROJECT}/retrieval/chunks",
        json={"source_id": "s1", "chunks": ["内容"]},
        headers={**headers, "Idempotency-Key": "regress-" + uuid.uuid4().hex},
    )
    mine = client.get(f"/projects/{PROJECT}/audit", headers=headers).json()
    assert mine["records"] > 0

    spoofed = client.get(
        f"/projects/{PROJECT}/audit?tenant_id=tenant_other", headers=headers
    ).json()
    assert spoofed == mine, "查询参数不能改变认证身份"


# --------------------------------------------------------- 3. 预算预留的原子性


@pytest.mark.invariant
def test_multi_dimension_reservation_leaves_no_partial_state():
    """审查发现：先预留计数、再预留成本，第二次失败会留下第一次的残留。

    修法：改用一次 `batch_reserve`。这个测试用失败注入守住它。
    """
    ledger = BudgetLedger()
    ledger.open_account("acct", limits={TOOL_CALLS: 5, MONEY: 1})

    with pytest.raises(PlatformError) as exc:
        ledger.batch_reserve("acct", {TOOL_CALLS: 1, MONEY: 2})
    assert exc.value.code is ErrorCode.BUDGET_RESERVATION_FAILED

    account = ledger.account("acct")
    assert account.reserved[TOOL_CALLS] == 0, "失败后不得留下计数维度的残留"
    assert account.reserved[MONEY] == 0, "失败后不得留下成本维度的残留"
    assert ledger.open_reservations() == ()

    # 额度仍可正常使用（说明失败没有污染可用额度）
    ledger.batch_reserve("acct", {TOOL_CALLS: 1, MONEY: 1})


@pytest.mark.invariant
def test_budget_reservations_are_scoped():
    """审查发现：/budget 返回进程级全量预留。"""
    ledger = BudgetLedger()
    ledger.open_account("acct_a", tenant_id="t1", project_id="p1", limits={MONEY: 100})
    ledger.reserve("acct_a", MONEY, 10)
    ledger.open_account("acct_b", tenant_id="t2", project_id="p2", limits={MONEY: 100})
    ledger.reserve("acct_b", MONEY, 20)

    assert len(ledger.reservations_scoped(tenant_id="t1", project_id="p1")) == 1
    assert ledger.reservations_scoped(tenant_id="t1", project_id="p2") == ()
    assert ledger.reservations_scoped(tenant_id="t9") == ()


@pytest.mark.invariant
def test_child_account_inherits_scope_from_parent():
    """子账户继承租户/项目，避免调用方漏传导致账目脱离隔离范围。"""
    ledger = BudgetLedger()
    ledger.open_account("parent", tenant_id="t1", project_id="p1", limits={MONEY: 100})
    ledger.grant_to_child("parent", "child", {MONEY: 50})
    child = ledger.account("child")
    assert child.tenant_id == "t1"
    assert child.project_id == "p1"


@pytest.mark.invariant
def test_budget_api_is_scoped_to_authenticated_tenant(client, auth_headers):
    headers = auth_headers()
    client.post(
        f"/projects/{PROJECT}/interactions",
        json={
            "node_id": "diagnose_prerequisites",
            "user_input": "x",
            "params": {"targets": ["agent.harness"]},
        },
        headers={**headers, "Idempotency-Key": "regress-" + uuid.uuid4().hex},
    )
    body = client.get(f"/projects/{PROJECT}/budget", headers=headers).json()
    assert body["open_reservations"] == []
    assert body["needs_reconciliation"] == []


# ------------------------------------------------- 4. 精确回读不得绕过作用域


@pytest.mark.invariant
def test_read_span_is_scoped_to_tenant_and_project():
    """审查发现：`read_source_span` 直接遍历内部列表，绕过租户与项目过滤。"""
    index = ChunkIndex()
    with tenant_scope(TenantContext("t1", "u", "p1")):
        index.add(
            Chunk(
                chunk_id="c1",
                tenant_id="t1",
                learning_project_id="p1",
                source_id="s1",
                span=(0, 5),
                text="hello",
                origin=TaintSource.UPLOADED_SOURCE,
            )
        )
        assert index.read_span("s1", (0, 5)) is not None

    with tenant_scope(TenantContext("t1", "u", "p2")):
        assert index.read_span("s1", (0, 5)) is None, "同租户不同项目不得读取"

    with tenant_scope(TenantContext("t2", "u", "p1")):
        assert index.read_span("s1", (0, 5)) is None, "不同租户不得读取"


# ------------------------------------------------------- 5. 投影输入按作用域过滤


@pytest.mark.invariant
def test_mastery_projection_is_scoped(tmp_path):
    """审查延伸发现：投影输入未按租户/项目过滤，会把多个项目的证据混在一起。

    修法：先按作用域过滤再投影 —— 投影器只负责重建，不负责隔离。
    """
    platform = build_platform(var_dir=tmp_path)
    log = platform.evidence_log

    def write(tenant: str, project: str, component: str) -> None:
        log.append(
            tenant_id=tenant,
            project_id=project,
            kind=EvidenceKind.LEARNING,
            task_id="task",
            contract_id="ctr",
            mapping_version="map/v1",
            graph_version="graph/v1",
            occurred_at=utc(2026, 9, 18),
            verdicts=(
                ComponentVerdict(
                    component_id=component,
                    assessment_validity=Validity.VALID,
                    observation_strength=ObservationStrength.OBS_4,
                    source_reliability_ok=True,
                    independence_level=IndependenceLevel.PRACTICED,
                    direction=Direction.POSITIVE,
                    independence_group="g1",
                ),
            ),
        )

    write("t1", "p1", "comp.for_p1")
    write("t1", "p2", "comp.for_p2")

    with tenant_scope(TenantContext("t1", "u", "p1")):
        projection = platform.runtime.projection()
    assert [c.component_id for c in projection.components] == ["comp.for_p1"]

    with tenant_scope(TenantContext("t1", "u", "p2")):
        other = platform.runtime.projection()
    assert [c.component_id for c in other.components] == ["comp.for_p2"]
