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
        try:
            body, signature = raw.rsplit(".", 1)
            padded = body + "=" * (-len(body) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded))
        except Exception as exc:  # 格式错与签名错对外不做区分
            raise deny(ErrorCode.AUTH_REQUIRED, "会话令牌格式无效") from exc
        return SessionToken(
            token_id=data["token_id"],
            principal_id=data["principal_id"],
            tenant_id=data["tenant_id"],
            display_name=data.get("display_name", ""),
            roles=tuple(data.get("roles", ())),
            issued_at=datetime.fromisoformat(data["issued_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            signature=signature,
        )

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
