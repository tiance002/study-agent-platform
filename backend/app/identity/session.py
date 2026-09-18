"""会话令牌：签发、序列化、校验。

设计依据：不变量 #1；04 号规格 §2（上下文注入）。

**为什么用签名令牌而不是请求字段**：请求体里的 `tenant_id` 是客户端**声称**的，
签名令牌里的 `tenant_id` 是服务端**断言**的。前者无法作为授权依据，后者可以。

⚠️ 生产替换点：本适配器是**开发用**的 HMAC 会话令牌。真实部署必须替换为
   OIDC / SAML / 企业 SSO 适配器（`AuthProvider` 接口不变），
   并接入 KMS/Secret Manager 管理签名密钥。
"""

from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256

from app.core.errors import ErrorCode, deny
from app.core.hashing import canonical_json
from app.core.ids import new_id
from app.identity.models import Principal

DEFAULT_TTL = timedelta(hours=8)

# 令牌长度上限。解析要解码并构造对象，让调用方随手递一个 10MB 的字符串进来
# 是不必要的开销，所以先卡长度，再解码。
MAX_TOKEN_BYTES = 8192


def _require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} 缺失或不是非空字符串")
    return value


def _optional_str(data: dict, key: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    return value


def _optional_str_tuple(data: dict, key: str) -> tuple[str, ...]:
    value = data.get(key, ())
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} 必须是字符串序列")
    return tuple(value)


def _require_aware_datetime(data: dict, key: str) -> datetime:
    """取一个**带时区**的时间戳。

    必须带时区：否则后面 `now >= expires_at` 会变成 aware 与 naive 比较，
    抛出的 `TypeError` 同样会逃出 `AUTH_REQUIRED` 的边界、变成 500。
    """
    raw = data.get(key)
    if not isinstance(raw, str):
        raise ValueError(f"{key} 缺失或不是字符串")
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        raise ValueError(f"{key} 必须带时区")
    return value


@dataclass(frozen=True)
class SessionToken:
    """一次认证会话。`tenant_id` 由签发方写入，客户端改不了。"""

    token_id: str
    principal_id: str
    tenant_id: str
    display_name: str
    roles: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    signature: str = ""

    def signing_payload(self) -> dict:
        return {
            "token_id": self.token_id,
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def to_principal(self) -> Principal:
        return Principal(
            principal_id=self.principal_id,
            tenant_id=self.tenant_id,
            display_name=self.display_name,
            roles=self.roles,
        )


class SessionIssuer:
    """签发与校验会话令牌。

    ⚠️ **开发适配器**：生产必须替换为 OIDC/SAML。密钥必须来自 KMS/Secret Manager。
    """

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("会话签名密钥不能为空")
        self._secret = secret

    def _sign(self, token: SessionToken) -> str:
        material = canonical_json(token.signing_payload()).encode("utf-8")
        return hmac.new(self._secret.encode("utf-8"), material, sha256).hexdigest()

    # ------------------------------------------------------------------ 签发

    def issue(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        display_name: str = "",
        roles: tuple[str, ...] = (),
        issued_at: datetime,
        ttl: timedelta = DEFAULT_TTL,
    ) -> SessionToken:
        if not principal_id or not tenant_id:
            raise ValueError("principal_id 与 tenant_id 都不能为空")
        if ttl <= timedelta(0):
            raise ValueError("ttl 必须为正")
        token = SessionToken(
            token_id=new_id("sess"),
            principal_id=principal_id,
            tenant_id=tenant_id,
            display_name=display_name,
            roles=tuple(roles),
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        return replace(token, signature=self._sign(token))

    # ------------------------------------------------------------ 序列化

    def serialize(self, token: SessionToken) -> str:
        """编码为 `Bearer` 可用的紧凑字符串。"""
        body = base64.urlsafe_b64encode(
            canonical_json(token.signing_payload()).encode("utf-8")
        ).decode("ascii")
        return f"{body.rstrip('=')}.{token.signature}"

    def parse(self, raw: str) -> SessionToken:
        """把紧凑字符串解析成会话令牌。

        **整个解析过程都在同一个异常边界里。** 早期版本只包住了 base64 与
        JSON 解码，字段读取和 `datetime.fromisoformat()` 留在了外面 ——
        于是发一个内容为 `{}` 的合法编码令牌会抛 `KeyError`，对外变成 500。

        这不只是"少了个 catch"：攻击者只要构造畸形令牌就能把「认证失败」
        变成「服务器错误」，既污染错误指标，也绕开了统一的错误码契约。

        字段类型与时间有效性也在这里校验，原因同上 —— 任何漏出去的异常
        都会变成 500，而 500 和 401 对攻击者是两种完全不同的信号。
        """
        if len(raw.encode("utf-8", errors="ignore")) > MAX_TOKEN_BYTES:
            raise deny(ErrorCode.AUTH_REQUIRED, "会话令牌超出长度上限")

        try:
            body, signature = raw.rsplit(".", 1)
            padded = body + "=" * (-len(body) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded))
            if not isinstance(data, dict):
                raise ValueError("载荷不是 JSON 对象")

            token = SessionToken(
                token_id=_require_str(data, "token_id"),
                principal_id=_require_str(data, "principal_id"),
                tenant_id=_require_str(data, "tenant_id"),
                display_name=_optional_str(data, "display_name"),
                roles=_optional_str_tuple(data, "roles"),
                issued_at=_require_aware_datetime(data, "issued_at"),
                expires_at=_require_aware_datetime(data, "expires_at"),
                signature=signature,
            )
        except Exception as exc:
            # 格式错、字段错、时间错对外一律同一句话：
            # 不区分原因，免得把「这个令牌哪里不对」当成信息泄露出去。
            raise deny(ErrorCode.AUTH_REQUIRED, "会话令牌格式无效") from exc

        if token.expires_at <= token.issued_at:
            raise deny(ErrorCode.AUTH_REQUIRED, "会话令牌的有效期不合法")
        return token

    # ------------------------------------------------------------------ 校验

    def verify(self, token: SessionToken, *, now: datetime) -> Principal:
        """验签 + 时效。失败一律拒绝，且**不区分**原因（避免泄露信息）。"""
        expected = self._sign(token)
        if not hmac.compare_digest(token.signature, expected):
            raise deny(
                ErrorCode.AUTH_REQUIRED,
                "会话令牌签名无效",
            )
        if now >= token.expires_at:
            raise deny(ErrorCode.AUTH_REQUIRED, "会话令牌已过期")
        return token.to_principal()
