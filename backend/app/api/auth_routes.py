"""邀请登录与退出（认证引导 HTTP 入口）。

## 本模块存在的意义

用户通过**一次性邀请**进入系统，全程不需要终端、不需要粘贴 bearer 令牌：

```
POST /auth/invitations/exchange  {"token": "<原始邀请令牌>"}
  → 服务端算 sha256 → InvitationRepository.exchange()（无身份参数）
  → 成功：种下 HttpOnly cookie，响应里**没有任何令牌材料**
  → 失败：未知 / 已过期 / 已消费 / 格式错 → 同一个错误码同一句话
  → 按客户端键限流，超限 429 + Retry-After

POST /auth/logout       → 撤销当前会话 + 清除 cookie
POST /auth/logout/all   → 集中失效本主体全部会话（退出所有设备）+ 清除 cookie
```

## 安全边界（第 1 轮收口后）

- **客户端没有身份参数可传**：请求体里只有 `token`，主体由邀请行预绑定
  （0003 迁移的 `invitee_principal_id` + `SECURITY DEFINER` 兑换函数）；
- **原始令牌与哈希不出现在任何响应/审计载荷里**；
- **失败统一拒绝**：探测者无法区分"token 不存在"和"token 已被用过"；
- **CSRF 严格模式**：凭 cookie 的不安全方法**必须**携带可信 Origin；
  无 Origin 时只接受同源 Referer；两者都没有 → 拒绝。可信集合 =
  配置的外部 Origin 白名单 ∪ 请求自身 Host 来源（反代下按
  X-Forwarded-* 计算，且只有显式声明 `STUDY_PLATFORM_BEHIND_PROXY=1`
  才信任转发头，否则客户端可随意伪造）；
- **Bearer 兜底可整体关闭**：生产装配关闭 bearer，只认 cookie；
- **审计**：兑换成功/被拒、认证失败、限流命中、退出（单个/全部）
  全部进入审计事实源。
"""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.audit.sink import RiskLevel
from app.core.errors import ErrorCode, PlatformError, deny
from app.core.ids import new_id
from app.identity.cookie_auth import SESSION_COOKIE_NAME
from app.identity.models import Principal

if TYPE_CHECKING:  # 类型标注用，运行时不导入（避免与 main 循环依赖）
    from app.main import PlatformState

router = APIRouter()

#: 邀请令牌的长度上限。原始令牌是高熵随机串；超长输入不是用户，是探测。
TOKEN_MAX_CHARS = 4096

#: 不依赖环境凭证的"安全方法"：即使带 cookie 也不做 CSRF 判定。
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class ExchangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=TOKEN_MAX_CHARS)


def _state(request: Request):
    return request.app.state.platform


def _token_hash(raw_token: str) -> str:
    """原始令牌 → 固定长度哈希。**只存哈希**：库与日志里永远没有原始令牌。"""
    digest = sha256(raw_token.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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


@router.post("/auth/invitations/exchange")
def exchange_invitation(request: Request, body: ExchangeBody) -> JSONResponse:
    """兑换邀请：限流 → 兑换 → 审计 → 种下会话 cookie。

    这是**认证引导**端点 —— 调用方此刻还没有任何身份，所以本端点不做认证，
    也不做 CSRF 来源检查（没有环境凭证可被利用），但必须限流。
    """
    state = _state(request)
    now = state.clock.now()
    client_key = _client_key(request, state)

    decision = state.rate_limiter.register(client_key, now=now)
    if not decision.allowed:
        state.audit.append(
            "auth_rate_limited",
            {"client_key": client_key, "attempts": decision.attempts},
            risk=RiskLevel.LOW,
        )
        # 统一走 error_response：429 + Retry-After + 标准错误体（含 request_id）。
        raise PlatformError(
            code=ErrorCode.RATE_LIMITED,
            message="尝试过于频繁，请稍后再试",
            retryable=True,
            details={"retry_after_seconds": decision.retry_after_seconds},
        )

    try:
        session = state.invitations.exchange(
            _token_hash(body.token),
            session_id=new_id("sess"),
            session_expires_at=now + state.session_ttl,
        )
    except PlatformError:
        # 内部一致性错误（例如 TTL 越界）不伪装成"邀请无效"，原样上抛为 5xx。
        raise
    if session is None:
        # 审计同一条事件、载荷不带失败原因（未知/过期/已消费不可区分）。
        # 未发生任何业务写入，这里保持同步写链式 sink：sink 不可用时
        # fail-closed 是真实的 —— 拒绝响应的同时确实什么都没发生。
        state.audit.append(
            "invitation_rejected",
            {"client_key": client_key},
            risk=RiskLevel.HIGH,
        )
        # 统一拒绝：不给"token 存在但已被用掉"留任何可区分的信号。
        raise deny(ErrorCode.INVITATION_INVALID, "邀请无效、已过期或已被使用")

    # 成功路径的审计事实已与业务**同一事务**写入审计 outbox
    # （数据库版）/同一临界区（内存版）—— sink 不可用不再可能
    # "邀请已消费却返回 503"。这里只负责把事实投影进链式 sink；
    # 投影失败时事件保持 pending 可观测，待后续请求补投影。
    if state.audit_outbox is not None:
        state.audit_outbox.flush_pending(session.tenant_id)

    cookie_value = state.cookie_auth.issue(session)
    response = JSONResponse(
        {
            "principal_id": session.principal_id,
            "expires_at": session.expires_at.isoformat(),
        }
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie_value,
        httponly=True,      # JS 读不到：XSS 偷不走会话
        samesite="lax",     # 跨站导航不带 cookie：CSRF 的第一道（非全部）防线
        secure=state.cookie_secure,
        max_age=int(state.session_ttl.total_seconds()),
        path="/",
    )
    return response


def _require_cookie_session(request: Request):
    """logout 系列端点共同前置：有效签名 cookie + 严格 CSRF，失败统一审计。"""
    state = _state(request)
    try:
        raw = request.cookies.get(SESSION_COOKIE_NAME)
        if raw is None:
            raise deny(ErrorCode.AUTH_REQUIRED, "未认证或凭据无效")
        claims = state.cookie_auth.verify(raw, now=state.clock.now())
        _require_same_origin(request, state)
        return claims
    except PlatformError as exc:
        if exc.code in (ErrorCode.AUTH_REQUIRED, ErrorCode.CSRF_DENIED):
            _audit_auth_failure(request, state, exc)
        raise


@router.post("/auth/logout")
def logout(request: Request) -> JSONResponse:
    """退出：撤销数据库会话并清除 cookie。

    cookie 是会话的引用，**撤销必须落在库上** —— 只删 cookie 的话，
    那份 cookie 若被复制过（日志、代理、他人屏幕）就还能用。
    """
    state = _state(request)
    claims = _require_cookie_session(request)
    now = state.clock.now()

    revoked = state.session_store.revoke(claims.to_principal(), claims.session_id, at=now)
    if revoked:
        state.audit.append(
            "session_revoked",
            {"session_id": claims.session_id},
            risk=RiskLevel.LOW,
            tenant_id=claims.tenant_id,
        )
    response = JSONResponse({"revoked": revoked})
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
        {"session_id": claims.session_id, "revoked_count": revoked_count},
        risk=RiskLevel.LOW,
        tenant_id=claims.tenant_id,
    )
    response = JSONResponse({"revoked": revoked_count})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
