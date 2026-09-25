"""注册、登录与退出（认证 HTTP 入口）。

## 本模块存在的意义

用户通过**注册或密码登录**取得会话，全程不需要终端、不需要粘贴 bearer 令牌：

```
POST /auth/register  {"username": ..., "password": ...}
  → 建账号 + 首个密码会话 + 默认项目（原子）
POST /auth/login     {"username": ..., "password": ...}
  → 校验密码 → 种下 HttpOnly cookie，响应里**没有任何口令材料**
  → 未知账号 / 密码错误 / 已禁用 → 同一个错误码同一句话
  → 按客户端键与账号键限流，超限 429 + Retry-After

POST /auth/logout       → 撤销当前会话 + 清除 cookie
POST /auth/logout/all   → 集中失效本主体全部会话（退出所有设备）+ 清除 cookie
```

## 安全边界

- **客户端没有身份参数可传**：身份由账号行决定，请求体里只有用户名与口令；
- **口令与哈希不出现在任何响应/审计载荷里**；
- **失败统一拒绝**：探测者无法区分"用户不存在"和"密码错误"；
- **CSRF 严格模式**：凭 cookie 的不安全方法**必须**携带可信 Origin；
  无 Origin 时只接受同源 Referer；两者都没有 → 拒绝。可信集合 =
  配置的外部 Origin 白名单 ∪ 请求自身 Host 来源（反代下按
  X-Forwarded-* 计算，且只有显式声明 `STUDY_PLATFORM_BEHIND_PROXY=1`
  才信任转发头，否则客户端可随意伪造）；
- **Bearer 兜底可整体关闭**：生产装配关闭 bearer，只认 cookie；
- **审计**：注册成功/失败、认证失败、限流命中、退出（单个/全部）
  全部进入审计事实源。
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.audit.sink import RiskLevel
from app.core.errors import ErrorCode, PlatformError, deny
from app.identity.cookie_auth import SESSION_COOKIE_NAME
from app.identity.models import Principal
from app.identity.passwords import (
    DUMMY_PASSWORD_HASH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    normalize_username,
    verify_password_diagnostic,
)

if TYPE_CHECKING:  # 类型标注用，运行时不导入（避免与装配模块循环依赖）
    from app.platform import PlatformState

router = APIRouter()

REGISTER_LIMIT = 5
REGISTER_WINDOW_SECONDS = 3600
LOGIN_CLIENT_LIMIT = 30
LOGIN_ACCOUNT_LIMIT = 10
LOGIN_WINDOW_SECONDS = 600

#: 不依赖环境凭证的"安全方法"：即使带 cookie 也不做 CSRF 判定。
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class PasswordAuthBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=16)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


def _state(request: Request):
    return request.app.state.platform


def _username_digest(request: Request, username: str) -> str:
    configured = getattr(getattr(_state(request), "settings", None), "cookie_secret", "audit-key")
    return hmac.new(configured.encode("utf-8"), username.encode("utf-8"), sha256).hexdigest()


def _auth_response(request: Request, payload: dict, *, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _set_session_cookie(request: Request, response: JSONResponse, session) -> JSONResponse:
    state = _state(request)
    cookie_value = state.cookie_auth.issue(session)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie_value,
        httponly=True,
        samesite="lax",
        secure=state.cookie_secure,
        max_age=int(state.session_ttl.total_seconds()),
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _live_cookie_session(request: Request, state):
    """Return a live cookie session; bad/expired/revoked cookies are anonymous."""
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None
    try:
        claims = state.cookie_auth.verify(raw, now=state.clock.now())
        return state.session_store.get_live(claims.to_principal(), claims.session_id)
    except Exception:
        return None


def _reject_if_authenticated(request: Request, state) -> None:
    session = _live_cookie_session(request, state)
    if session is None:
        return
    raise deny(ErrorCode.ACCOUNT_ALREADY_AUTHENTICATED, "当前浏览器已有有效会话，请先退出")


def _require_password_origin(request: Request, state) -> None:
    try:
        _require_same_origin(request, state)
    except PlatformError as exc:
        if exc.code is ErrorCode.CSRF_DENIED:
            _audit_auth_failure(request, state, exc)
        raise


def _argon2(state):
    from app.identity.argon2_pool import Argon2WorkPool

    pool = state.argon2_pool
    if pool is None:
        pool = Argon2WorkPool(
            state.auth_argon2_max_concurrency,
            state.auth_argon2_queue_limit,
            state.auth_argon2_wait_seconds,
        )
        state.argon2_pool = pool
    return pool


def _check_json_body(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/json"):
        raise deny(ErrorCode.PARAMS_INVALID, "请求必须使用 application/json")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > 64 * 1024:
                raise deny(ErrorCode.PARAMS_INVALID, "请求体过大")
        except ValueError as exc:
            raise deny(ErrorCode.PARAMS_INVALID, "请求体长度无效") from exc


# ------------------------------------------------------------ CSRF / 客户端键


def _request_host_origin(request: Request) -> str | None:
    """本次请求自身的 Origin（scheme://host[:port]）。

    反向代理场景只有在请求确实来自**可信代理**时才采信 X-Forwarded-* ——
    这些头客户端可以随便造，无条件信任等于让 CSRF 白名单形同虚设。
    """
    headers = request.headers
    if _peer_is_trusted_proxy(request):
        host = (
            headers.get("x-forwarded-host", "").split(",")[0].strip()
            or headers.get("host", "").strip()
        )
        scheme = (
            headers.get("x-forwarded-proto", "").split(",")[0].strip()
            or request.url.scheme
        )
    else:
        host = headers.get("host", "").strip()
        scheme = request.url.scheme
    if not host:
        return None
    return f"{scheme.lower()}://{host.lower()}"


def _origin_from_url(value: str) -> str | None:
    """从绝对 URL 提取 scheme://host[:port]；非法返回 None。"""
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _require_same_origin(request: Request, state: "PlatformState") -> None:
    """cookie 认证的**不安全方法**必须通过严格同源校验。

    判定顺序（主流框架严格模式一致）：

    1. 有 ``Origin``：必须命中可信集合（白名单 ∪ 请求自身 Host 来源）；
    2. 无 ``Origin`` 但有 ``Referer``：Referer 的源必须命中可信集合；
    3. 两者都没有 / 都不可信 → ``CSRF_DENIED``。

    「没有 Origin 就放行」是第 1 轮修掉的漏洞：非浏览器客户端可以不带
    Origin 直接发 POST，浏览器在部分隐私模式下也会省略 Origin。
    """
    if request.method in _SAFE_METHODS:
        return
    trusted = set(state.trusted_origins)
    host_origin = _request_host_origin(request)
    if host_origin is not None:
        trusted.add(host_origin)

    origin = request.headers.get("origin")
    if origin:
        if origin.strip().lower().rstrip("/") in trusted:
            return
        raise deny(ErrorCode.CSRF_DENIED, "请求来源与会话站点不一致（Origin 不匹配）")

    referer = request.headers.get("referer")
    if referer:
        referer_origin = _origin_from_url(referer)
        if referer_origin is not None and referer_origin in trusted:
            return
        raise deny(ErrorCode.CSRF_DENIED, "请求来源与会话站点不一致（Referer 不匹配）")

    raise deny(ErrorCode.CSRF_DENIED, "请求缺少 Origin/Referer，无法确认同源")


