"""会话仓储的**内存实现**（开发适配器）。

与 PostgreSQL 实现（`db.identity_store`）实现**同一套端口**
（`identity.ports`），由同一份契约测试（`tests/test_identity_repositories.py`
按参数化夹具分别跑两个适配器）保证行为一致：
「内存版拦截、数据库版放行」这类漂移会在测试里直接暴露。

⚠️ 内存实现的持久化边界：进程重启即失。这正是它作为开发适配器的定位 ——
   但**语义**（撤销立即生效、归属隔离、幂等返回值）必须与数据库版一致，
   否则 API 层在两种部署形态下的行为会分叉。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime

from app.core.clock import Clock, SystemClock
from app.identity.models import Principal, UserSession


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
