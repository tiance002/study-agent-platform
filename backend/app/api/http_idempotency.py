"""HTTP write guard and request-level idempotency handling."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.errors import ErrorCode, PlatformError, deny
from app.identity.idempotency import (
    IDEMPOTENCY_KEY_MAX_CHARS,
    command_fingerprint,
)
from app.identity.idempotency import (
    MAX_CACHED_RESPONSE_BYTES as MAX_CACHED_RESPONSE_BYTES,
)
from app.identity.idempotency import (
    HttpIdempotencyStore as HttpIdempotencyStore,
)
from app.identity.idempotency import (
    InMemoryHttpIdempotencyStore as InMemoryHttpIdempotencyStore,
)
from app.identity.models import Principal


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

    def replay_response(self) -> JSONResponse:
        """Return the stored response with the observable replay marker."""
        assert self.replay and self.cached_status_code is not None
        return JSONResponse(
            status_code=self.cached_status_code,
            content=self.cached_body,
            headers={"X-Idempotent-Replay": "true"},
        )

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
