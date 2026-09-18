"""认证端口与开发适配器。

`AuthProvider` 是**替换点**：开发用 Bearer 会话令牌；生产接 OIDC / SAML /
企业 SSO，接口不变。业务代码只依赖这个协议，因此换实现不需要改任何调用方。

**关键约束**：`authenticate()` 是**唯一**能产生 `Principal` 的入口。
不允许任何地方从请求字段拼装 `Principal` —— 那等于把认证降级成"客户端声明"。
"""

from __future__ import annotations

from typing import Protocol

from app.core.clock import Clock
from app.core.errors import ErrorCode, deny
from app.identity.models import Principal
from app.identity.session import SessionIssuer


class AuthProvider(Protocol):
    def authenticate(self, authorization: str | None) -> Principal: ...


class BearerSessionAuthProvider:
    """从 `Authorization: Bearer <token>` 解析身份（开发适配器）。

    失败时**不区分**原因（格式错 / 签名错 / 过期都返回同一种错误），
    避免给攻击者提供探测信号。
    """

    def __init__(self, issuer: SessionIssuer, clock: Clock) -> None:
        self._issuer = issuer
        self._clock = clock

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization:
            raise deny(
                ErrorCode.AUTH_REQUIRED,
                "缺少认证凭据；此接口不接受客户端自报的身份",
            )
        scheme, _, raw = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw.strip():
            raise deny(ErrorCode.AUTH_REQUIRED, "仅支持 Bearer 认证方式")
        token = self._issuer.parse(raw.strip())
        return self._issuer.verify(token, now=self._clock.now())
