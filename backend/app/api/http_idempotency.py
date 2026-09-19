"""HTTP 命令幂等的**唯一**判定出口（0002 的 `http_idempotency` 表）。

## 语义（与 runtime 进程内幂等同一套原则，铁律 17/18 的落点）

- 键 = `(tenant_id, principal_id, command_scope, client_key)`：
  少主体 → 猜到 key 就能读到别人的响应；少命令 → 同一把钥匙在两个端点上
  互相串味。**`project_id` 不进唯一键**：创建项目的那个命令被占用时项目
  还不存在，进键就等于让"创建项目"永远无法幂等。
- 三态：`pending`（先到者持有）/ `completed`（结果已落定）/ `released`
  （占用者失败后放弃，等待者可接手）。
- **失败与拒绝不缓存**：失败释放占用 —— 缓存旧拒绝会让修好参数后的重试
  永远拿不到正确结果。
- 指纹绑定**请求语义的每个字段**（主体/项目/路径参数/请求体）：同键不同
  内容 → `IDEMPOTENCY_VIOLATION`，绝不"以先到者为准"。
- 重放返回缓存响应并标注 `X-Idempotent-Replay: true` —— 重放必须可观测。

## 与 runtime 幂等的关系

两层不合并（铁律 27）：这里是 HTTP 命令层的防重放；runtime 的是交互
执行层的业务幂等。本模块的存在不改变 runtime 的任何判定。

⚠️ 适配器说明：内存实现是开发适配器（进程内成立）；PostgreSQL 实现由
数据库唯一约束保证跨 worker 原子 —— claim 用的就是
`INSERT ... ON CONFLICT DO NOTHING` 的"插入成功即占用成功"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from psycopg import errors as pg_errors

from app.core.errors import ErrorCode, PlatformError, deny
from app.core.hashing import content_hash
from app.db.session import connect
from app.identity.models import Principal


@dataclass(frozen=True)
class ClaimOutcome:
    """一次占用尝试的结果。"""

    kind: Literal["claimed", "replay", "in_progress", "violation"]
    #: replay 时为缓存的 (status_code, body)；claimed/in_progress/violation 为 None。
    cached_status_code: int | None = None
    cached_body: dict | None = None


def command_fingerprint(
    *,
    tenant_id: str,
    principal_id: str,
    command_scope: str,
    path_params: dict,
    body: dict,
) -> str:
    """指纹绑定请求语义的**每个字段**。漏字段 = 同键换语义静默串味（铁律 9）。"""
    return content_hash(
        {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "command_scope": command_scope,
            "path_params": path_params,
            "body": body,
        }
    )


class HttpIdempotencyStore(Protocol):
    """幂等存储协议：内存 / PostgreSQL 实现可互换（契约测试互验）。"""

    def claim(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        fingerprint: str,
    ) -> ClaimOutcome: ...

    def complete(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        status_code: int,
        response_body: dict,
    ) -> None: ...

    def release(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
    ) -> None: ...


class InMemoryHttpIdempotencyStore:
    """内存实现（开发适配器）。结构镜像数据库行，语义与 PG 版一致。"""

    def __init__(self) -> None:
        # key -> {claim_id, fingerprint, state, status_code, response_body}
        self._rows: dict[tuple[str, str, str, str], dict] = {}

    def claim(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        fingerprint: str,
    ) -> ClaimOutcome:
        key = (tenant_id, principal_id, command_scope, client_key)
        row = self._rows.get(key)
        if row is None:
            self._rows[key] = {
                "claim_id": f"claim_{len(self._rows)}",
                "fingerprint": fingerprint,
                "state": "pending",
                "status_code": None,
                "response_body": None,
            }
            return ClaimOutcome(kind="claimed")
        if row["fingerprint"] != fingerprint:
            return ClaimOutcome(kind="violation")
        if row["state"] == "completed":
            return ClaimOutcome(
                kind="replay",
                cached_status_code=row["status_code"],
                cached_body=row["response_body"],
            )
        if row["state"] == "released":
            # 释放后的重试 = 重新执行：重置占用（同一行，状态回 pending）。
            row["state"] = "pending"
            return ClaimOutcome(kind="claimed")
        return ClaimOutcome(kind="in_progress")

    def complete(
        self, *, tenant_id: str, principal_id: str, command_scope: str,
        client_key: str, status_code: int, response_body: dict,
    ) -> None:
        key = (tenant_id, principal_id, command_scope, client_key)
        row = self._rows.get(key)
        if row is not None and row["state"] == "pending":
            row["state"] = "completed"
            row["status_code"] = status_code
            row["response_body"] = response_body

    def release(
        self, *, tenant_id: str, principal_id: str, command_scope: str, client_key: str
    ) -> None:
        key = (tenant_id, principal_id, command_scope, client_key)
        row = self._rows.get(key)
        if row is not None and row["state"] == "pending":
            row["state"] = "released"


class PostgresHttpIdempotencyStore:
    """PostgreSQL 实现。占用的原子性由 UNIQUE (tenant_id, principal_id,
    command_scope, client_key) 保证 —— INSERT 成功即占用成功，
    冲突即别人持有，不需要任何进程内锁。"""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn

    @staticmethod
    def _set_rls_context(conn, tenant_id: str, principal_id: str) -> None:
        """在每个方法开头设置 RLS 上下文（铁律 33：有主体列就必须约束主体维度）。

        用会话级 `is_local=false`：连接随 with 块关闭，不会跨请求残留；
        `is_local=true` 要求显式事务块，而本类用 commit/rollback 手工管理，
        两套事务控制混用只会引入新错误。
        """
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
        conn.execute("SELECT set_config('app.principal_id', %s, false)", (principal_id,))

    def claim(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        fingerprint: str,
    ) -> ClaimOutcome:
        with connect(self._dsn) as conn:
            self._set_rls_context(conn, tenant_id, principal_id)
            inserted = conn.execute(
                "INSERT INTO http_idempotency"
                " (claim_id, tenant_id, principal_id, command_scope, client_key,"
                "  request_hash, project_id, state)"
                " VALUES (%s, %s, %s, %s, %s, %s, NULL, 'pending')"
                " ON CONFLICT (tenant_id, principal_id, command_scope, client_key)"
                " DO NOTHING"
                " RETURNING claim_id",
                (f"claim_{content_hash(client_key)[:16]}", tenant_id, principal_id,
                 command_scope, client_key, fingerprint),
            ).fetchone()
            if inserted is not None:
                conn.commit()
                return ClaimOutcome(kind="claimed")

            row = conn.execute(
                "SELECT request_hash, state, status_code, response_body"
                " FROM http_idempotency"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s",
                (tenant_id, principal_id, command_scope, client_key),
            ).fetchone()
            assert row is not None, "唯一键冲突却查不到行 —— 并发窗口内行被删，需复查"
            request_hash, state_name, status_code, response_body = row
            if request_hash != fingerprint:
                conn.rollback()
                return ClaimOutcome(kind="violation")
            if state_name == "completed":
                conn.commit()
                return ClaimOutcome(
                    kind="replay",
                    cached_status_code=status_code,
                    cached_body=response_body,
                )
            if state_name == "released":
                # 释放后的重试 = 重新执行：CAS 到 pending（仍以唯一键防并发抢占）。
                updated = conn.execute(
                    "UPDATE http_idempotency SET state = 'pending'"
                    " WHERE tenant_id = %s AND principal_id = %s"
                    "   AND command_scope = %s AND client_key = %s"
                    "   AND state = 'released'"
                    " RETURNING claim_id",
                    (tenant_id, principal_id, command_scope, client_key),
                ).fetchone()
                conn.commit()
                if updated is not None:
                    return ClaimOutcome(kind="claimed")
                return ClaimOutcome(kind="in_progress")
            conn.rollback()
            return ClaimOutcome(kind="in_progress")

    def complete(
        self, *, tenant_id: str, principal_id: str, command_scope: str,
        client_key: str, status_code: int, response_body: dict,
    ) -> None:
        import json as _json

        with connect(self._dsn) as conn:
            self._set_rls_context(conn, tenant_id, principal_id)
            conn.execute(
                "UPDATE http_idempotency"
                " SET state = 'completed', status_code = %s, response_body = %s,"
                "     completed_at = now()"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s AND state = 'pending'",
                (
                    status_code,
                    _json.dumps(response_body, ensure_ascii=False, sort_keys=True),
                    tenant_id, principal_id, command_scope, client_key,
                ),
            )
            conn.commit()

    def release(
        self, *, tenant_id: str, principal_id: str, command_scope: str, client_key: str
    ) -> None:
        with connect(self._dsn) as conn:
            self._set_rls_context(conn, tenant_id, principal_id)
            conn.execute(
                "UPDATE http_idempotency SET state = 'released'"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s AND state = 'pending'",
                (tenant_id, principal_id, command_scope, client_key),
            )
            conn.commit()


def raise_violation() -> None:
    """同键不同内容的统一拒绝（供守卫层调用，保持错误构造单点）。"""
    raise deny(
        ErrorCode.IDEMPOTENCY_VIOLATION,
        "同一 idempotency_key 被用于内容不同的请求；"
        "幂等键必须唯一标识一次请求，不得复用于不同参数",
    )


def unexpected_pg_error(exc: pg_errors.Error) -> Exception:
    """PG 适配器的意外错误出口（保持堆栈与类型，不吞异常）。"""
    return exc

# ---------------------------------------------------------------- 守卫层


@dataclass
class WriteGuard:
    """一次幂等写守卫的句柄。`replay=True` 时端点必须直接返回缓存响应。"""

    replay: bool
    #: 认证出的身份。守卫是端点取身份的唯一入口 —— 接入幂等的端点
    # 不再自己调 authenticate_request（避免重复认证与两个出口）。
    principal: Principal
    cached_status_code: int | None = None
    cached_body: dict | None = None
    _store: "HttpIdempotencyStore | None" = None
    _keys: dict | None = None

    _done: bool = False

    def complete(self, status_code: int, response_body: dict) -> None:
        if self._store is None:
            # 直通模式（请求没带 Idempotency-Key）：没有存储可写，完成即 no-op。
            return
        assert self._keys is not None, "claim 成功的守卫必然携带键组"
        self._store.complete(
            **self._keys, status_code=status_code, response_body=response_body
        )
        self._done = True


from contextlib import contextmanager  # noqa: E402

from fastapi import Request  # noqa: E402


@contextmanager
def idempotent_write(request: Request, body_model=None):
    """写端点的幂等守卫。

    - 无 ``Idempotency-Key`` 头 → 直通（yield None），端点照常执行；
    - 有头 → claim；``guard.replay`` 为真时端点**必须**返回缓存响应；
    - 端点执行异常 → release（失败不缓存）；正常返回但没调 ``complete``
      也 release（没有可缓存的确定结果，等于本次没做成）。
    """
    state = request.app.state.platform
    store = state.http_idempotency
    key = request.headers.get("Idempotency-Key")

    # 身份必须来自统一认证入口；这里不重复实现认证规则。
    from app.api.auth_routes import authenticate_request

    principal = authenticate_request(request)

    if key is None or store is None:
        # 无幂等要求：直通 guard（complete 是 no-op），端点代码不用分叉。
        yield WriteGuard(replay=False, principal=principal, _store=None, _keys={}, _done=True)
        return
    scope = request.scope.get("route")
    command_scope = (
        f"{request.method} {scope.path}" if scope is not None
        else f"{request.method} {request.url.path}"
    )
    body_dict = body_model.model_dump() if body_model is not None else {}
    fingerprint = command_fingerprint(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        command_scope=command_scope,
        path_params=dict(request.path_params),
        body=body_dict,
    )
    keys = {
        "tenant_id": principal.tenant_id,
        "principal_id": principal.principal_id,
        "command_scope": command_scope,
        "client_key": key,
    }
    outcome = store.claim(**keys, fingerprint=fingerprint)
    if outcome.kind == "violation":
        raise_violation()
    if outcome.kind == "in_progress":
        raise PlatformError(
            code=ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            message="同一幂等键的请求正在处理中；请稍后重试",
            retryable=True,
        )

    guard = WriteGuard(
        replay=outcome.kind == "replay",
        principal=principal,
        cached_status_code=outcome.cached_status_code,
        cached_body=outcome.cached_body,
        _store=store,
        _keys=keys,
    )
    try:
        yield guard
    except BaseException:
        store.release(**keys)
        raise
    if not guard._done:
        # 正常返回但端点没标记 complete：没有可缓存的确定结果。
        store.release(**keys)