def _peer_is_trusted_proxy(request: Request) -> bool:
    """请求的 TCP 对端是否在可信代理清单里。

    behind_proxy=1 但对端不可信（或没配可信清单）时，X-Forwarded-* 一律
    不采信 —— 审查实测的缺陷：无条件信任 XFF 首值，攻击者轮换该值
    即可无限重置限流桶。
    """
    state = _state(request)
    if not state.behind_proxy:
        return False
    peer = request.client.host if request.client is not None else None
    if peer is None or not state.trusted_proxies:
        return False
    return _matches_proxy_entry(peer, state.trusted_proxies)


def _matches_proxy_entry(value: str, proxies: tuple[str, ...]) -> bool:
    """value 是否命中任一可信代理条目（IP 精确 / CIDR 网段 / 显式列出的标识）。

    非 IP 的对端标识（如 TestClient 的 "testclient"）只允许**精确匹配**；
    CIDR 匹配只在条目与值都可解析为 IP 时进行，解析失败不放大权限。
    """
    import ipaddress

    for entry in proxies:
        if entry == value:
            return True
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            continue
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _client_key(request: Request, state: "PlatformState") -> str:
    """限流客户端键。

    - 未声明反代：TCP 对端（客户端影响不了它）；
    - 声明反代且对端可信：从 X-Forwarded-For **从右往左**跳过可信代理，
      取第一个不可信地址 —— 直接取首值会把攻击者可控的最左项当真，
      而从右往左走是 XFF 语义里唯一站得住的方向（最右由最近的代理写入，
      最左是客户端自报的）；
    - 其余情况（对端不可信 / XFF 解析不出）：TCP 对端。
    """
    peer = request.client.host if request.client is not None else "unknown"
    if not state.behind_proxy:
        return peer
    if peer == "unknown" or not state.trusted_proxies:
        return peer
    if not _matches_proxy_entry(peer, state.trusted_proxies):
        # 请求不来自可信代理：XFF 是客户端自说自话，不采信。
        return peer
    forwarded = [
        part.strip()
        for part in request.headers.get("x-forwarded-for", "").split(",")
        if part.strip()
    ]
    for candidate in reversed(forwarded):
        if _matches_proxy_entry(candidate, state.trusted_proxies):
            continue
        # 键必须是可解析的地址：解析不了说明有人塞了垃圾，
        # 退回 TCP 对端 —— 绝不让任意字符串进入限流桶。
        import ipaddress

        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return peer
        return candidate
    # 全部条目都是可信代理（内级调用）：用 TCP 对端兜底。
    return peer


