"""产品仓储的内存实现（`product.ports` 的开发适配器）。

与 PostgreSQL 实现（`db.product_store`）跑同一套参数化契约测试
（`tests/test_product_repositories.py`），两侧语义必须一致：

- **访问判定先行**：每个方法的第一个动作都是 `membership.get(actor, project_id)`
  —— 未授予者看到的是"项目不存在"（统一拒绝），不是"存在但拒绝"。
- **seq 由实现分配**：`Message.append` 用会话计数器原子取号，
  PG 版用 `UPDATE ... RETURNING`，两者都不允许调用方提供 seq。
- **计划版本由实现分配**：`replace` 忽略入参 bundle 的 version，
  分配 `当前最大 + 1`；并发分配同一版本时按 VERSION_CONFLICT 拒绝
  （PG 由 UNIQUE (project_id, version) 兜底）。

⚠️ 内存版是开发适配器：进程重启即失。原子性靠一把锁；
   PG 版靠单事务 —— 两侧的**可观察行为**必须一致，内部手段可以不同。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
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
    SourceRecord,
    TaskStatus,
)
from app.product.transitions import assert_transition_legal


@dataclass
class InMemoryProductRepository:
    """会话、消息、计划、资料的内存实现。

    聚合在一个类里而不是四个：它们共享同一把锁与同一份访问判定，
    拆四个类只会让"同一项目上并发写消息与换计划"的互斥变得难以推理。
    """

    #: 依赖协议而非具体类：PG 装配传 PostgresMembershipRepository 也成立。
    membership: MembershipRepository
    #: 自持时钟 —— 不借 membership 的时钟（协议里没有它，借等于引入隐藏耦合）。
    clock: Clock = field(default_factory=SystemClock)
    _conversations: dict[str, Conversation] = field(default_factory=dict)
    _messages: dict[str, list[Message]] = field(default_factory=dict)
    _plans: dict[str, list[LearningPlan]] = field(default_factory=dict)
    _milestones: dict[str, list[Milestone]] = field(default_factory=dict)
    _tasks: dict[str, list[LearningTask]] = field(default_factory=dict)
    _sources: dict[str, SourceRecord] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # ------------------------------------------------------------------ 会话

    def create_conversation(
        self, actor: Principal, project_id: str, *, conversation_id: str, title: str
    ) -> Conversation:
        self.membership.get(actor, project_id)
        with self._lock:
            if conversation_id in self._conversations:
                raise deny(
                    ErrorCode.BUDGET_TREE_INVALID, f"会话已存在：{conversation_id}"
                )
            conversation = Conversation(
                conversation_id=conversation_id,
                tenant_id=actor.tenant_id,
                project_id=project_id,
                title=title,
                last_message_seq=0,
                created_at=self.clock.now(),
            )
            self._conversations[conversation_id] = conversation
            return conversation

    def list_conversations(
        self, actor: Principal, project_id: str
    ) -> tuple[Conversation, ...]:
        self.membership.get(actor, project_id)
        with self._lock:
            rows = [
                c
                for c in self._conversations.values()
                if c.project_id == project_id and c.tenant_id == actor.tenant_id
            ]
        return tuple(sorted(rows, key=lambda c: c.created_at))

    def get_conversation(
        self, actor: Principal, project_id: str, conversation_id: str
    ) -> Conversation:
        self.membership.get(actor, project_id)
        with self._lock:
            conversation = self._conversations.get(conversation_id)
        if (
            conversation is None
            or conversation.project_id != project_id
            or conversation.tenant_id != actor.tenant_id
        ):
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                conversation_id=conversation_id,
            )
        return conversation

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
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if (
                conversation is None
                or conversation.project_id != project_id
                or conversation.tenant_id != actor.tenant_id
            ):
                raise deny(
                    ErrorCode.CROSS_TENANT_DENIED,
                    "无权访问该项目",
                    conversation_id=conversation_id,
                )
            # 取号与写入同一临界区：并发追加不丢号、不重号。
            seq = conversation.last_message_seq + 1
            self._conversations[conversation_id] = replace(
                conversation, last_message_seq=seq
            )
            message = Message(
                message_id=message_id,
                tenant_id=conversation.tenant_id,
                project_id=project_id,
                conversation_id=conversation_id,
                seq=seq,
                role=role,
                content=content,
                created_at=self.clock.now(),
            )
            self._messages.setdefault(conversation_id, []).append(message)
            return message

    def list_messages(
        self, actor: Principal, project_id: str, conversation_id: str
    ) -> tuple[Message, ...]:
        self.get_conversation(actor, project_id, conversation_id)
        with self._lock:
            return tuple(self._messages.get(conversation_id, ()))

    # ------------------------------------------------------------------ 计划

    def current_plan(
        self, actor: Principal, project_id: str
    ) -> PlanBundle | None:
        self.membership.get(actor, project_id)
        with self._lock:
            versions = self._plans.get(project_id, [])
            if not versions:
                return None
            plan = max(versions, key=lambda p: p.version)
            return PlanBundle(
                plan=plan,
                milestones=tuple(self._milestones.get(plan.plan_id, ())),
                tasks=tuple(self._tasks.get(plan.plan_id, ())),
            )

    def replace_plan(
        self, actor: Principal, project_id: str, bundle: PlanBundle
    ) -> PlanBundle:
        """整版替换。**入参 bundle 的 version 被忽略**：版本由实现分配为
        当前最大 + 1 —— 客户端声明的版本号在这里没有意义，还容易被
        误当成乐观锁（真正的并发防护是 UNIQUE (project_id, version)）。
        """
        self.membership.get(actor, project_id)
        with self._lock:
            return self._replace_plan_locked(project_id, bundle)

    def _replace_plan_locked(self, project_id: str, bundle: PlanBundle) -> PlanBundle:
        """调用方必须持有 ``_lock``；供跨仓储原子命令复用。"""
        versions = self._plans.get(project_id, [])
        next_version = max((p.version for p in versions), default=0) + 1
        plan = replace(bundle.plan, version=next_version)
        if any(p.version == next_version for p in versions):
            raise deny(
                ErrorCode.VERSION_CONFLICT,
                "计划版本冲突；请重试（将基于最新状态重新分配版本）",
            )
        milestones = tuple(replace(m, plan_id=plan.plan_id) for m in bundle.milestones)
        tasks = bundle.tasks
        self._plans.setdefault(project_id, []).append(plan)
        self._milestones[plan.plan_id] = list(milestones)
        self._tasks[plan.plan_id] = list(tasks)
        return PlanBundle(plan=plan, milestones=milestones, tasks=tasks)

    def plan_history(
        self, actor: Principal, project_id: str
    ) -> tuple[LearningPlan, ...]:
        self.membership.get(actor, project_id)
        with self._lock:
            return tuple(sorted(self._plans.get(project_id, ()), key=lambda p: p.version))

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
        with self._lock:
            # 去重靠 identity_hash：同一项目内同一资料重复登记 → 返回既有记录。
            # 对调用方这是幂等成功，不是错误 —— 上传重试不该变成报错。
            for existing in self._sources.values():
                if (
                    existing.project_id == project_id
                    and existing.identity_hash == identity_hash
                ):
                    return existing
            record = SourceRecord(
                source_id=source_id,
                tenant_id=actor.tenant_id,
                project_id=project_id,
                display_name=display_name,
                media_type=media_type,
                identity_hash=identity_hash,
                registered_at=self.clock.now(),
                acquisition=acquisition,
            )
            self._sources[source_id] = record
            return record

    def list_sources(
        self, actor: Principal, project_id: str
    ) -> tuple[SourceRecord, ...]:
        self.membership.get(actor, project_id)
        with self._lock:
            rows = [
                s
                for s in self._sources.values()
                if s.project_id == project_id and s.tenant_id == actor.tenant_id
            ]
        return tuple(sorted(rows, key=lambda s: s.registered_at))

    def get_source(
        self, actor: Principal, project_id: str, source_id: str
    ) -> SourceRecord:
        self.membership.get(actor, project_id)
        with self._lock:
            record = self._sources.get(source_id)
        if (
            record is None
            or record.project_id != project_id
            or record.tenant_id != actor.tenant_id
        ):
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                source_id=source_id,
            )
        return record

    # ------------------------------------------------------------------ 任务

    def _find_task(self, task_id: str) -> tuple[str, int, LearningTask] | None:
        """在锁内定位任务（plan_id, 下标, 任务）。task_id 全局唯一。"""
        for plan_id, tasks in self._tasks.items():
            for index, task in enumerate(tasks):
                if task.task_id == task_id:
                    return plan_id, index, task
        return None

    def get_task(
        self, actor: Principal, project_id: str, task_id: str
    ) -> LearningTask:
        self.membership.get(actor, project_id)
        with self._lock:
            found = self._find_task(task_id)
        if (
            found is None
            or found[2].project_id != project_id
            or found[2].tenant_id != actor.tenant_id
        ):
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                task_id=task_id,
            )
        return found[2]

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
        with self._lock:
            found = self._find_task(task_id)
            if (
                found is None
                or found[2].project_id != project_id
                or found[2].tenant_id != actor.tenant_id
            ):
                raise deny(
                    ErrorCode.CROSS_TENANT_DENIED,
                    "无权访问该项目",
                    task_id=task_id,
                )
            plan_id, index, task = found
            if task.status == next_status:
                # 幂等重放：目标状态已达成（重试语义），返回现状不报错。
                return task
            if task.status != expected_status:
                raise deny(
                    ErrorCode.VERSION_CONFLICT,
                    "任务状态已被其他操作改变；请刷新后基于最新状态重试",
                    task_id=task_id,
                    current_status=task.status.value,
                )
            assert_transition_legal(task.status, next_status)
            updated = replace(task, status=next_status)
            self._tasks[plan_id][index] = updated
            return updated
