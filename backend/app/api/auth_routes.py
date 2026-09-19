"""邀请登录与退出（Task 3 的 HTTP 入口）。

## 本模块存在的意义

用户通过**一次性邀请**进入系统，全程不需要终端、不需要粘贴 bearer 令牌：

```
POST /auth/invitations/exchange  {"token": "<原始邀请令牌>"}
  → 服务端算 sha256 → InvitationRepository.exchange()（无身份参数）
  → 成功：种下 HttpOnly cookie，响应里**没有任何令牌材料**
  → 失败：未知 / 已过期 / 已消费 / 格式错 → 同一个错误码同一句话

POST /auth/logout   → 撤销数据库会话 + 清除 cookie
```

安全属性（都有测试守着）：

- **客户端没有身份参数可传**：请求体里只有 `token`，主体由邀请行预绑定
  （0003 迁移的 `invitee_principal_id` + `SECURITY DEFINER` 兑换函数）；
- **原始令牌与哈希不出现在任何响应里**（响应体没有、Set-Cookie 里也没有 ——
  cookie 里放的是签名声明，与邀请令牌是两回事）；
- **失败统一拒绝**：探测者无法区分"token 不存在"和"token 已被用过"；
- `extra="forbid"`：多传 `tenant_id` / `principal_id` 直接 422 ——
  那类字段出现在认证请求里，应当当场被拒而不是"安全地"忽略。
"""

from __future__ import annotations

from hashlib import sha256
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ErrorCode, deny
from app.core.ids import new_id
from app.identity.cookie_auth import SESSION_COOKIE_NAME
from app.identity.models import Principal

router = APIRouter()

#: 邀请令牌的长度上限。原始令牌是高熵随机串；超长输入不是用户，是探测。
TOKEN_MAX_CHARS = 4096


class ExchangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=TOKEN_MAX_CHARS)


def _state(request: Request):
    return request.app.state.platform


def _token_hash(raw_token: str) -> str:
    """原始令牌 → 固定长度哈希。**只存哈希**：库与日志里永远没有原始令牌。"""
    digest = sha256(raw_token.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# --------------------------------------------------------------------- 认证


def authenticate_request(request: Request) -> Principal:
    """HTTP 请求的**唯一**认证入口：cookie 优先，bearer 兼容兜底。

    cookie 路径三步、顺序不可换：
    1. 验签 —— 签名无效立刻拒绝，此后才谈得上信任载荷；
    2. CSRF 来源检查（仅不安全方法）—— 利用的是浏览器自动携带的环境凭证；
    3. 回库查撤销 —— cookie 可撤销的全部根据：状态只在数据库里。

    bearer 路径是显式的运维/测试兼容适配器：凭据由调用方显式携带，
    不经过 CSRF 检查（没有"自动携带"可被利用）。
    """
    state = _state(request)
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is not None:
        claims = state.cookie_auth.verify(raw, now=state.clock.now())
        _require_same_origin(request)
        if state.session_store.get_live(claims.to_principal(), claims.session_id) is None:
            # 已撤销 / 已过期 / 不存在 —— 同一种拒绝。
            # 撤销立刻生效的全部根据就在这次回库查询上。
            raise deny(ErrorCode.AUTH_REQUIRED, "未认证或凭据无效")
        return claims.to_principal()
    return state.auth.authenticate(request.headers.get("Authorization"))


def _require_same_origin(request: Request) -> None:
    """cookie 认证的**不安全方法**必须通过来源检查。

    浏览器对不安全请求**总会**带 `Origin`；带了就必须与 Host 一致。
    没有 Origin 的请求不是浏览器发的 —— 显式携带凭据的客户端
    （curl、测试、服务间调用）不在 CSRF 威胁模型内，直接放行。
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("host", "")
    if urlsplit(origin).netloc.lower() != host.lower():
        raise deny(
            ErrorCode.CSRF_DENIED,
            "请求来源与会话站点不一致",
            origin=origin,
        )


# --------------------------------------------------------------------- 端点


@router.post("/auth/invitations/exchange")
def exchange_invitation(request: Request, body: ExchangeBody) -> JSONResponse:
    """兑换邀请：种下会话 cookie。

    这是**认证引导**端点 —— 调用方此刻还没有任何身份，所以本端点不做认证，
    也不做 CSRF 来源检查（没有环境凭证可被利用）。
    """
    state = _state(request)
    session = state.invitations.exchange(
        _token_hash(body.token),
        session_id=new_id("sess"),
        session_expires_at=state.clock.now() + state.session_ttl,
    )
    if session is None:
        # 统一拒绝：不给"token 存在但已被用掉"留任何可区分的信号。
        raise deny(ErrorCode.INVITATION_INVALID, "邀请无效、已过期或已被使用")

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


@router.post("/auth/logout")
def logout(request: Request) -> JSONResponse:
    """退出：撤销数据库会话并清除 cookie。

    cookie 是会话的引用，**撤销必须落在库上** —— 只删 cookie 的话，
    那份 cookie 若被复制过（日志、代理、他人屏幕）就还能用。
    撤销之后同一 cookie 再来，认证路径的回库查询会扑空 → 拒绝。
    """
    state = _state(request)
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        raise deny(ErrorCode.AUTH_REQUIRED, "未认证或凭据无效")

    # 复用统一认证路径的验签与 CSRF 检查；这里只差"撤销"这一步。
    claims = state.cookie_auth.verify(raw, now=state.clock.now())
    _require_same_origin(request)
    revoked = state.session_store.revoke(
        claims.to_principal(), claims.session_id, at=state.clock.now()
    )
    response = JSONResponse({"revoked": revoked})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