# --------------------------------------------------------------------- 认证


def _audit_auth_failure(request: Request, state: "PlatformState", exc: PlatformError) -> None:
    """认证失败统一进审计事实源（HIGH：安全追踪与异常告警依赖它）。"""
    stage = "csrf_origin" if exc.code is ErrorCode.CSRF_DENIED else "credential"
    state.audit.append(
        "authentication_failed",
        {
            "stage": stage,
            "path": request.url.path,
            "client_key": _client_key(request, state),
        },
        risk=RiskLevel.HIGH,
    )


def _authenticate(request: Request, state: "PlatformState") -> Principal:
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is not None:
        claims = state.cookie_auth.verify(raw, now=state.clock.now())
        _require_same_origin(request, state)
        if state.session_store.get_live(claims.to_principal(), claims.session_id) is None:
            # 已撤销 / 已过期 / 不存在 —— 同一种拒绝。
            # 撤销立刻生效的全部根据就在这次回库查询上。
            raise deny(ErrorCode.AUTH_REQUIRED, "未认证或凭据无效")
        return claims.to_principal()

    # bearer 是显式的运维/测试兼容适配器。生产装配整体关闭它，
    # 避免"浏览器路径收紧了、开发通道还开着"的双标面。
    if not state.bearer_enabled:
        raise deny(ErrorCode.AUTH_REQUIRED, "未认证或凭据无效")
    return state.auth.authenticate(request.headers.get("Authorization"))


def authenticate_request(request: Request) -> Principal:
    """HTTP 请求的**唯一**认证入口：cookie 优先，bearer 受开关控制兜底。"""
    state = _state(request)
    try:
        return _authenticate(request, state)
    except PlatformError as exc:
        if exc.code in (ErrorCode.AUTH_REQUIRED, ErrorCode.CSRF_DENIED):
            _audit_auth_failure(request, state, exc)
        raise


# --------------------------------------------------------------------- 端点


