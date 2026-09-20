"""产品仓储的 PostgreSQL 适配器（`product.ports` 的数据库实现）。

三条纪律与 `db.identity_store` 一致：参数绑定、事务边界即方法边界、
数据库错误翻译成平台错误。

隔离结构：每个方法先过 `membership.get`（统一 404 语义，不依赖 RLS 的
存在与否），再开 `tenant_transaction`（项目级 RLS 需要项目上下文）——
**应用层判定 + 数据库兜底**两层都在，缺一层就不是纵深防御。

关键语义：
- **seq 取号**：`UPDATE conversations SET last_message_seq = last_message_seq + 1
  RETURNING` 与消息 INSERT 同一事务 —— 并发追加不丢号不重号，
  失败则取号一并回滚（不留空洞）。
- **计划版本**：`UNIQUE (project_id, version)` 兜底并发分配；
  UniqueViolation 翻译成 VERSION_CONFLICT（可重试：客户端重新 PUT 即可，
  实现会基于最新状态重新分配版本）。
- **资料去重**：`UNIQUE (project_id, identity_hash)`，冲突时查回既有记录
  —— 上传重试是幂等成功，不是报错。
"""

from __future__ import annotations

from psycopg import errors as pg_errors

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
from app.db.session import tenant_transaction
from app.identity.models import Principal
from app.identity.ports import MembershipRepository
from app.product.models import (
    Conversation,
    LearningPlan,
    LearningTask,
    Message,
    MessageRole,
    Milestone,
    PlanBundle,
    PlanStatus,
    SourceRecord,
    TaskStatus,
)
from app.product.transitions import assert_transition_legal

_CONVERSATION_COLUMNS = (
    "conversation_id, tenant_id, project_id, title, last_message_seq, created_at"
)
_MESSAGE_COLUMNS = (
    "message_id, tenant_id, project_id, conversation_id, seq, role, content, created_at"
)
_PLAN_COLUMNS = "plan_id, tenant_id, project_id, version, goal, status, created_at"
_SOURCE_COLUMNS = (
    "source_id, tenant_id, project_id, display_name, media_type,"
    " identity_hash, registered_at, acquisition"
)


def _conversation_from_row(row: tuple) -> Conversation:
    return Conversation(
        conversation_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        title=row[3],
        last_message_seq=row[4],
        created_at=row[5],
    )


def _message_from_row(row: tuple) -> Message:
    return Message(
        message_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        conversation_id=row[3],
        seq=row[4],
        role=MessageRole(row[5]),
        content=row[6],
        created_at=row[7],
    )


def _plan_from_row(row: tuple) -> LearningPlan:
    return LearningPlan(
        plan_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        version=row[3],
        goal=row[4],
        status=PlanStatus(row[5]),
        created_at=row[6],
    )


def _source_from_row(row: tuple) -> SourceRecord:
    return SourceRecord(
        source_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        display_name=row[3],
        media_type=row[4],
        identity_hash=row[5],
        registered_at=row[6],
        acquisition=row[7],
    )


