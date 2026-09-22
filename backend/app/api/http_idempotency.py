"""HTTP 命令幂等的**唯一**判定出口（0002 的 `http_idempotency` 表）。

## 语义（与 runtime 进程内幂等同一套原则，铁律 17/18 的落点）

- 键 = `(tenant_id, principal_id, command_scope, client_key)`：
  少主体 → 猜到 key 就能读到别人的响应；少命令 → 同一把钥匙在两个端点上
  互相串味。**`project_id` 不进唯一键**：创建项目的那个命令被占用时项目
  还不存在，进键就等于让"创建项目"永远无法幂等。
- **key 必填**（0007 规格冻结的数据边界）：变更接口缺少 `Idempotency-Key`
  一律拒绝（`IDEMPOTENCY_KEY_REQUIRED`，400）。早先"缺头直通"是一个
  可靠性缺口 —— 客户端超时重试仍会重复创建项目/消息/计划，
  而且守卫形同虚设只需"不发头"。
- 四态：`pending`（先到者持有）/ `completed`（结果已落定）/ `released`
  （业务明确失败，可重新执行）/ `indeterminate`（租约超时，必须先对账）。
- **租约不等于接管许可**：`pending` 超时只能证明持有者失联，不能证明业务
  没有提交。超时后原子转为 `indeterminate` 并返回
  `RECONCILIATION_REQUIRED`；在没有 durable receipt 前绝不自动重执行。
- **失败与拒绝不缓存**：失败释放占用 —— 缓存旧拒绝会让修好参数后的重试
  永远拿不到正确结果。
- 指纹绑定**请求语义的每个字段**（主体/项目/路径参数/请求体）：同键不同
  内容 → `IDEMPOTENCY_VIOLATION`，绝不"以先到者为准"。
- **缓存响应有界**：超过 `MAX_CACHED_RESPONSE_BYTES` 的响应不缓存原文，
  落一个显式的"过大不可重放"标记体 —— 无界 JSONB 是客户端可控的
  数据库放大入口（0007 规格冻结的边界）。
- 重放返回缓存响应并标注 `X-Idempotent-Replay: true` —— 重放必须可观测。

## 与 runtime 幂等的关系

两层不合并（铁律 27）：这里是 HTTP 命令层的防重放；runtime 的是交互
执行层的业务幂等。本模块的存在不改变 runtime 的任何判定。

⚠️ 适配器说明：内存实现是开发适配器（进程内成立）；PostgreSQL 实现由
数据库唯一约束保证跨 worker 原子 —— claim 用的就是
`INSERT ... ON CONFLICT DO NOTHING` 的"插入成功即占用成功"。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from app.core.errors import ErrorCode, PlatformError, deny
from app.core.hashing import content_hash
from app.core.ids import new_id
from app.identity.models import Principal

#: client_key 的长度上限（0007 规格冻结）。超限直接拒绝：
#: 这个值会长期保留在幂等记录里，不设界就是客户端可控的存储放大入口。
IDEMPOTENCY_KEY_MAX_CHARS = 200

#: pending 占用的租约期（秒）。持有者超过该时长未 complete/release，
#: 状态进入 indeterminate 并要求对账。取值太小会把仍在运行的慢请求误标为
#: 不确定，太大则崩溃后暴露得太慢；命令端点的 P99 远小于 120 秒。
IDEMPOTENCY_LEASE_SECONDS = 120

#: 缓存响应体的字节上限。超过即落"过大不可重放"标记，缓存体积有界。
MAX_CACHED_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class ClaimOutcome:
    """一次占用尝试的结果。"""

    kind: Literal[
        "claimed", "replay", "in_progress", "violation", "reconciliation_required"
    ]
    #: replay 时为缓存的 (status_code, body)；claimed/in_progress/violation 为 None。
    cached_status_code: int | None = None
    cached_body: dict | None = None
    #: 本次占用的所有者令牌。complete/release 携带它：
    #: 状态进入 indeterminate 后，旧持有者的 complete 不再改写结果。
    owner_token: str | None = None


def _bounded_body(response_body: dict) -> dict:
    """缓存体有界化。超限落显式标记而不是原文 —— 无界 JSONB 不可接受。

    重放时返回标记体而非原文：客户端凭 `X-Idempotent-Replay` 与标记字段
    可明确知道"这次命令没有重复执行，但响应需通过查询接口重新获取"。
    这比两个更坏的替代方案好：缓存原文（无界放大）或 release
    （重试会重复执行已成功的副作用）。
    """
    encoded = json.dumps(response_body, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode("utf-8")) <= MAX_CACHED_RESPONSE_BYTES:
        return response_body
    return {
        "idempotency_cache": "response_too_large",
        "limit_bytes": MAX_CACHED_RESPONSE_BYTES,
        "actual_bytes": len(encoded.encode("utf-8")),
    }


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


def _claim_id(
    *, tenant_id: str, principal_id: str, command_scope: str, client_key: str
) -> str:
    """claim_id 从**完整逻辑键**派生。

    审查发现的缺陷：早先只哈希 client_key，而 claim_id 是全局主键 ——
    两个用户/两个端点用同一个客户端 key 时，本应互不相干的占用
    会在主键上相撞，返回 500。派生必须覆盖逻辑键的每个维度。
    """
    return "claim_" + content_hash(
        {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "command_scope": command_scope,
            "client_key": client_key,
        }
    )[:16]


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
        owner_token: str | None = None,
    ) -> None: ...

    def release(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        owner_token: str | None = None,
    ) -> None: ...


class InMemoryHttpIdempotencyStore:
    """内存实现（开发适配器）。结构镜像数据库行，语义与 PG 版一致。

    租约时钟用 `time.monotonic()`：单调时钟不受系统时间跳变影响，
    测试可通过 `lease_seconds=0` 注入"立即过期"。
    """

    def __init__(self, *, lease_seconds: float = IDEMPOTENCY_LEASE_SECONDS) -> None:
        self._lease_seconds = lease_seconds
        self._lock = threading.RLock()
        # key -> {claim_id, fingerprint, state, status_code, response_body,
        #         owner_token, claimed_at}
        self._rows: dict[tuple[str, str, str, str], dict] = {}

    @staticmethod
    def _key(
        tenant_id: str, principal_id: str, command_scope: str, client_key: str
    ) -> tuple[str, str, str, str]:
        return (tenant_id, principal_id, command_scope, client_key)

    def _expired(self, row: dict) -> bool:
        claimed_at = row.get("claimed_at")
        if claimed_at is None:
            return False
        # >= 而不是 >：lease_seconds=0（测试注入"立即过期"）时也成立。
        return (time.monotonic() - claimed_at) >= self._lease_seconds

    def claim(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        fingerprint: str,
    ) -> ClaimOutcome:
        with self._lock:
            return self._claim_unlocked(
                tenant_id=tenant_id,
                principal_id=principal_id,
                command_scope=command_scope,
                client_key=client_key,
                fingerprint=fingerprint,
            )

    def _claim_unlocked(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        fingerprint: str,
    ) -> ClaimOutcome:
        key = self._key(tenant_id, principal_id, command_scope, client_key)
        row = self._rows.get(key)
        owner_token = new_id("idm")
        if row is None:
            self._rows[key] = {
                "claim_id": _claim_id(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    command_scope=command_scope,
                    client_key=client_key,
                ),
                "fingerprint": fingerprint,
                "state": "pending",
                "status_code": None,
                "response_body": None,
                "owner_token": owner_token,
                "claimed_at": time.monotonic(),
            }
            return ClaimOutcome(kind="claimed", owner_token=owner_token)
        if row["fingerprint"] != fingerprint:
            return ClaimOutcome(kind="violation")
        if row["state"] == "completed":
            return ClaimOutcome(
                kind="replay",
                cached_status_code=row["status_code"],
                cached_body=row["response_body"],
            )
        if row["state"] == "indeterminate":
            return ClaimOutcome(kind="reconciliation_required")
        if row["state"] == "released":
            # 只有业务明确失败并 release 才允许重新执行。
            row["state"] = "pending"
            row["owner_token"] = owner_token
            row["claimed_at"] = time.monotonic()
            return ClaimOutcome(kind="claimed", owner_token=owner_token)
        if row["state"] == "pending" and self._expired(row):
            row["state"] = "indeterminate"
            row["owner_token"] = None
            return ClaimOutcome(kind="reconciliation_required")
        return ClaimOutcome(kind="in_progress")

    def complete(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        status_code: int,
        response_body: dict,
        owner_token: str | None = None,
    ) -> None:
        with self._lock:
            self._complete_unlocked(
                tenant_id=tenant_id,
                principal_id=principal_id,
                command_scope=command_scope,
                client_key=client_key,
                status_code=status_code,
                response_body=response_body,
                owner_token=owner_token,
            )

    def _complete_unlocked(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        status_code: int,
        response_body: dict,
        owner_token: str | None = None,
    ) -> None:
        key = self._key(tenant_id, principal_id, command_scope, client_key)
        row = self._rows.get(key)
        if (
            row is not None
            and row["state"] == "pending"
            # 严格相等（IS NOT DISTINCT FROM 语义，与 PG 版一致）：
            # owner_token=None 只匹配没有令牌的行，绝不充当通配符 ——
            # 否则旧持有者迟到完成会覆盖新持有者的占用。
            and row.get("owner_token") == owner_token
        ):
            row["state"] = "completed"
            row["status_code"] = status_code
            row["response_body"] = _bounded_body(response_body)

    def release(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        owner_token: str | None = None,
    ) -> None:
        with self._lock:
            self._release_unlocked(
                tenant_id=tenant_id,
                principal_id=principal_id,
                command_scope=command_scope,
                client_key=client_key,
                owner_token=owner_token,
            )

    def _release_unlocked(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        command_scope: str,
        client_key: str,
        owner_token: str | None = None,
    ) -> None:
        key = self._key(tenant_id, principal_id, command_scope, client_key)
        row = self._rows.get(key)
        if (
            row is not None
            and row["state"] == "pending"
            and row.get("owner_token") == owner_token
        ):
            row["state"] = "released"


class PostgresHttpIdempotencyStore:
    """PostgreSQL 实现。占用的原子性由 UNIQUE (tenant_id, principal_id,
    command_scope, client_key) 保证 —— INSERT 成功即占用成功，
    冲突即别人持有，不需要任何进程内锁。

    租约到期只把 `pending` 原子推进到 `indeterminate`。业务可能已经提交，
    未经对账直接接管会重复副作用。
    """

    def __init__(self, dsn: str | None = None, *, connect_factory=None) -> None:
        self._dsn = dsn
        if connect_factory is None:
            raise TypeError("PostgreSQL 适配器必须由 app.db.idempotency_store 装配")
        self._connect = connect_factory

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
        owner_token = new_id("idm")
        with self._connect(self._dsn) as conn:
            self._set_rls_context(conn, tenant_id, principal_id)
            inserted = conn.execute(
                "INSERT INTO http_idempotency"
                " (claim_id, tenant_id, principal_id, command_scope, client_key,"
                "  request_hash, project_id, state, owner_token)"
                " VALUES (%s, %s, %s, %s, %s, %s, NULL, 'pending', %s)"
                " ON CONFLICT (tenant_id, principal_id, command_scope, client_key)"
                " DO NOTHING"
                " RETURNING claim_id",
                (
                    _claim_id(
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                        command_scope=command_scope,
                        client_key=client_key,
                    ),
                    tenant_id, principal_id, command_scope, client_key, fingerprint,
                    owner_token,
                ),
            ).fetchone()
            if inserted is not None:
                conn.commit()
                return ClaimOutcome(kind="claimed", owner_token=owner_token)

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
            if state_name == "indeterminate":
                conn.commit()
                return ClaimOutcome(kind="reconciliation_required")
            if state_name == "released":
                # 释放后的重试 = 重新执行：CAS 到 pending（仍以唯一键防并发抢占）。
                updated = conn.execute(
                    "UPDATE http_idempotency SET state = 'pending', owner_token = %s,"
                    " claimed_at = now()"
                    " WHERE tenant_id = %s AND principal_id = %s"
                    "   AND command_scope = %s AND client_key = %s"
                    "   AND state = 'released'"
                    " RETURNING claim_id",
                    (owner_token, tenant_id, principal_id, command_scope, client_key),
                ).fetchone()
                conn.commit()
                if updated is not None:
                    return ClaimOutcome(kind="claimed", owner_token=owner_token)
                return ClaimOutcome(kind="in_progress")

            # pending 超时只能说明持有者失联，不能证明业务未提交。
            # 原子进入 indeterminate，禁止后来者盲目重执行。
            indeterminate = conn.execute(
                "UPDATE http_idempotency SET state = 'indeterminate', owner_token = NULL"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s"
                "   AND state = 'pending'"
                "   AND claimed_at < now() - make_interval(secs => %s)"
                " RETURNING claim_id",
                (
                    tenant_id, principal_id, command_scope, client_key,
                    float(IDEMPOTENCY_LEASE_SECONDS),
                ),
            ).fetchone()
            conn.commit()
            if indeterminate is not None:
                return ClaimOutcome(kind="reconciliation_required")
            return ClaimOutcome(kind="in_progress")

    def complete(
        self, *, tenant_id: str, principal_id: str, command_scope: str,
        client_key: str, status_code: int, response_body: dict,
        owner_token: str | None = None,
    ) -> None:
        with self._connect(self._dsn) as conn:
            self._set_rls_context(conn, tenant_id, principal_id)
            # owner_token 校验：占用不属于当前持有者或已经进入不确定态时，
            # 迟到的 complete 不得改写结果。
            conn.execute(
                "UPDATE http_idempotency"
                " SET state = 'completed', status_code = %s, response_body = %s,"
                "     completed_at = now()"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s AND state = 'pending'"
                "   AND owner_token IS NOT DISTINCT FROM %s",
                (
                    status_code,
                    json.dumps(_bounded_body(response_body), ensure_ascii=False, sort_keys=True),
                    tenant_id, principal_id, command_scope, client_key, owner_token,
                ),
            )
            conn.commit()

    def release(
        self, *, tenant_id: str, principal_id: str, command_scope: str,
        client_key: str, owner_token: str | None = None,
    ) -> None:
        with self._connect(self._dsn) as conn:
            self._set_rls_context(conn, tenant_id, principal_id)
            conn.execute(
                "UPDATE http_idempotency SET state = 'released'"
                " WHERE tenant_id = %s AND principal_id = %s"
                "   AND command_scope = %s AND client_key = %s AND state = 'pending'"
                "   AND owner_token IS NOT DISTINCT FROM %s",
                (tenant_id, principal_id, command_scope, client_key, owner_token),
            )
            conn.commit()


def raise_violation() -> None:
    """同键不同内容的统一拒绝（供守卫层调用，保持错误构造单点）。"""
    raise deny(
        ErrorCode.IDEMPOTENCY_VIOLATION,
        "同一 idempotency_key 被用于内容不同的请求；"
        "幂等键必须唯一标识一次请求，不得复用于不同参数",
    )


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
    #: 本次占用的所有者令牌。complete/release 携带它：
    #: 占用进入不确定态后，旧持有者的收尾不再改写结果。
    _owner_token: str | None = None
    _completion_attempted: bool = False

    _done: bool = False

    def complete(self, status_code: int, response_body: dict) -> None:
        if self._store is None:
            # 直通模式（幂等存储未装配）：没有存储可写，完成即 no-op。
            return
        assert self._keys is not None, "claim 成功的守卫必然携带键组"
        # From this point the business effect has already succeeded. If the
        # cache write fails, releasing would authorize a duplicate execution.
        self._completion_attempted = True
        self._store.complete(
            **self._keys, status_code=status_code, response_body=response_body,
            owner_token=self._owner_token,
        )
        self._done = True

    def _release(self) -> None:
        if self._store is None:
            return
        assert self._keys is not None, "claim 成功的守卫必然携带键组"
        self._store.release(**self._keys, owner_token=self._owner_token)


from contextlib import contextmanager  # noqa: E402

from fastapi import Request  # noqa: E402


@contextmanager
def idempotent_write(request: Request, body_model=None):
    """写端点的幂等守卫。**Idempotency-Key 必填**。

    - 无 ``Idempotency-Key`` 头 → 拒绝（``IDEMPOTENCY_KEY_REQUIRED``，400）。
      早先"缺头直通"是一个可靠性缺口：客户端超时重试会重复创建
      项目/消息/计划，而守卫只需"不发头"即可完全绕过。
    - 有头 → 校验长度（≤200 字符）→ claim；``guard.replay`` 为真时端点
      **必须**返回缓存响应；
    - 端点执行异常 → release（失败不缓存）；正常返回但没调 ``complete``
      也 release（没有可缓存的确定结果，等于本次没做成）。
    """
    state = request.app.state.platform
    store = state.http_idempotency
    key = request.headers.get("Idempotency-Key")

    # 身份必须来自统一认证入口；这里不重复实现认证规则。
    # 认证先于幂等判定：未认证请求 401，而不是 400 —— 错误语义不混淆。
    from app.api.auth_routes import authenticate_request

    principal = authenticate_request(request)

    if store is None:
        # 幂等存储未装配（理论上的直通分支）：端点照常执行，无防重放。
        yield WriteGuard(replay=False, principal=principal, _store=None, _keys={}, _done=True)
        return
    if key is None or not key.strip():
        raise deny(
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
            "变更请求必须携带 Idempotency-Key 头（客户端生成的本次命令唯一键）；"
            "缺失时服务端无法区分'首次执行'与'超时重试'",
        )
    key = key.strip()
    if len(key) > IDEMPOTENCY_KEY_MAX_CHARS:
        raise deny(
            ErrorCode.PARAMS_INVALID,
            f"Idempotency-Key 最长 {IDEMPOTENCY_KEY_MAX_CHARS} 字符"
            f"（收到 {len(key)} 字符）",
        )
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
    if outcome.kind == "reconciliation_required":
        raise deny(
            ErrorCode.RECONCILIATION_REQUIRED,
            "上一次请求的最终结果未知；请先查询当前资源状态或联系支持完成对账，"
            "不要使用新幂等键重复提交",
        )

    guard = WriteGuard(
        replay=outcome.kind == "replay",
        principal=principal,
        cached_status_code=outcome.cached_status_code,
        cached_body=outcome.cached_body,
        _store=store,
        _keys=keys,
        _owner_token=outcome.owner_token,
    )
    try:
        yield guard
    except BaseException:
        if not guard._completion_attempted:
            guard._release()
        raise
    if not guard._done:
        # 正常返回但端点没标记 complete：没有可缓存的确定结果。
        guard._release()