def _require_cookie_session(request: Request):
    """logout 系列端点共同前置：有效签名 cookie + 严格 CSRF，失败统一审计。"""
    state = _state(request)
    try:
        raw = request.cookies.get(SESSION_COOKIE_NAME)
        if raw is None:
            raise deny(ErrorCode.AUTH_REQUIRED, "未认证或凭据无效")
        claims = state.cookie_auth.verify(raw, now=state.clock.now())
        _require_same_origin(request, state)
        if state.session_store.get_live(claims.to_principal(), claims.session_id) is None:
            raise deny(ErrorCode.AUTH_REQUIRED, "未认证或凭据无效")
        return claims
    except PlatformError as exc:
        if exc.code in (ErrorCode.AUTH_REQUIRED, ErrorCode.CSRF_DENIED):
            _audit_auth_failure(request, state, exc)
        raise


def _raise_auth_limit(
    request: Request,
    *,
    bucket: str,
    scope: str | None = None,
    limit: int | None = None,
    window_seconds: int | None = None,
) -> None:
    state = _state(request)
    decision = state.rate_limiter.register(
        f"{bucket}:{scope or _client_key(request, state)}",
        now=state.clock.now(),
        limit=limit,
        window_seconds=window_seconds,
    )
    if not decision.allowed:
        state.audit.append(
            "auth_rate_limited",
            {"client_key": _client_key(request, state), "bucket": bucket},
            risk=RiskLevel.LOW,
        )
        raise PlatformError(
            code=ErrorCode.RATE_LIMITED,
            message="尝试过于频繁，请稍后再试",
            retryable=True,
            details={"retry_after_seconds": decision.retry_after_seconds},
        )


@router.post("/auth/register")
def register_account(request: Request, body: PasswordAuthBody) -> JSONResponse:
    """Create a password account and its first password session atomically."""
    state = _state(request)
    _check_json_body(request)
    _reject_if_authenticated(request, state)
    if not state.registration_enabled:
        raise deny(ErrorCode.REGISTRATION_DISABLED, "注册暂未开放")
    if state.accounts is None:
        raise deny(ErrorCode.REGISTRATION_DISABLED, "注册暂未配置")
    _require_password_origin(request, state)
    _raise_auth_limit(
        request,
        bucket="register",
        limit=REGISTER_LIMIT,
        window_seconds=REGISTER_WINDOW_SECONDS,
    )
    try:
        username = normalize_username(body.username)
    except (TypeError, ValueError) as exc:
        raise deny(ErrorCode.PARAMS_INVALID, "用户名格式不符合要求") from exc
    try:
        password_hash = _argon2(state).run(
            lambda: hash_password(body.password), priority="registration"
        )
    except ValueError as exc:
        raise deny(
            ErrorCode.PARAMS_INVALID,
            f"密码长度需为 {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} 个字符",
        ) from exc
    result = state.accounts.register(
        username_original=username.original,
        username_normalized=username.normalized,
        password_hash=password_hash,
        hash_version=1,
        session_expires_at=state.clock.now() + state.session_ttl,
    )
    if state.audit_outbox is not None:
        state.audit_outbox.flush_pending(result.lookup.tenant_id)
    response = _auth_response(
        request,
        {
            "principal_id": result.session.principal_id,
            "expires_at": result.session.expires_at.isoformat(),
            "default_project_id": result.default_project_id,
            "default_project_name": result.default_project_name,
        },
        status_code=201,
    )
    return _set_session_cookie(request, response, result.session)


