"""教学运行仓储的 PostgreSQL 适配器（`teaching.ports` 的持久化实现）。

四条纪律与 `db/ingestion_store.py` 一致：参数绑定、事务边界即方法边界、
数据库错误翻译成平台错误、**每条语句之前先设 RLS 上下文**。

## 两个事务形状

| 方法 | 角色 | 上下文 | 为什么 |
|---|---|---|---|
| `start_run` / `get_run` / `list_events` / `budget_snapshot` | 应用 | 租户 + 项目 | 用户路径 |
| `claim_run` | **worker** | `app.worker_id`（无租户） | 先知道"哪个运行有活干"才可能有租户上下文 |
| `mark_dispatched` / `record_result` / `finish_run` / `fail_run` / `require_reconciliation` / `find_stored_result` | **worker** | 租户 + 项目（取自 run 行） | 认领范围内的读写 |

`claim_run` 的跨租户可见性由 0010 的 `teaching_runs_worker` 策略
（`TO study_worker`）授予 —— 普通应用角色自设 `app.worker_id` 看不到任何运行
（有专门的反例测试锁住这一点）。

## 派发与记账的关系

`mark_dispatched` 在**调用 provider 之前**执行：写 attempt 行（UNIQUE
约束保证一次运行至多一次派发）+ 预算 held → in_flight。此后网络调用
**不持有**任何事务或行锁 —— worker 在网络调用期间占着行锁会把其他
运行全部堵死，而事务超时回滚又会让"已派发"的存证消失。

## final commit 为什么是一条事务

`finish_run` 同事务写：唯一 assistant 消息（槽位预分配）+ 预算结算 +
attempt 收尾 + 终态事件 + run → succeeded。消息表的 UNIQUE
`(conversation_id, seq)` 保证重放不会写出第二条答案。
commit 失败时 result_payload 已在 attempt 行里（`record_result` 落的存证），
重放从存证落库 —— **绝不重新调用模型**。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

from psycopg import errors as pg_errors

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, PlatformError, deny
from app.db import budget_store
from app.db.session import full_transaction, tenant_transaction, worker_transaction
from app.identity.models import Principal
from app.identity.ports import MembershipRepository
from app.teaching.models import TokenUsage
from app.teaching.routing import RoutingDecision
from app.teaching.runs import Grounding, RunClaim, RunStatus, TeachingEvent, TeachingRun

_RUN_COLUMNS = (
    "run_id, tenant_id, project_id, conversation_id, user_message_id,"
    " principal_id, answer_message_id, answer_seq, question, status,"
    " attempt_count, model_id, prompt_version, ranking_version, grounding,"
    " error_code, error_detail, created_at, updated_at, routing_decision"
)

_LEASE_EXPIRED_SQL = "(status = 'running' AND lease_until IS NOT NULL AND lease_until < now())"


def _run_from_row(row: tuple) -> TeachingRun:
    grounding = Grounding(row[14]) if row[14] else None
    return TeachingRun(
        run_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        conversation_id=row[3],
        user_message_id=row[4],
        principal_id=row[5],
        answer_message_id=row[6],
        question=row[8],
        status=RunStatus(row[9]),
        attempt_count=row[10],
        model_id=row[11],
        prompt_version=row[12],
        ranking_version=row[13],
        grounding=grounding,
        error_code=row[15],
        error_detail=row[16],
        created_at=row[17],
        updated_at=row[18],
        routing_decision=(RoutingDecision.from_dict(row[19]) if row[19] is not None else None),
    )


class PostgresTeachingRepository:
    """教学运行的 PostgreSQL 实现。"""

    def __init__(
        self,
        *,
        membership: MembershipRepository,
        dsn: str | None = None,
        worker_dsn: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.membership = membership
        self._dsn = dsn
        self._worker_dsn = worker_dsn
        self._clock = clock or SystemClock()

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
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        reservation_id = f"res_{uuid.uuid4().hex[:12]}"
        user_message_id = f"msg_{uuid.uuid4().hex[:12]}"
        try:
            with full_transaction(
                tenant_id=actor.tenant_id,
                project_id=project_id,
                principal_id=actor.principal_id,
                dsn=self._dsn,
            ) as conn:
                # 会话可见性：UPDATE 命中 0 行 = 会话不存在或不属于本项目
                # （RLS 过滤），统一拒绝。
                seq_row = conn.execute(
                    "UPDATE conversations SET last_message_seq = last_message_seq + 2"
                    " WHERE conversation_id = %s"
                    " RETURNING last_message_seq",
                    (conversation_id,),
                ).fetchone()
                if seq_row is None:
                    raise deny(
                        ErrorCode.CROSS_TENANT_DENIED,
                        "无权访问该项目",
                        conversation_id=conversation_id,
                    )
                answer_seq = int(seq_row[0])
                user_seq = answer_seq - 1

                # 写入顺序由组合外键决定：消息 → 运行 → 预留（预留的组合外键
                # 指向 teaching_runs，必须后插）。预算容量不足时抛
                # BUDGET_EXCEEDED，整个事务回滚 —— 取号、消息、运行一并消失，
                # 原子性不依赖写入顺序。
                conn.execute(
                    "INSERT INTO messages (message_id, tenant_id, project_id,"
                    " conversation_id, seq, role, content)"
                    " VALUES (%s, %s, %s, %s, %s, 'user', %s)",
                    (
                        user_message_id,
                        actor.tenant_id,
                        project_id,
                        conversation_id,
                        user_seq,
                        question,
                    ),
                )
                conn.execute(
                    "INSERT INTO teaching_runs (run_id, tenant_id, project_id,"
                    " conversation_id, user_message_id, principal_id, answer_seq,"
                    " question, status, model_id, prompt_version, ranking_version)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s)",
                    (
                        run_id,
                        actor.tenant_id,
                        project_id,
                        conversation_id,
                        user_message_id,
                        actor.principal_id,
                        answer_seq,
                        question,
                        model_id,
                        prompt_version,
                        ranking_version,
                    ),
                )
                budget_store.reserve_in_conn(
                    conn,
                    tenant_id=actor.tenant_id,
                    project_id=project_id,
                    reservation_id=reservation_id,
                    run_id=run_id,
                    estimated_input_tokens=estimated_input_tokens,
                    estimated_output_tokens=estimated_output_tokens,
                    total_micro=budget_total_micro,
                    max_input_tokens=budget_max_input_tokens,
                    max_output_tokens=budget_max_output_tokens,
                )
                self._insert_event(conn, run_id, "run.created", {"question_chars": len(question)})
                row = conn.execute(
                    "SELECT " + _RUN_COLUMNS + " FROM teaching_runs WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
        except pg_errors.UniqueViolation as exc:
            # 唯一键冲突（同一条问题重复建 run）：翻译成稳定错误。
            raise PlatformError(
                ErrorCode.IDEMPOTENCY_VIOLATION,
                "该问题已有对应的运行（user_message_id 唯一）",
            ) from exc
        assert row is not None
        return _run_from_row(row)

    def get_run(self, actor: Principal, project_id: str, run_id: str) -> TeachingRun:
        self.membership.get(actor, project_id)
        with full_transaction(
            tenant_id=actor.tenant_id,
            project_id=project_id,
            principal_id=actor.principal_id,
            dsn=self._dsn,
        ) as conn:
            row = conn.execute(
                "SELECT " + _RUN_COLUMNS + ","
                " COALESCE((SELECT result_payload->'validated_citations'"
                " FROM provider_attempts pa WHERE pa.run_id = teaching_runs.run_id), '[]'::jsonb),"
                " COALESCE((SELECT result_payload->'citation_rejections'"
                " FROM provider_attempts pa WHERE pa.run_id = teaching_runs.run_id), '[]'::jsonb)"
                " FROM teaching_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        if row is None:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "资源不存在", run_id=run_id)
        return replace_run_metadata(_run_from_row(row[:20]), row[20], row[21])

    def list_events(
        self, actor: Principal, project_id: str, run_id: str, *, after_seq: int = 0
    ) -> tuple[TeachingEvent, ...]:
        self.get_run(actor, project_id, run_id)  # 可见性同一出口
        with full_transaction(
            tenant_id=actor.tenant_id,
            project_id=project_id,
            principal_id=actor.principal_id,
            dsn=self._dsn,
        ) as conn:
            rows = conn.execute(
                "SELECT run_id, seq, event_type, payload, created_at"
                " FROM teaching_events WHERE run_id = %s AND seq > %s ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return tuple(
            TeachingEvent(
                run_id=row[0],
                seq=row[1],
                event_type=row[2],
                payload=dict(row[3]),
                created_at=row[4],
            )
            for row in rows
        )

    def budget_snapshot(self, actor: Principal, project_id: str) -> dict:
        self.membership.get(actor, project_id)
        with tenant_transaction(tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn) as conn:
            return budget_store.snapshot_in_conn(conn, tenant_id=actor.tenant_id, project_id=project_id)

    def get_run_limits(self, claim: RunClaim) -> tuple[int, int]:
        with worker_transaction(
            tenant_id=claim.run.tenant_id,
            project_id=claim.run.project_id,
            principal_id=claim.run.principal_id,
            dsn=self._worker_dsn,
        ) as conn:
            row = conn.execute(
                "SELECT max_input_tokens, max_output_tokens FROM teaching_budgets"
                " WHERE tenant_id = %s AND project_id = %s",
                (claim.run.tenant_id, claim.run.project_id),
            ).fetchone()
        if row is None:
            raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "运行没有预算上限快照")
        return int(row[0]), int(row[1])

    def attempt_state(self, claim: RunClaim) -> str:
        with worker_transaction(
            tenant_id=claim.run.tenant_id,
            project_id=claim.run.project_id,
            principal_id=claim.run.principal_id,
            dsn=self._worker_dsn,
        ) as conn:
            if self._require_live_claim_row(conn, claim.run.run_id, claim.claim_token) is None:
                raise self._stale_claim_error(conn, claim)
            row = conn.execute(
                "SELECT status FROM provider_attempts WHERE run_id = %s", (claim.run.run_id,)
            ).fetchone()
        return str(row[0]) if row else "none"

    # ---------------------------------------------------------- worker 路径

    def claim_run(self, *, worker_id: str, lease_seconds: int) -> RunClaim | None:
        """跨租户认领（worker 角色）：queued 优先，其次租约过期的 running。"""
        with worker_transaction(dsn=self._worker_dsn) as conn:
            conn.execute("SELECT set_config('app.worker_id', %s, true)", (worker_id,))
            selected = conn.execute(
                "SELECT " + _RUN_COLUMNS + " FROM teaching_runs"
                f" WHERE status = 'queued' OR {_LEASE_EXPIRED_SQL}"
                " ORDER BY created_at, run_id"
                " LIMIT 1 FOR UPDATE SKIP LOCKED"
            ).fetchone()
            if selected is None:
                return None
            row = conn.execute(
                "UPDATE teaching_runs"
                " SET status = 'running', attempt_count = attempt_count + 1,"
                "     claim_token = gen_random_uuid(), lease_owner = %s,"
                "     lease_until = now() + make_interval(secs => %s),"
                "     updated_at = now()"
                " WHERE run_id = %s"
                " RETURNING " + _RUN_COLUMNS + ", claim_token",
                (worker_id, float(lease_seconds), selected[0]),
            ).fetchone()
        assert row is not None, "刚被本事务锁住并选中的行不可能在 UPDATE 时消失"
        run = _run_from_row(row[:20])
        return RunClaim(
            run=run,
            # RETURNING 拿到的就是本次生成的 token（同一事务内读回）。
            claim_token=str(row[20]),
            worker_id=worker_id,
        )

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
        with worker_transaction(
            tenant_id=claim.run.tenant_id,
            project_id=claim.run.project_id,
            principal_id=claim.run.principal_id,
            dsn=self._worker_dsn,
        ) as conn:
            current = self._require_live_claim_row(conn, claim.run.run_id, claim.claim_token)
            if current is None:
                # 已不在 running（终态/被接管）—— 幂等由 attempt 是否存在决定。
                if self._attempt_exists(conn, attempt_id, claim.run.run_id):
                    return
                raise self._stale_claim_error(conn, claim)
            # 幂等：attempt 已存在说明上次派发事务其实已提交。
            if self._attempt_exists(conn, attempt_id, claim.run.run_id):
                return
            limits = conn.execute(
                "SELECT max_input_tokens, max_output_tokens FROM teaching_budgets"
                " WHERE tenant_id = %s AND project_id = %s",
                (claim.run.tenant_id, claim.run.project_id),
            ).fetchone()
            if limits is None:
                raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "运行没有预算上限快照")
            budget_store.resize_held_in_conn(
                conn,
                run_id=claim.run.run_id,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
                max_input_tokens=int(limits[0]),
                max_output_tokens=int(limits[1]),
            )
            budget_store.hold_to_in_flight_in_conn(conn, run_id=claim.run.run_id)
            if routing_decision is not None:
                conn.execute(
                    "UPDATE teaching_runs SET routing_decision = %s::jsonb WHERE run_id = %s",
                    (
                        json.dumps(routing_decision.to_dict(), ensure_ascii=False),
                        claim.run.run_id,
                    ),
                )
            conn.execute(
                "INSERT INTO provider_attempts (attempt_id, tenant_id, project_id,"
                " run_id, status, request_payload) VALUES (%s, %s, %s, %s, 'dispatched', %s::jsonb)",
                (
                    attempt_id,
                    claim.run.tenant_id,
                    claim.run.project_id,
                    claim.run.run_id,
                    json.dumps(request_payload or {}, ensure_ascii=False),
                ),
            )
            self._insert_event(
                conn,
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
        with worker_transaction(
            tenant_id=claim.run.tenant_id,
            project_id=claim.run.project_id,
            principal_id=claim.run.principal_id,
            dsn=self._worker_dsn,
        ) as conn:
            if self._require_live_claim_row(conn, claim.run.run_id, claim.claim_token) is None:
                raise self._stale_claim_error(conn, claim)
            updated = conn.execute(
                "UPDATE provider_attempts SET status = 'completed',"
                " provider_request_id = %s, result_payload = %s::jsonb, updated_at = now()"
                " WHERE attempt_id = %s AND status = 'dispatched'",
                (provider_request_id, json.dumps(payload, ensure_ascii=False), attempt_id),
            ).rowcount
            if updated != 1:
                raise PlatformError(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    "attempt 不在 dispatched 状态，不能重复记录结果",
                )

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
        with worker_transaction(
            tenant_id=claim.run.tenant_id,
            project_id=claim.run.project_id,
            principal_id=claim.run.principal_id,
            dsn=self._worker_dsn,
        ) as conn:
            row = conn.execute(
                "SELECT status FROM teaching_runs WHERE run_id = %s FOR UPDATE",
                (claim.run.run_id,),
            ).fetchone()
            if row is None:
                raise deny(ErrorCode.CROSS_TENANT_DENIED, "运行不存在", run_id=claim.run.run_id)
            if row[0] == str(RunStatus.SUCCEEDED):
                # 幂等重放：上次提交其实成功了。返回既有状态，不写第二条消息。
                fresh = conn.execute(
                    "SELECT " + _RUN_COLUMNS + " FROM teaching_runs WHERE run_id = %s",
                    (claim.run.run_id,),
                ).fetchone()
                assert fresh is not None
                return _run_from_row(fresh)
            if self._require_live_claim_row(conn, claim.run.run_id, claim.claim_token) is None:
                raise self._stale_claim_error(conn, claim)
            # 答案消息：seq 在 start_run 已预留，UNIQUE (conversation_id, seq)
            # 保证重放不会写出第二条。
            seq_row = conn.execute(
                "SELECT answer_seq FROM teaching_runs WHERE run_id = %s",
                (claim.run.run_id,),
            ).fetchone()
            assert seq_row is not None and seq_row[0] is not None, "start_run 必须已为答案预留槽位"
            conn.execute(
                "INSERT INTO messages (message_id, tenant_id, project_id,"
                " conversation_id, seq, role, content)"
                " VALUES (%s, %s, %s, %s, %s, 'assistant', %s)",
                (
                    answer_message_id,
                    claim.run.tenant_id,
                    claim.run.project_id,
                    claim.run.conversation_id,
                    int(seq_row[0]),
                    answer_text,
                ),
            )
            # 结算：有权威用量 → settled；没有 → 预留停在 in_flight（敞口可见）。
            budget_store.settle_in_conn(
                conn,
                run_id=claim.run.run_id,
                actual_micro=(None if usage is None else budget_store_usage_micro(usage)),
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
            )
            conn.execute(
                "UPDATE provider_attempts SET"
                " input_tokens = %s, output_tokens = %s, cost_micro = %s,"
                " updated_at = now()"
                " WHERE attempt_id = %s",
                (
                    usage.input_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    None if usage is None else budget_store_usage_micro(usage),
                    attempt_id,
                ),
            )
            conn.execute(
                "UPDATE provider_attempts SET result_payload ="
                " jsonb_set(jsonb_set(result_payload, '{validated_citations}', %s::jsonb),"
                " '{citation_rejections}', %s::jsonb), updated_at = now()"
                " WHERE attempt_id = %s",
                (
                    json.dumps(list(citations), ensure_ascii=False),
                    json.dumps(list(citation_rejections), ensure_ascii=False),
                    attempt_id,
                ),
            )
            updated = conn.execute(
                "UPDATE teaching_runs SET status = 'succeeded',"
                " answer_message_id = %s, grounding = %s,"
                " lease_owner = NULL, lease_until = NULL, claim_token = NULL,"
                " updated_at = now()"
                " WHERE run_id = %s AND status = 'running'"
                " AND claim_token = %s AND lease_until > now()"
                " RETURNING " + _RUN_COLUMNS,
                (answer_message_id, str(grounding), claim.run.run_id, claim.claim_token),
            ).fetchone()
            if updated is None:
                raise self._stale_claim_error(conn, claim)
            self._insert_event(
                conn,
                claim.run.run_id,
                "run.succeeded",
                {
                    "answer_message_id": answer_message_id,
                    "grounding": str(grounding),
                    "usage_reported": usage is not None,
                },
            )
            return _run_from_row(updated)

    def fail_run(
        self,
        claim: RunClaim,
        *,
        error_code: str,
        safe_detail: str,
        dispatch_happened: bool,
        usage: TokenUsage | None,
    ) -> TeachingRun:
        if dispatch_happened and usage is None:
            raise PlatformError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                "已派发但拿不到权威用量：费用未知，必须走 reconciliation",
            )
        with worker_transaction(
            tenant_id=claim.run.tenant_id,
            project_id=claim.run.project_id,
            principal_id=claim.run.principal_id,
            dsn=self._worker_dsn,
        ) as conn:
            row = conn.execute(
                "SELECT status FROM teaching_runs WHERE run_id = %s FOR UPDATE",
                (claim.run.run_id,),
            ).fetchone()
            if row is None:
                raise deny(ErrorCode.CROSS_TENANT_DENIED, "运行不存在", run_id=claim.run.run_id)
            if row[0] == str(RunStatus.FAILED):
                fresh = conn.execute(
                    "SELECT " + _RUN_COLUMNS + " FROM teaching_runs WHERE run_id = %s",
                    (claim.run.run_id,),
                ).fetchone()
                assert fresh is not None
                return _run_from_row(fresh)
            if self._require_live_claim_row(conn, claim.run.run_id, claim.claim_token) is None:
                raise self._stale_claim_error(conn, claim)
            if dispatch_happened:
                assert usage is not None
                budget_store.settle_in_conn(
                    conn,
                    run_id=claim.run.run_id,
                    actual_micro=budget_store_usage_micro(usage),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
                conn.execute(
                    "UPDATE provider_attempts SET status = 'failed',"
                    " input_tokens = %s, output_tokens = %s, cost_micro = %s,"
                    " result_payload = %s::jsonb,"
                    " updated_at = now()"
                    " WHERE attempt_id = %s",
                    (
                        usage.input_tokens,
                        usage.output_tokens,
                        budget_store_usage_micro(usage),
                        json.dumps(
                            {
                                "kind": "failure",
                                "error_code": error_code,
                                "detail": safe_detail,
                            },
                            ensure_ascii=False,
                        ),
                        self._attempt_id_for_run(conn, claim.run.run_id),
                    ),
                )
            else:
                budget_store.release_in_conn(conn, run_id=claim.run.run_id)
            updated = conn.execute(
                "UPDATE teaching_runs SET status = 'failed',"
                " error_code = %s, error_detail = %s,"
                " lease_owner = NULL, lease_until = NULL, claim_token = NULL,"
                " updated_at = now()"
                " WHERE run_id = %s AND status = 'running'"
                " AND claim_token = %s AND lease_until > now()"
                " RETURNING " + _RUN_COLUMNS,
                (error_code, safe_detail, claim.run.run_id, claim.claim_token),
            ).fetchone()
            if updated is None:
                raise self._stale_claim_error(conn, claim)
            self._insert_event(
                conn,
                claim.run.run_id,
                "run.failed",
                {"error_code": error_code, "detail": safe_detail},
            )
            return _run_from_row(updated)

    def require_reconciliation(self, claim: RunClaim, *, error_code: str, safe_detail: str) -> TeachingRun:
        with worker_transaction(
            tenant_id=claim.run.tenant_id,
            project_id=claim.run.project_id,
            principal_id=claim.run.principal_id,
            dsn=self._worker_dsn,
        ) as conn:
            row = conn.execute(
                "SELECT status FROM teaching_runs WHERE run_id = %s FOR UPDATE",
                (claim.run.run_id,),
            ).fetchone()
            if row is None:
                raise deny(ErrorCode.CROSS_TENANT_DENIED, "运行不存在", run_id=claim.run.run_id)
            if row[0] == str(RunStatus.RECONCILIATION_REQUIRED):
                fresh = conn.execute(
                    "SELECT " + _RUN_COLUMNS + " FROM teaching_runs WHERE run_id = %s",
                    (claim.run.run_id,),
                ).fetchone()
                assert fresh is not None
                return _run_from_row(fresh)
            if self._require_live_claim_row(conn, claim.run.run_id, claim.claim_token) is None:
                raise self._stale_claim_error(conn, claim)
            conn.execute(
                "UPDATE provider_attempts SET status = 'unknown', updated_at = now()"
                " WHERE attempt_id = %s AND status = 'dispatched'",
                (self._attempt_id_for_run(conn, claim.run.run_id),),
            )
            updated = conn.execute(
                "UPDATE teaching_runs SET status = 'reconciliation_required',"
                " error_code = %s, error_detail = %s,"
                " lease_owner = NULL, lease_until = NULL, claim_token = NULL,"
                " updated_at = now()"
                " WHERE run_id = %s AND status = 'running'"
                " AND claim_token = %s AND lease_until > now()"
                " RETURNING " + _RUN_COLUMNS,
                (error_code, safe_detail, claim.run.run_id, claim.claim_token),
            ).fetchone()
            if updated is None:
                raise self._stale_claim_error(conn, claim)
            self._insert_event(
                conn,
                claim.run.run_id,
                "run.reconciliation_required",
                {"error_code": error_code, "detail": safe_detail},
            )
            return _run_from_row(updated)

    def find_stored_result(self, claim: RunClaim) -> dict | None:
        with worker_transaction(
            tenant_id=claim.run.tenant_id,
            project_id=claim.run.project_id,
            principal_id=claim.run.principal_id,
            dsn=self._worker_dsn,
        ) as conn:
            if self._require_live_claim_row(conn, claim.run.run_id, claim.claim_token) is None:
                raise self._stale_claim_error(conn, claim)
            row = conn.execute(
                "SELECT result_payload FROM provider_attempts"
                " WHERE run_id = %s AND result_payload IS NOT NULL",
                (claim.run.run_id,),
            ).fetchone()
        return dict(row[0]) if row and row[0] else None

    def run_status_for_worker(self, run_id: str) -> tuple[RunStatus, str]:
        with worker_transaction(dsn=self._worker_dsn) as conn:
            conn.execute("SELECT set_config('app.worker_id', 'status-read', true)")
            row = conn.execute(
                "SELECT status, claim_token FROM teaching_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        if row is None:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "运行不存在", run_id=run_id)
        return RunStatus(row[0]), str(row[1]) if row[1] else ""

    # ---------------------------------------------------------------- 内部

    def _require_live_claim_row(self, conn, run_id: str, claim_token: str) -> tuple | None:
        """围栏：状态 running + token 匹配 + 租约未过期（期限也是围栏）。

        返回 run 行（仍处于合法认领中）或 None（已离开 running）。
        条件更新在同一事务里比对 —— 失去租约的旧持有者改不了新持有者的运行。
        （worker 事务已带租户 + 项目上下文，标准策略即可命中行；
        这里不需要也不设置任何"worker 变量"—— 那不是凭据。）
        """
        fresh = conn.execute(
            "SELECT status FROM teaching_runs WHERE run_id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if fresh is None:
            raise deny(ErrorCode.CROSS_TENANT_DENIED, "运行不存在", run_id=run_id)
        if fresh[0] != str(RunStatus.RUNNING):
            return None
        valid = conn.execute(
            "SELECT 1 FROM teaching_runs WHERE run_id = %s AND status = 'running'"
            " AND claim_token = %s AND lease_until > now()",
            (run_id, uuid.UUID(claim_token)),
        ).fetchone()
        if valid is None:
            raise PlatformError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                "这次认领已经失效（租约过期后任务被重新认领）；不得用旧凭证改写当前持有者的运行",
            )
        return fresh

    def _stale_claim_error(self, conn, claim: RunClaim) -> PlatformError:
        row = conn.execute(
            "SELECT status FROM teaching_runs WHERE run_id = %s", (claim.run.run_id,)
        ).fetchone()
        status = row[0] if row else "missing"
        return PlatformError(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            f"这次认领已经失效（运行当前状态 {status}）",
        )

    def _attempt_exists(self, conn, attempt_id: str, run_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM provider_attempts WHERE attempt_id = %s AND run_id = %s",
            (attempt_id, run_id),
        ).fetchone()
        return row is not None

    def _attempt_id_for_run(self, conn, run_id: str) -> str:
        row = conn.execute("SELECT attempt_id FROM provider_attempts WHERE run_id = %s", (run_id,)).fetchone()
        if row is None:
            raise PlatformError(ErrorCode.BUDGET_TREE_INVALID, "运行没有 attempt 行（记账与派发脱节）")
        return str(row[0])

    def _insert_event(self, conn, run_id: str, event_type: str, payload: dict) -> None:
        """事件写入。seq 分配依赖**运行行锁**串行化：
        每个写事件的调用方都在同一事务里持有该 run 的行锁。"""
        conn.execute(
            "INSERT INTO teaching_events (run_id, seq, tenant_id, project_id,"
            " event_type, payload)"
            " SELECT %s,"
            "       COALESCE((SELECT MAX(seq) FROM teaching_events"
            "                 WHERE run_id = %s), 0) + 1,"
            "       tenant_id, project_id, %s, %s::jsonb"
            " FROM teaching_runs WHERE run_id = %s",
            (
                run_id,
                run_id,
                event_type,
                json.dumps(payload, ensure_ascii=False),
                run_id,
            ),
        )


def budget_store_usage_micro(usage: TokenUsage) -> int:
    """权威用量 → 微单位（`budget.ports.usage_to_micro` 的转接，单一出口）。"""
    from app.budget.ports import usage_to_micro

    return usage_to_micro(usage.input_tokens, usage.output_tokens)


def replace_run_metadata(run: TeachingRun, citations: object, rejections: object) -> TeachingRun:
    """把 attempt 中的已校验证据投影到用户可见运行，原始 payload 不外泄。"""
    raw_citations = citations if isinstance(citations, list) else []
    raw_rejections = rejections if isinstance(rejections, list) else []
    return replace(
        run,
        citations=tuple(dict(item) for item in raw_citations if isinstance(item, dict)),
        citation_rejections=tuple(dict(item) for item in raw_rejections if isinstance(item, dict)),
    )
