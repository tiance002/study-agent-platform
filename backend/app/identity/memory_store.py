"""邀请与会话仓储的**内存实现**（开发适配器）。

与 PostgreSQL 实现（`db.identity_store`）实现**同一套端口**
（`identity.ports`），由同一份契约测试（`tests/test_identity_repositories.py`
按参数化夹具分别跑两个适配器）保证行为一致：
「内存版拦截、数据库版放行」这类漂移会在测试里直接暴露。

⚠️ 内存实现的持久化边界：进程重启即失。这正是它作为开发适配器的定位 ——
   但**语义**（单次消费、统一拒绝、撤销生效）必须与数据库版一致，
   否则 API 层在两种部署形态下的行为会分叉。

并发说明：兑换是 check-then-act（查存活 → 标记消费 → 建会话），
内存版用一把锁保证原子。这把锁只管单进程 —— 与审计 sink 的锁同一限制；
数据库版由 `exchange_invitation()` 的行锁保证，跨进程成立。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING

from app.audit.sink import RiskLevel
from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
from app.identity.limits import MAX_SESSION_TTL
from app.identity.models import Principal
from app.identity.ports import SystemContext
from app.product.models import Invitation, UserSession

if TYPE_CHECKING:  # 类型标注专用：避免运行时循环导入
    from app.audit.outbox import InMemoryAuditOutbox


@dataclass
class InMemoryInvitationRepository:
    """邀请的内存实现。按 `token_hash` 索引 —— 兑换只认哈希。

    兑换成功会把会话**登记进会话仓储**：与数据库版对齐 ——
    `exchange_invitation()` 在函数内完成"消费邀请 + 建会话"两条写入，
    内存版也必须让兑换出的会话立刻可被 `get_live` 查到，
    否则认证路径的回库检查会在兑换成功后的第一个请求上把用户拒之门外。
    """

    clock: Clock = field(default_factory=SystemClock)
    #: 会话仓储。兑换出的会话落在这里，认证路径才能回库查到。
    sessions: InMemorySessionRepository | None = None
    #: 审计 outbox。兑换成功的审计事实在**同一临界区**内登记
    #: （与数据库版的"同一事务"对齐）；投影由端点在业务成功后进行。
    outbox: "InMemoryAuditOutbox | None" = None
    #: 允许注入共享字典：重启恢复测试用它模拟"两个进程看同一份存储"。
    _by_hash: dict[str, Invitation] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def issue(
        self,
        context: SystemContext,
        *,
        invitation_id: str,
        token_hash: str,
        issued_by: str,
        invitee_principal_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> Invitation:
        with self._lock:
            if token_hash in self._by_hash:
                raise deny(
                    ErrorCode.PARAMS_INVALID,
                    "同一 token_hash 只能签发一次；重复签发会让旧邀请的消费状态失去意义",
                )
            invitation = Invitation(
                invitation_id=invitation_id,
                tenant_id=context.tenant_id,
                token_hash=token_hash,
                issued_by=issued_by,
                invitee_principal_id=invitee_principal_id,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            self._by_hash[token_hash] = invitation
            return invitation

    def exchange(
        self, token_hash: str, *, session_id: str, session_expires_at: datetime
    ) -> UserSession | None:
        now = self.clock.now()
        # TTL 硬上限在消费邀请**之前**判：这是服务端配置错误，不是邀请状态，
        # 不能把邀请消费掉再失败（否则留下"邀请没了、会话也没有"的悬空状态）。
        self._require_session_window(now, session_expires_at)
        with self._lock:
            invitation = self._by_hash.get(token_hash)
            # 未知 / 已过期 / 已消费在这里汇成同一个结果：None。
            # 数据库版的 `exchange_invitation()` 用同一语义 ——
            # `WHERE token_hash = %s AND consumed_at IS NULL AND expires_at > now()`。
            if invitation is None or not invitation.is_live(now):
                return None
            session = UserSession(
                session_id=session_id,
                tenant_id=invitation.tenant_id,
                principal_id=invitation.invitee_principal_id,
                issued_at=now,
                expires_at=session_expires_at,
            )
            # 「消费邀请 + 登记会话」必须在**同一临界区**内共同成功或共同不发生。
            # 此前标记消费在锁内、建会话在锁外，建会话一旦失败（例如重复 session_id）
            # 就会留下"邀请已消费但会话不存在"——兑换者被永久锁在门外。
            # PG 版由兑换函数内的单事务保证；内存版靠这把锁 + 失败回滚对齐语义。
            try:
                if self.sessions is not None:
                    # 锁顺序恒为 邀请锁 → 会话锁；会话仓储从不回调邀请仓储，无死锁。
                    self.sessions.create(session)
            except BaseException:
                # 会话没建成就把邀请原样还回去：本次兑换等于没发生。
                self._by_hash[token_hash] = invitation
                raise
            self._by_hash[token_hash] = replace(
                invitation,
                consumed_at=now,
                consumed_by=invitation.invitee_principal_id,
            )
            if self.outbox is not None:
                # 与 PG 版同一语义：审计事实与业务在同一临界区落定。
                self.outbox.stage(
                    "invitation_exchanged",
                    {"principal_id": session.principal_id},
                    risk=RiskLevel.HIGH,
                    tenant_id=invitation.tenant_id,
                )
            return session

    @staticmethod
    def _require_session_window(now: datetime, expires_at: datetime) -> None:
        """会话期限硬上限。与 0004 迁移的数据库守卫同一数值、同一拒绝方向。"""
        if expires_at <= now or expires_at > now + MAX_SESSION_TTL:
            raise deny(
                ErrorCode.INTERNAL_CONSISTENCY_ERROR,
                "会话有效期超出服务端允许范围",
            )


@dataclass
class InMemorySessionRepository:
    """会话的内存实现。撤销语义（时间戳、幂等返回值）与数据库版一致。"""

    clock: Clock = field(default_factory=SystemClock)
    _sessions: dict[str, UserSession] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create(self, session: UserSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def get_live(self, actor: Principal, session_id: str) -> UserSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return None
        # 归属校验与 RLS 同语义：别的主体的会话「不可见」，不是「可见但拒绝」。
        if (
            session.tenant_id != actor.tenant_id
            or session.principal_id != actor.principal_id
        ):
            return None
        return session if session.is_live(self.clock.now()) else None

    def revoke(self, actor: Principal, session_id: str, *, at: datetime) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if (
                session is None
                or session.tenant_id != actor.tenant_id
                or session.principal_id != actor.principal_id
            ):
                # 与 get_live 同一语义：看不见的会话撤不掉，也不暴露其存在。
                return False
            if session.revoked_at is not None:
                return False
            self._sessions[session_id] = replace(session, revoked_at=at)
            return True

    def revoke_all_for(
        self,
        actor: Principal,
        *,
        at: datetime,
        except_session_id: str | None = None,
    ) -> int:
        """集中失效：撤销该主体名下全部**存活**会话，返回撤销条数。"""
        count = 0
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if (
                    session.tenant_id != actor.tenant_id
                    or session.principal_id != actor.principal_id
                ):
                    continue
                if except_session_id is not None and session_id == except_session_id:
                    continue
                # 只数本次真正撤掉的存活会话：已撤销/已过期的不计数。
                if session.revoked_at is None and session.is_live(self.clock.now()):
                    self._sessions[session_id] = replace(session, revoked_at=at)
                    count += 1
        return count