class PostgresProductRepository:
    """会话、消息、计划、资料的 PostgreSQL 实现。"""

    def __init__(
        self,
        membership: MembershipRepository,
        clock: Clock | None = None,
        dsn: str | None = None,
    ) -> None:
        # 公共属性名：Protocol 结构匹配要求字段名一致（内存版同名为 membership）。
        self.membership = membership
        self._clock = clock or SystemClock()
        self._dsn = dsn

    # ------------------------------------------------------------------ 会话

    def create_conversation(
        self, actor: Principal, project_id: str, *, conversation_id: str, title: str
    ) -> Conversation:
        self.membership.get(actor, project_id)
        try:
            with tenant_transaction(
                tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
            ) as conn:
                row = conn.execute(
                    "INSERT INTO conversations (conversation_id, tenant_id,"
                    " project_id, title)"
                    " VALUES (%s, %s, %s, %s)"
                    " RETURNING " + _CONVERSATION_COLUMNS,
                    (conversation_id, actor.tenant_id, project_id, title),
                ).fetchone()
        except pg_errors.UniqueViolation as exc:
            raise deny(
                ErrorCode.BUDGET_TREE_INVALID, f"会话已存在：{conversation_id}"
            ) from exc
        assert row is not None
        return _conversation_from_row(row)

    def list_conversations(
        self, actor: Principal, project_id: str
    ) -> tuple[Conversation, ...]:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "SELECT " + _CONVERSATION_COLUMNS
                + " FROM conversations WHERE project_id = %s ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return tuple(_conversation_from_row(row) for row in rows)

    def get_conversation(
        self, actor: Principal, project_id: str, conversation_id: str
    ) -> Conversation:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _CONVERSATION_COLUMNS
                + " FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        if row is None:
            # RLS 已按租户 + 项目过滤：剩下的只有"不存在"一种解释。
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                conversation_id=conversation_id,
            )
        return _conversation_from_row(row)

    # ------------------------------------------------------------------ 消息

    def append_message(
        self,
        actor: Principal,
        project_id: str,
        conversation_id: str,
        *,
        message_id: str,
        role: MessageRole,
        content: str,
    ) -> Message:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            # 取号：原子自增 + RETURNING。与 INSERT 同事务，
            # INSERT 失败则取号一并回滚（seq 不留空洞）。
            seq_row = conn.execute(
                "UPDATE conversations SET last_message_seq = last_message_seq + 1"
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
            message_row = conn.execute(
                "INSERT INTO messages (message_id, tenant_id, project_id,"
                " conversation_id, seq, role, content)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " RETURNING " + _MESSAGE_COLUMNS,
                (
                    message_id,
                    actor.tenant_id,
                    project_id,
                    conversation_id,
                    seq_row[0],
                    str(role),
                    content,
                ),
            ).fetchone()
        assert message_row is not None
        return _message_from_row(message_row)

    def list_messages(
        self, actor: Principal, project_id: str, conversation_id: str
    ) -> tuple[Message, ...]:
        self.get_conversation(actor, project_id, conversation_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "SELECT " + _MESSAGE_COLUMNS
                + " FROM messages WHERE conversation_id = %s ORDER BY seq",
                (conversation_id,),
            ).fetchall()
        return tuple(_message_from_row(row) for row in rows)

    # ------------------------------------------------------------------ 计划

    def current_plan(self, actor: Principal, project_id: str) -> PlanBundle | None:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            plan_row = conn.execute(
                "SELECT " + _PLAN_COLUMNS
                + " FROM learning_plans WHERE project_id = %s"
                " ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if plan_row is None:
                return None
            plan = _plan_from_row(plan_row)
            milestones = conn.execute(
                "SELECT milestone_id, tenant_id, project_id, plan_id, order_index,"
                " title, description FROM milestones WHERE plan_id = %s"
                " ORDER BY order_index",
                (plan.plan_id,),
            ).fetchall()
            tasks = conn.execute(
                "SELECT t.task_id, t.tenant_id, t.project_id, t.milestone_id,"
                " t.order_index, t.title, t.status"
                " FROM learning_tasks t"
                " JOIN milestones m ON m.milestone_id = t.milestone_id"
                " WHERE m.plan_id = %s ORDER BY t.order_index",
                (plan.plan_id,),
            ).fetchall()
        return PlanBundle(
            plan=plan,
            milestones=tuple(
                Milestone(
                    milestone_id=r[0], tenant_id=r[1], project_id=r[2], plan_id=r[3],
                    order_index=r[4], title=r[5], description=r[6],
                )
                for r in milestones
            ),
            tasks=tuple(
                LearningTask(
                    task_id=r[0], tenant_id=r[1], project_id=r[2], milestone_id=r[3],
                    order_index=r[4], title=r[5], status=TaskStatus(r[6]),
                )
                for r in tasks
            ),
        )

    def replace_plan(
        self, actor: Principal, project_id: str, bundle: PlanBundle
    ) -> PlanBundle:
        """整版替换：plans + milestones + tasks 全部 INSERT 在**同一事务** ——
        中途失败整版回滚，库里永远是完整的某一版（这是 PlanBundle 存在的理由）。
        """
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            return self._replace_plan_in_transaction(conn, actor, project_id, bundle)

    def _replace_plan_in_transaction(
        self, conn, actor: Principal, project_id: str, bundle: PlanBundle
    ) -> PlanBundle:
        """在调用方已有事务内写整版计划，供学习闭环冻结映射时复用。"""
        version_row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM learning_plans"
                " WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        assert version_row is not None, "聚合查询必须返回一行（COALESCE 保证非 NULL）"
        next_version = int(version_row[0]) + 1
        plan = replace_plan_version(bundle.plan, next_version)
        milestones = tuple(
            replace_milestone_plan_id(m, plan.plan_id) for m in bundle.milestones
        )
        tasks = bundle.tasks
        try:
            conn.execute(
                    "INSERT INTO learning_plans (plan_id, tenant_id, project_id,"
                    " version, goal, status)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        plan.plan_id,
                        actor.tenant_id,
                        project_id,
                        plan.version,
                        plan.goal,
                        str(plan.status),
                    ),
                )
            for milestone in milestones:
                conn.execute(
                        "INSERT INTO milestones (milestone_id, tenant_id, project_id,"
                        " plan_id, order_index, title, description)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            milestone.milestone_id,
                            actor.tenant_id,
                            project_id,
                            milestone.plan_id,
                            milestone.order_index,
                            milestone.title,
                            milestone.description,
                        ),
                    )
            for task in tasks:
                conn.execute(
                        "INSERT INTO learning_tasks (task_id, tenant_id, project_id,"
                        " milestone_id, order_index, title, status)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            task.task_id,
                            actor.tenant_id,
                            project_id,
                            task.milestone_id,
                            task.order_index,
                            task.title,
                            str(task.status),
                        ),
                    )
        except pg_errors.UniqueViolation as exc:
            raise deny(
                ErrorCode.VERSION_CONFLICT,
                "计划版本冲突；请重新提交（将基于最新状态重新分配版本）",
            ) from exc
        return PlanBundle(plan=plan, milestones=milestones, tasks=tasks)

    def plan_history(
        self, actor: Principal, project_id: str
    ) -> tuple[LearningPlan, ...]:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "SELECT " + _PLAN_COLUMNS
                + " FROM learning_plans WHERE project_id = %s ORDER BY version",
                (project_id,),
            ).fetchall()
        return tuple(_plan_from_row(row) for row in rows)

    # ------------------------------------------------------------------ 资料

    def register_source(
        self,
        actor: Principal,
        project_id: str,
        *,
        source_id: str,
        display_name: str,
        media_type: str,
        identity_hash: str,
        acquisition: dict,
    ) -> SourceRecord:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "INSERT INTO sources (source_id, tenant_id, project_id,"
                " display_name, media_type, identity_hash, acquisition)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (project_id, identity_hash) DO NOTHING"
                " RETURNING " + _SOURCE_COLUMNS,
                (
                    source_id,
                    actor.tenant_id,
                    project_id,
                    display_name,
                    media_type,
                    identity_hash,
                    _jsonb(acquisition),
                ),
            ).fetchone()
            if row is None:
                # 去重命中：返回**既有**记录 —— 上传重试是幂等成功。
                row = conn.execute(
                    "SELECT " + _SOURCE_COLUMNS
                    + " FROM sources WHERE project_id = %s AND identity_hash = %s",
                    (project_id, identity_hash),
                ).fetchone()
        assert row is not None
        return _source_from_row(row)

    def list_sources(
        self, actor: Principal, project_id: str
    ) -> tuple[SourceRecord, ...]:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            rows = conn.execute(
                "SELECT " + _SOURCE_COLUMNS
                + " FROM sources WHERE project_id = %s ORDER BY registered_at",
                (project_id,),
            ).fetchall()
        return tuple(_source_from_row(row) for row in rows)

    def get_source(
        self, actor: Principal, project_id: str, source_id: str
    ) -> SourceRecord:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _SOURCE_COLUMNS
                + " FROM sources WHERE source_id = %s",
                (source_id,),
            ).fetchone()
        if row is None:
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                source_id=source_id,
            )
        return _source_from_row(row)


    # ------------------------------------------------------------------ 任务

    def get_task(
        self, actor: Principal, project_id: str, task_id: str
    ) -> LearningTask:
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            row = conn.execute(
                "SELECT " + _TASK_COLUMNS
                + " FROM learning_tasks WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        if row is None:
            # RLS 已按租户 + 项目过滤：剩下的只有"不存在"一种解释。
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                task_id=task_id,
            )
        return _task_from_row(row)

    def transition_task(
        self,
        actor: Principal,
        project_id: str,
        task_id: str,
        *,
        expected_status: TaskStatus,
        next_status: TaskStatus,
    ) -> LearningTask:
        # 静态合法性先行：非法迁移不看存储直接拒绝（判定出口唯一）。
        assert_transition_legal(expected_status, next_status)
        self.membership.get(actor, project_id)
        with tenant_transaction(
            tenant_id=actor.tenant_id, project_id=project_id, dsn=self._dsn
        ) as conn:
            # 条件更新即并发控制：命中行 = 状态确实是 expected 才写入。
            row = conn.execute(
                "UPDATE learning_tasks SET status = %s"
                " WHERE task_id = %s AND status = %s"
                " RETURNING " + _TASK_COLUMNS,
                (next_status.value, task_id, expected_status.value),
            ).fetchone()
            if row is None:
                # 零行有两种原因，必须区分（与项目 UPDATE 同一套判别法）：
                # 不可见 → 404；可见但状态已变 → 冲突或幂等重放。
                current = conn.execute(
                    "SELECT status FROM learning_tasks WHERE task_id = %s",
                    (task_id,),
                ).fetchone()
                if current is None:
                    raise deny(
                        ErrorCode.CROSS_TENANT_DENIED,
                        "无权访问该项目",
                        task_id=task_id,
                    )
                if current[0] == next_status.value:
                    # 幂等重放：目标状态已达成（重试语义），返回现状。
                    row = conn.execute(
                        "SELECT " + _TASK_COLUMNS
                        + " FROM learning_tasks WHERE task_id = %s",
                        (task_id,),
                    ).fetchone()
                else:
                    raise deny(
                        ErrorCode.VERSION_CONFLICT,
                        "任务状态已被其他操作改变；请刷新后基于最新状态重试",
                        task_id=task_id,
                        current_status=current[0],
                    )
        assert row is not None
        return _task_from_row(row)


def _jsonb(value: dict) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


_TASK_COLUMNS = (
    "task_id, tenant_id, project_id, milestone_id, order_index, title, status"
)


def _task_from_row(row: tuple) -> LearningTask:
    return LearningTask(
        task_id=row[0],
        tenant_id=row[1],
        project_id=row[2],
        milestone_id=row[3],
        order_index=row[4],
        title=row[5],
        status=TaskStatus(row[6]),
    )


def replace_plan_version(plan: LearningPlan, version: int) -> LearningPlan:
    from dataclasses import replace

    return replace(plan, version=version)


def replace_milestone_plan_id(milestone: Milestone, plan_id: str) -> Milestone:
    from dataclasses import replace

    return replace(milestone, plan_id=plan_id)