@router.post("/auth/login")
def password_login(request: Request, body: PasswordAuthBody) -> JSONResponse:
    state = _state(request)
    _check_json_body(request)
    _reject_if_authenticated(request, state)
    if not state.password_login_enabled:
        raise deny(ErrorCode.PASSWORD_LOGIN_DISABLED, "密码登录暂未启用")
    if state.accounts is None:
        raise deny(ErrorCode.PASSWORD_LOGIN_DISABLED, "密码登录暂未配置")
    try:
        username = normalize_username(body.username)
    except (TypeError, ValueError):
        # Keep malformed usernames on the same public failure path as unknown users.
        username = None
    _require_password_origin(request, state)
    _raise_auth_limit(
        request,
        bucket="login_client",
        limit=LOGIN_CLIENT_LIMIT,
        window_seconds=LOGIN_WINDOW_SECONDS,
    )
    account_scope = _username_digest(
        request, username.normalized if username is not None else body.username
    )
    _raise_auth_limit(
        request,
        bucket="login_account",
        scope=account_scope,
        limit=LOGIN_ACCOUNT_LIMIT,
        window_seconds=LOGIN_WINDOW_SECONDS,
    )
    lookup = state.accounts.lookup(username.normalized) if username is not None else None
    encoded = lookup.password_hash if lookup is not None else DUMMY_PASSWORD_HASH
    ok, _reason = _argon2(state).run(
        lambda: verify_password_diagnostic(body.password, encoded), priority="login"
    )
    if not ok or lookup is None or lookup.disabled_at is not None:
        state.audit.append(
            "password_login_failed",
            {
                "username_digest": _username_digest(request, username.normalized if username else body.username),
                "client_key": _client_key(request, state),
            },
            risk=RiskLevel.HIGH,
        )
        raise deny(ErrorCode.AUTH_REQUIRED, "用户名或密码错误")
    new_hash = None
    if needs_rehash(lookup.password_hash):
        new_hash = _argon2(state).run(lambda: hash_password(body.password), priority="login")
    session = state.accounts.complete_login(
        lookup=lookup,
        session_expires_at=state.clock.now() + state.session_ttl,
        new_password_hash=new_hash,
        new_hash_version=1 if new_hash else None,
    )
    if session is None:
        raise deny(ErrorCode.AUTH_REQUIRED, "用户名或密码错误")
    if state.audit_outbox is not None:
        state.audit_outbox.flush_pending(lookup.tenant_id)
    response = _auth_response(
        request,
        {"principal_id": session.principal_id, "expires_at": session.expires_at.isoformat()},
    )
    return _set_session_cookie(request, response, session)


@router.post("/auth/logout")
def logout(request: Request) -> JSONResponse:
    """退出：撤销数据库会话并清除 cookie。

    cookie 是会话的引用，**撤销必须落在库上** —— 只删 cookie 的话，
    那份 cookie 若被复制过（日志、代理、他人屏幕）就还能用。
    """
    state = _state(request)
    try:
        claims = _require_cookie_session(request)
    except PlatformError as exc:
        # A normal logout is idempotent for an expired/revoked/malformed cookie:
        # clear the browser reference, but never claim that a server session was
        # revoked.  Keep the same-origin check so a cross-site request cannot
        # drive even this local cleanup path.  logout/all intentionally keeps the
        # stricter _require_cookie_session-only behavior below.
        if exc.code is not ErrorCode.AUTH_REQUIRED:
            raise
        _require_same_origin(request, state)
        response = _auth_response(
            request,
            {
                "revoked": False,
                "cookie_cleared": request.cookies.get(SESSION_COOKIE_NAME) is not None,
            },
        )
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response
    now = state.clock.now()

    revoked = state.session_store.revoke(claims.to_principal(), claims.session_id, at=now)
    if revoked:
        state.audit.append(
            "session_revoked",
            {"principal_id": claims.principal_id},
            risk=RiskLevel.LOW,
            tenant_id=claims.tenant_id,
        )
    response = JSONResponse({"revoked": revoked})
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@router.post("/auth/logout/all")
def logout_all(request: Request) -> JSONResponse:
    """退出所有设备：集中失效本主体名下全部存活会话（含当前设备）。

    用于 cookie 密钥轮换、设备丢失等场景；返回实际撤销条数。
    """
    state = _state(request)
    claims = _require_cookie_session(request)
    now = state.clock.now()

    revoked_count = state.session_store.revoke_all_for(claims.to_principal(), at=now)
    state.audit.append(
        "sessions_revoked_all",
        {"principal_id": claims.principal_id, "revoked_count": revoked_count},
        risk=RiskLevel.LOW,
        tenant_id=claims.tenant_id,
    )
    response = JSONResponse({"revoked": revoked_count})
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
