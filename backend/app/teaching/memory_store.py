"""教学运行仓储的内存实现（开发适配器）。

与 PostgreSQL 实现（`db.teaching_store`）跑同一套参数化契约测试，
两侧**可观察行为**必须一致：

- 原子性靠一把锁（PG 靠单事务）；
- 容量判定走 `budget.ports.assert_capacity`（与 PG 共享的纯不变量）；
- 状态迁移走 `budget.ports.transition` / `runs.RUN_TRANSITIONS`
  （两个适配器、一个判定出口）。

⚠️ 进程重启即失 —— 这正是第五轮要修的问题本身：
`teaching_runs` 的持久化语义只有 PG 适配器真正具备，
内存版只保证**同一套规则**，不假装具备持久性。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from app.budget.ports import (
    PRICE_VERSION,
    BudgetAccountFacts,
    ReservationState,
    assert_capacity,
    estimate_micro,
    transition,
    usage_to_micro,
)
from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, PlatformError, deny
from app.identity.models import Principal
from app.identity.ports import MembershipRepository
from app.product.memory_store import InMemoryProductRepository
from app.product.models import MessageRole
from app.teaching.models import TokenUsage
from app.teaching.routing import RoutingDecision
from app.teaching.runs import (
    Grounding,
    RunClaim,
    RunStatus,
    TeachingEvent,
    TeachingRun,
)


@dataclass
class _LiveClaim:
    """进行中的认领（内部状态；对外只暴露 RunClaim 的不可变快照）。"""

    claim_token: str
    worker_id: str
    lease_until: datetime


@dataclass
class _Attempt:
    """provider attempt 的可变记录（对外只暴露持久化 payload）。"""

    attempt_id: str
    run_id: str
    status: str  # dispatched / unknown / completed / failed
    provider_request_id: str = ""
    payload: dict | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_micro: int | None = None
    request_payload: dict | None = None


@dataclass
class _Reservation:
    reservation_id: str
    run_id: str
    state: ReservationState
    estimated_micro: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    actual_micro: int | None = None


@dataclass
class _Budget:
    scope: str
    total_micro: int
    reserved_micro: int = 0
    in_flight_micro: int = 0
    spent_micro: int = 0

    def facts(self) -> BudgetAccountFacts:
        return BudgetAccountFacts(
            scope=self.scope,
            total_micro=self.total_micro,
            reserved_micro=self.reserved_micro,
            in_flight_micro=self.in_flight_micro,
            spent_micro=self.spent_micro,
            price_version=PRICE_VERSION,
        )


@dataclass
class InMemoryTeachingRepository:
    """教学运行的内存实现。聚合 runs / attempts / events / 预算于一把锁：
    `start_run` 与 `finish_run` 的原子性都在这一个临界区里。"""

    membership: MembershipRepository
    products: InMemoryProductRepository
    clock: Clock = field(default_factory=SystemClock)
    _runs: dict[str, TeachingRun] = field(default_factory=dict)
    _events: dict[str, list[TeachingEvent]] = field(default_factory=dict)
    _event_seq: dict[str, int] = field(default_factory=dict)
    _attempts: dict[str, _Attempt] = field(default_factory=dict)
    _reservations: dict[str, _Reservation] = field(default_factory=dict)
    _tenant_budgets: dict[str, _Budget] = field(default_factory=dict)
    _project_budgets: dict[tuple[str, str], _Budget] = field(default_factory=dict)
    _claims: dict[str, _LiveClaim] = field(default_factory=dict)
    _answer_seq: dict[str, int] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # ------------------------------------------------------------ 用户路径

    def start_run(
        self,
        actor: Principal,
        project_id: str,
        conversation_id: str,
        *,
        run_id: str,
        question: str,
        model_id: str,
        prompt_version: str,
        ranking_version: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        budget_total_micro: int,
        budget_max_input_tokens: int,
        budget_max_output_tokens: int,
    ) -> TeachingRun:
        self.membership.get(actor, project_id)
        # 会话可见性：借产品仓储的判定（同一出口，不复制一份归属检查）。
        self.products.get_conversation(actor, project_id, conversation_id)

        estimated_micro = estimate_micro(estimated_input_tokens, estimated_output_tokens)
        with self._lock:
            # ---- 预算：账户不存在则按部署配置建；容量不足整体拒绝。
            tenant_budget = self._tenant_budgets.setdefault(
                actor.tenant_id, _Budget("tenant", budget_total_micro)
            )
            project_budget = self._project_budgets.setdefault(
                (actor.tenant_id, project_id), _Budget("project", budget_total_micro)
            )
            assert_capacity(
                tenant_budget.facts(),
                project_budget.facts(),
                estimated_micro=estimated_micro,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
                max_input_tokens=budget_max_input_tokens,
                max_output_tokens=budget_max_output_tokens,
            )
            tenant_budget.reserved_micro += estimated_micro
            project_budget.reserved_micro += estimated_micro
            reservation_id = f"res_{uuid.uuid4().hex[:12]}"
            self._reservations[reservation_id] = _Reservation(
                reservation_id=reservation_id,
                run_id=run_id,
                state=ReservationState.HELD,
                estimated_micro=estimated_micro,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
            )

            # ---- 用户消息 + 答案槽位。此后任何失败都要整体回滚：
            # ---- 内存事务的"回滚"就是手工恢复每一步的旧值（except 块）。
            undo_tenant = tenant_budget.reserved_micro - estimated_micro
            undo_project = project_budget.reserved_micro - estimated_micro
            try:
                user_message = self.products.append_message(
                    actor,
                    project_id,
                    conversation_id,
                    message_id=f"msg_{uuid.uuid4().hex[:12]}",
                    role=MessageRole.USER,
                    content=question,
                )
                answer_seq = self.products.reserve_message_seq(actor, project_id, conversation_id)
                now = self.clock.now()
                run = TeachingRun(
                    run_id=run_id,
                    tenant_id=actor.tenant_id,
                    project_id=project_id,
                    conversation_id=conversation_id,
                    user_message_id=user_message.message_id,
                    principal_id=actor.principal_id,
                    answer_message_id=None,
                    question=question,
                    status=RunStatus.QUEUED,
                    attempt_count=0,
                    model_id=model_id,
                    prompt_version=prompt_version,
                    ranking_version=ranking_version,
                    grounding=None,
                    error_code="",
                    error_detail="",
                    created_at=now,
                    updated_at=now,
                    max_input_tokens=budget_max_input_tokens,
                    max_output_tokens=budget_max_output_tokens,
                )
                self._runs[run_id] = run
                self._answer_seq[run_id] = answer_seq
                self._append_event(run_id, "run.created", {"question_chars": len(question)})
                return run
            except Exception:
                tenant_budget.reserved_micro = undo_tenant
                project_budget.reserved_micro = undo_project
                self._reservations.pop(reservation_id, None)
                self._runs.pop(run_id, None)
                self._answer_seq.pop(run_id, None)
                self._events.pop(run_id, None)
                self._event_seq.pop(run_id, None)
                raise

    def get_run(self, actor: Principal, project_id: str, run_id: str) -> TeachingRun:
        self.membership.get(actor, project_id)
        with self._lock:
            run = self._runs.get(run_id)
        if run is None or run.project_id != project_id or run.tenant_id != actor.tenant_id:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "资源不存在", run_id=run_id)
        return run

    def list_events(
        self, actor: Principal, project_id: str, run_id: str, *, after_seq: int = 0
    ) -> tuple[TeachingEvent, ...]:
        self.get_run(actor, project_id, run_id)  # 可见性同一出口
        with self._lock:
            events = tuple(self._events.get(run_id, ()))
        return tuple(event for event in events if event.seq > after_seq)

    def budget_snapshot(self, actor: Principal, project_id: str) -> dict:
        self.membership.get(actor, project_id)
        with self._lock:
            tenant = self._tenant_budgets.get(actor.tenant_id)
            project = self._project_budgets.get((actor.tenant_id, project_id))
        return {
            "tenant": tenant.facts().to_dict() if tenant else None,
            "project": project.facts().to_dict() if project else None,
        }

    def get_run_limits(self, claim: RunClaim) -> tuple[int, int]:
        with self._lock:
            run = self._run_for_claim(claim)
            return run.max_input_tokens, run.max_output_tokens

    def attempt_state(self, claim: RunClaim) -> str:
        with self._lock:
            self._run_for_claim(claim)
            attempt = self._attempt_for_run(claim.run.run_id)
            return attempt.status if attempt else "none"

    # ---------------------------------------------------------- worker 路径

    def claim_run(self, *, worker_id: str, lease_seconds: int) -> RunClaim | None:
        """跨租户认领：queued 优先，其次租约过期的 running（崩溃回收）。

        与摄取队列同样的纪律：认领是系统级操作，"哪个运行有活干"必须先于
        任何租户上下文被知道。锁内完成"选 + 占"，没有 check-then-act 窗口。
        """
        with self._lock:
            now = self.clock.now()
            candidates = [
                run
                for run in self._runs.values()
                if run.status is RunStatus.QUEUED or self._lease_expired(run, now)
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda run: (run.created_at, run.run_id))
            stale = candidates[0]
            claim_token = uuid.uuid4().hex
            claimed = replace(
                stale,
                status=RunStatus.RUNNING,
                attempt_count=stale.attempt_count + 1,
                updated_at=now,
            )
            self._runs[stale.run_id] = claimed
            self._claims[stale.run_id] = _LiveClaim(
                claim_token=claim_token,
                worker_id=worker_id,
                lease_until=now + timedelta(seconds=lease_seconds),
            )
            return RunClaim(run=claimed, claim_token=claim_token, worker_id=worker_id)

    def mark_dispatched(
        self,
        claim: RunClaim,
        *,
        attempt_id: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        request_payload: dict | None = None,
        routing_decision: RoutingDecision | None = None,
    ) -> None:
        with self._lock:
            current = self._require_live_claim(claim)
            reservation = self._reservation_for_run(claim.run.run_id)
            if reservation.state is not ReservationState.HELD:
                raise PlatformError(
                    ErrorCode.BUDGET_TREE_INVALID,
                    f"派发前的预留必须停在 held，当前 {reservation.state}（记账与派发已经脱节）",
                )
            self._resize_held_reservation(
                current,
                reservation,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
            )
            transition(reservation.state, ReservationState.IN_FLIGHT)
            reservation.state = ReservationState.IN_FLIGHT
            tenant_budget, project_budget = self._budgets_for_run(claim.run.run_id)
            tenant_budget.reserved_micro -= reservation.estimated_micro
            tenant_budget.in_flight_micro += reservation.estimated_micro
            project_budget.reserved_micro -= reservation.estimated_micro
            project_budget.in_flight_micro += reservation.estimated_micro
            self._attempts[attempt_id] = _Attempt(
                attempt_id=attempt_id,
                run_id=claim.run.run_id,
                status="dispatched",
                request_payload=dict(request_payload or {}),
            )
            if routing_decision is not None:
                self._runs[claim.run.run_id] = replace(current, routing_decision=routing_decision)
            self._touch(claim)
            self._append_event(
                claim.run.run_id,
                "run.dispatched",
                {
                    "attempt_id": attempt_id,
                    **(
                        {"routing_decision": routing_decision.to_dict()}
                        if routing_decision is not None
                        else {}
                    ),
                },
            )

    def record_result(
        self,
        claim: RunClaim,
        *,
        attempt_id: str,
        provider_request_id: str,
        payload: dict,
    ) -> None:
        with self._lock:
            self._require_live_claim(claim)
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.run_id != claim.run.run_id:
                raise deny(ErrorCode.CROSS_TENANT_DENIED, "attempt 不存在", attempt_id=attempt_id)
            if attempt.status != "dispatched":
                raise PlatformError(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    f"attempt 已是 {attempt.status}，不能重复记录结果",
                )
            attempt.status = "completed"
            attempt.provider_request_id = provider_request_id
            attempt.payload = payload
            self._touch(claim)

    def finish_run(
        self,
        claim: RunClaim,
        *,
        attempt_id: str,
        answer_message_id: str,
        answer_text: str,
        grounding: Grounding,
        usage: TokenUsage | None,
        citations: tuple[dict, ...] = (),
        citation_rejections: tuple[dict, ...] = (),
    ) -> TeachingRun:
        with self._lock:
            current = self._run_for_claim(claim)
            if current.status is RunStatus.SUCCEEDED:
                # 幂等重放：终态已落定（上次提交其实成功了），直接返回既有结果。
                return current
            self._require_live_claim(claim)
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.run_id != claim.run.run_id:
                raise deny(ErrorCode.CROSS_TENANT_DENIED, "attempt 不存在", attempt_id=attempt_id)
            # 唯一 assistant 消息（槽位在 start_run 已预留）。
            self.products.append_reserved_message(
                _principal_of(current),
                current.project_id,
                current.conversation_id,
                message_id=answer_message_id,
                seq=self._answer_seq[current.run_id],
                role=MessageRole.ASSISTANT,
                content=answer_text,
            )
            # 结算：有权威用量 → settled；没有 → 敞口保留（in_flight）。
            reservation = self._reservation_for_run(current.run_id)
            if usage is not None:
                actual_micro = usage_to_micro(usage.input_tokens, usage.output_tokens)
                transition(reservation.state, ReservationState.SETTLED)
                reservation.state = ReservationState.SETTLED
                reservation.actual_micro = actual_micro
                tenant_budget, project_budget = self._budgets_for_run(current.run_id)
                tenant_budget.in_flight_micro -= reservation.estimated_micro
                tenant_budget.spent_micro += actual_micro
                project_budget.in_flight_micro -= reservation.estimated_micro
                project_budget.spent_micro += actual_micro
                attempt.cost_micro = actual_micro
                attempt.input_tokens = usage.input_tokens
                attempt.output_tokens = usage.output_tokens
            run = replace(
                current,
                status=RunStatus.SUCCEEDED,
                answer_message_id=answer_message_id,
                grounding=grounding,
                citations=tuple(dict(item) for item in citations),
                citation_rejections=tuple(dict(item) for item in citation_rejections),
                updated_at=self.clock.now(),
            )
            self._runs[run.run_id] = run
            self._claims.pop(run.run_id, None)
            self._append_event(
                run.run_id,
                "run.succeeded",
                {
                    "answer_message_id": answer_message_id,
                    "grounding": str(grounding),
                    "usage_reported": usage is not None,
                },
            )
            return run

    def fail_run(
        self,
        claim: RunClaim,
        *,
        error_code: str,
        safe_detail: str,
        dispatch_happened: bool,
        usage: TokenUsage | None,
    ) -> TeachingRun:
        with self._lock:
            current = self._run_for_claim(claim)
            if current.status is RunStatus.FAILED:
                return current  # 幂等重放
            self._require_live_claim(claim)
            if dispatch_happened and usage is None:
                raise PlatformError(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    "已派发但拿不到权威用量：费用未知，必须走 reconciliation",
                )
            reservation = self._reservation_for_run(current.run_id)
            tenant_budget, project_budget = self._budgets_for_run(current.run_id)
            if dispatch_happened:
                # 有权威用量 → 按量结算（usage=None 已在上面拒绝）。
                assert usage is not None
                actual = usage_to_micro(usage.input_tokens, usage.output_tokens)
                transition(reservation.state, ReservationState.SETTLED)
                reservation.state = ReservationState.SETTLED
                reservation.actual_micro = actual
                tenant_budget.in_flight_micro -= reservation.estimated_micro
                tenant_budget.spent_micro += actual
                project_budget.in_flight_micro -= reservation.estimated_micro
                project_budget.spent_micro += actual
                attempt = self._attempt_for_run(current.run_id)
                if attempt is not None:
                    attempt.status = "failed"
                    attempt.payload = {
                        "kind": "failure",
                        "error_code": error_code,
                        "detail": safe_detail,
                    }
                    attempt.cost_micro = actual
                    attempt.input_tokens = usage.input_tokens
                    attempt.output_tokens = usage.output_tokens
            else:
                # 可证明未发生费用：预留原路释放。held 与 in_flight 的
                # 归还账户不同（in_flight 已从 reserved 划走），不能一概而论。
                was_in_flight = reservation.state is ReservationState.IN_FLIGHT
                transition(reservation.state, ReservationState.RELEASED)
                reservation.state = ReservationState.RELEASED
                if was_in_flight:
                    tenant_budget.in_flight_micro -= reservation.estimated_micro
                    project_budget.in_flight_micro -= reservation.estimated_micro
                else:
                    tenant_budget.reserved_micro -= reservation.estimated_micro
                    project_budget.reserved_micro -= reservation.estimated_micro
            run = replace(
                current,
                status=RunStatus.FAILED,
                error_code=error_code,
                error_detail=safe_detail,
                updated_at=self.clock.now(),
            )
            self._runs[run.run_id] = run
            self._claims.pop(run.run_id, None)
            self._append_event(
                run.run_id,
                "run.failed",
                {"error_code": error_code, "detail": safe_detail},
            )
            return run

    def require_reconciliation(self, claim: RunClaim, *, error_code: str, safe_detail: str) -> TeachingRun:
        with self._lock:
            current = self._run_for_claim(claim)
            if current.status is RunStatus.RECONCILIATION_REQUIRED:
                return current  # 幂等重放
            self._require_live_claim(claim)
            attempt = self._attempt_for_run(current.run_id)
            if attempt is not None and attempt.status == "dispatched":
                # 结果未知：provider 可能已经在算钱。attempt 转 unknown。
                attempt.status = "unknown"
            run = replace(
                current,
                status=RunStatus.RECONCILIATION_REQUIRED,
                error_code=error_code,
                error_detail=safe_detail,
                updated_at=self.clock.now(),
            )
            self._runs[run.run_id] = run
            self._claims.pop(run.run_id, None)
            self._append_event(
                run.run_id,
                "run.reconciliation_required",
                {"error_code": error_code, "detail": safe_detail},
            )
            return run

    def find_stored_result(self, claim: RunClaim) -> dict | None:
        with self._lock:
            self._require_live_claim(claim)
            attempt = self._attempt_for_run(claim.run.run_id)
        return attempt.payload if attempt else None

    def run_status_for_worker(self, run_id: str) -> tuple[RunStatus, str]:
        with self._lock:
            run = self._runs.get(run_id)
            claim = self._claims.get(run_id)
        if run is None:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "运行不存在", run_id=run_id)
        return run.status, claim.claim_token if claim else ""

    # ---------------------------------------------------------------- 内部

    def _append_event(self, run_id: str, event_type: str, payload: dict) -> None:
        seq = self._event_seq.get(run_id, 0) + 1
        self._event_seq[run_id] = seq
        self._events.setdefault(run_id, []).append(
            TeachingEvent(
                run_id=run_id,
                seq=seq,
                event_type=event_type,
                payload=payload,
                created_at=self.clock.now(),
            )
        )

    def _lease_expired(self, run: TeachingRun, now: datetime) -> bool:
        claim = self._claims.get(run.run_id)
        # 没有认领记录的 running 是孤儿（持有者状态丢了），视同过期可回收。
        return run.status is RunStatus.RUNNING and (claim is None or claim.lease_until <= now)

    def _run_for_claim(self, claim: RunClaim) -> TeachingRun:
        run = self._runs.get(claim.run.run_id)
        if run is None or run.tenant_id != claim.run.tenant_id or run.project_id != claim.run.project_id:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "运行不存在", run_id=claim.run.run_id)
        return run

    def _require_live_claim(self, claim: RunClaim) -> TeachingRun:
        """围栏：状态 running + token 匹配 + 租约未过期，三者缺一不可。

        （铁律 59：期限也是围栏的一部分 —— 只比 token 的话，
        "租约已过期但还没人接管"的窗口里旧持有者仍能落定。）
        """
        run = self._run_for_claim(claim)
        if run.status is not RunStatus.RUNNING:
            raise PlatformError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                f"这次认领已经失效（运行已是 {run.status}）",
            )
        live = self._claims.get(run.run_id)
        if live is None or live.claim_token != claim.claim_token:
            raise PlatformError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                "这次认领已经失效（租约过期后任务被重新认领）；不得用旧凭证改写当前持有者的运行",
            )
        if live.lease_until <= self.clock.now():
            raise PlatformError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                "这次认领的租约已过期（尚未被重新认领）；期限也是围栏的一部分",
            )
        return run

    def _touch(self, claim: RunClaim) -> None:
        run = self._runs[claim.run.run_id]
        self._runs[run.run_id] = replace(run, updated_at=self.clock.now())

    def _reservation_for_run(self, run_id: str) -> _Reservation:
        for reservation in self._reservations.values():
            if reservation.run_id == run_id:
                return reservation
        raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "运行没有预算预留（记账与运行脱节）")

    def _attempt_for_run(self, run_id: str) -> _Attempt | None:
        for attempt in self._attempts.values():
            if attempt.run_id == run_id:
                return attempt
        return None

    def _resize_held_reservation(
        self,
        run: TeachingRun,
        reservation: _Reservation,
        *,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
    ) -> None:
        new_micro = estimate_micro(estimated_input_tokens, estimated_output_tokens)
        old_micro = reservation.estimated_micro
        tenant, project = self._budgets_for_run(run.run_id)
        tenant_without_old = tenant.facts()
        project_without_old = project.facts()
        tenant_without_old = replace(
            tenant_without_old, reserved_micro=tenant_without_old.reserved_micro - old_micro
        )
        project_without_old = replace(
            project_without_old, reserved_micro=project_without_old.reserved_micro - old_micro
        )
        assert_capacity(
            tenant_without_old,
            project_without_old,
            estimated_micro=new_micro,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            max_input_tokens=run.max_input_tokens,
            max_output_tokens=run.max_output_tokens,
        )
        delta = new_micro - old_micro
        tenant.reserved_micro += delta
        project.reserved_micro += delta
        reservation.estimated_micro = new_micro
        reservation.estimated_input_tokens = estimated_input_tokens
        reservation.estimated_output_tokens = estimated_output_tokens

    def _budgets_for_run(self, run_id: str) -> tuple[_Budget, _Budget]:
        run = self._runs[run_id]
        return (
            self._tenant_budgets[run.tenant_id],
            self._project_budgets[(run.tenant_id, run.project_id)],
        )


def _principal_of(run: TeachingRun) -> Principal:
    """从运行行重建提问者的 Principal（落定消息时的归属判定主体）。

    答案消息属于提问者的会话：principal_id 来自运行行（start_run 记录），
    不是 worker 编的占位身份 —— 否则内存版的归属判定就成了"谁都能过"。
    """
    return Principal(principal_id=run.principal_id, tenant_id=run.tenant_id, display_name="教学提问者")
