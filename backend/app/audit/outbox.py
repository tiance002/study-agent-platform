"""认证审计的可靠中转（transactional outbox）。

## 为什么需要它（审查 P1：审计失败时兑换已提交）

邀请兑换的事务顺序曾是：`exchange()` 提交（消费邀请 + 建会话）→ **之后**
才写高风险审计。审计 sink 不可用时接口返回 503，但邀请已经消费、
会话已经创建 —— 所谓 fail-closed 实际只是响应失败，业务动作并未拒绝，
重试也无法恢复（邀请已失效）。

修法（transactional outbox 模式）：

1. 兑换成功路径的审计事件由**仓储在业务同一事务内**写入 `auth_audit_outbox`
   （0006 迁移）—— 业务提交 = 审计事实落库，两者要么都发生要么都不发生；
2. 投影（把 outbox 事件追加进链式 sink）在事务提交**之后**进行；
   投影失败**不回滚业务** —— 事实已可靠留存，行保持 `projected_at IS NULL`
   可观测，待下次兑换请求或 sink 恢复后补投影。

> **名词：transactional outbox** —— 一句话定义：把"要发的消息"和"业务数据"
> 写进同一个数据库事务，再由后台异步投递。生活类比：挂号信不是邮递员
> 站在你家门口现写的 —— 你先把信投进邮筒（和办理业务同一个动作），
> 邮递员随后分拣投递；邮筒丢了信才算丢，投递晚点不算。
> 例子：兑换事务里同时写 `user_sessions` 和 `auth_audit_outbox` 两张表；
> 就算链式审计文件此刻写不进去，事件也已经在库里，绝不丢。

## 与链式 sink 的关系

链式 `AuditSink`（哈希链 JSONL）仍是审计的**展示与校验**载体；
本 outbox 是兑换路径审计事实的**可靠落点**。投影是 at-least-once：
极端情况下（append 成功后、标记 projected 前崩溃）同一事件可能被
投影两次，链上出现重复条目 —— 这是"丢事实"与"重复事实"之间
刻意选择的后者（审计宁重勿缺）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Protocol

from app.audit.sink import AuditSink, RiskLevel
from app.core.ids import new_id
from app.core.request_context import current_request_id


@dataclass(frozen=True)
class OutboxEvent:
    """一条待投影的审计事实。"""

    event_id: str
    event_type: str
    payload: dict
    risk: RiskLevel
    tenant_id: str | None
    project_id: str | None
    request_id: str | None


class AuditOutbox(Protocol):
    """审计 outbox 协议：内存 / PostgreSQL 实现可互换。"""

    def pending_count(self) -> int: ...

    def flush_pending(self, tenant_id: str | None = None) -> int: ...


class TransactionalAuditOutbox(AuditOutbox, Protocol):
    def stage_in_transaction(
        self,
        conn,
        *,
        event_type: str,
        payload: dict,
        risk: RiskLevel = RiskLevel.HIGH,
        tenant_id: str | None = None,
        project_id: str | None = None,
        request_id: str | None = None,
    ) -> str: ...


class InMemoryAuditOutbox:
    """内存实现（开发适配器）。

    `stage()` 在兑换的临界区内调用（与消费邀请、建会话同锁），
    语义对齐 PG 版的"同一事务"。
    """

    def __init__(self, sink: AuditSink) -> None:
        self._sink = sink
        self._pending: list[OutboxEvent] = []
        self._lock = threading.Lock()

    def stage(
        self,
        event_type: str,
        payload: dict,
        *,
        risk: RiskLevel = RiskLevel.HIGH,
        tenant_id: str | None = None,
        project_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """登记一条审计事实。在兑换业务临界区内调用，绝不失败。"""
        event = OutboxEvent(
            event_id=new_id("aud"),
            event_type=event_type,
            payload=payload,
            risk=risk,
            tenant_id=tenant_id,
            project_id=project_id,
            request_id=request_id or current_request_id(),
        )
        with self._lock:
            self._pending.append(event)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def flush_pending(self, tenant_id: str | None = None) -> int:
        """把待投影事件依次追加进链式 sink，返回成功投影条数。

        sink 不可用时**停止投影并保留剩余事件**（事实不丢，行仍可观测），
        不向上抛 —— 调用方（兑换端点）的业务已经成功，投影是尽力而为。
        """
        projected = 0
        with self._lock:
            while self._pending:
                event = self._pending[0]
                try:
                    self._sink.append(
                        event.event_type,
                        event.payload,
                        risk=event.risk,
                        tenant_id=event.tenant_id,
                        project_id=event.project_id,
                        request_id=event.request_id,
                    )
                except Exception:
                    # 首条失败即停：事件留在队首，顺序不乱，待下次补投影。
                    break
                self._pending.pop(0)
                projected += 1
        return projected


class PostgresAuditOutbox:
    """PostgreSQL 实现。

    `stage_in_transaction()` 在**调用方传入的连接/事务内**写行 ——
    这是原子性的全部根据：业务提交即事实落库。
    `flush_pending()` 在事务外逐条投影（`FOR UPDATE SKIP LOCKED`
    防多 worker 双投影），append 成功后才标记 `projected_at`。
    """

    # The file sink only has a process-local lock. Row-level SKIP LOCKED
    # alone would let two workers append different events concurrently and
    # fork the hash chain, so projection has one database-wide writer.
    _PROJECTOR_LOCK_ID = 0x5354554459415544

    def __init__(
        self,
        sink: AuditSink,
        dsn: str | None = None,
        *,
        connect_factory=None,
        json_factory=None,
    ) -> None:
        self._sink = sink
        self._dsn = dsn
        if connect_factory is None or json_factory is None:
            raise TypeError("PostgreSQL 适配器必须由 app.db.audit_store 装配")
        self._connect = connect_factory
        self._json = json_factory

    def stage_in_transaction(
        self,
        conn,
        *,
        event_type: str,
        payload: dict,
        risk: RiskLevel = RiskLevel.HIGH,
        tenant_id: str | None = None,
        project_id: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """在调用方的**未提交事务**内写入一条审计事实。

        连接由调用方持有：本方法绝不 commit/rollback ——
        事务边界属于业务（兑换），事件随它一起提交或一起消失。
        """
        event_id = new_id("aud")
        conn.execute(
            "INSERT INTO auth_audit_outbox"
            " (event_id, event_type, payload, risk, tenant_id, project_id, request_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                event_id,
                event_type,
                self._json(payload),
                str(risk),
                tenant_id,
                project_id,
                request_id or current_request_id(),
            ),
        )
        return event_id

    def pending_count(self) -> int:
        with self._connect(self._dsn) as conn:
            row = conn.execute("SELECT public.auth_audit_pending_count()").fetchone()
        return int(row[0]) if row else 0

    def flush_pending(self, tenant_id: str | None = None) -> int:
        if tenant_id is None:
            return 0
        projected = 0
        while True:
            with self._connect(self._dsn) as conn:
                conn.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    (tenant_id,),
                )
                # Hold the lock for read -> append -> mark. PostgreSQL releases
                # it automatically if this transaction or process crashes.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (self._PROJECTOR_LOCK_ID,),
                )
                row = conn.execute(
                    "SELECT event_id, event_type, payload, risk, tenant_id,"
                    "       project_id, request_id"
                    " FROM auth_audit_outbox"
                    " WHERE projected_at IS NULL"
                    " ORDER BY created_at, event_id"
                    " LIMIT 1 FOR UPDATE"
                ).fetchone()
                if row is None:
                    conn.commit()
                    return projected
                event_id, event_type, payload, risk, tenant_id, project_id, request_id = row
                try:
                    self._sink.refresh_chain_head()
                    self._sink.append(
                        event_type,
                        payload if isinstance(payload, dict) else json.loads(payload),
                        risk=RiskLevel(risk),
                        tenant_id=tenant_id,
                        project_id=project_id,
                        request_id=request_id,
                    )
                except Exception:
                    # sink 不可用：回滚，行保持 pending（事实不丢），停止本轮。
                    conn.rollback()
                    return projected
                conn.execute(
                    "UPDATE auth_audit_outbox SET projected_at = now()"
                    " WHERE event_id = %s",
                    (event_id,),
                )
                conn.commit()
                projected += 1
