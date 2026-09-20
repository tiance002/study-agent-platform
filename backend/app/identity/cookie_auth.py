"""签名 Cookie 会话：签发、验签、声明提取。

## 威胁模型先说清楚

cookie 的载荷（session/tenant/principal/时间）是**可见但防篡改**的 ——
base64 不是加密，任何人都能读出里面的字段。它防的是**伪造**：
没有签名密钥就改不了 `principal_id`，改了验签必失败。

所以流程是三段、顺序不可换（总设计 §4 / 冻结稿 §4.1）：

1. **验签**：签名无效 → 拒绝。此前**不信任**载荷里的任何字段；
2. **设上下文**：从**已验签**的声明构造 `Principal` —— 这与 bearer 令牌
   路径同构：身份都是服务端断言的，只是载体不同；
3. **回库查撤销**：`SessionRepository.get_live()` 确认会话未被撤销、未过期。
   这一步是 cookie **可撤销**的根基：声明本身没有吊销状态，
   状态只在数据库里，所以每次认证都要回去看一眼。

**不再把载荷称作"不透明值"** —— 两种说法差一个威胁模型，
错误的说法会让实现者误以为可以省掉验签。
"""

from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from app.core.clock import Clock
from app.core.errors import ErrorCode, deny
from app.core.hashing import canonical_json
from app.identity.models import Principal
from app.product.models import UserSession

#: 会话 cookie 的名字。前端不需要知道内容，只需要原样携带。
SESSION_COOKIE_NAME = "study_session"

# 与 bearer 令牌同一长度上限：解析要先解码再构造对象，
# 让调用方随手递一个 10MB 的字符串进来是不必要的开销。
MAX_COOKIE_BYTES = 8192


@dataclass(frozen=True)
class SessionClaims:
    """一份**已验签**的会话声明。

    它只能从 `CookieAuth.verify()` 产生 —— 那是唯一能把它变成
    `Principal` 的地方。直接从 cookie 字符串拼装声明等于伪造身份。
    """

    session_id: str
    tenant_id: str
    principal_id: str
    issued_at: datetime
    expires_at: datetime

    def to_principal(self) -> Principal:
        return Principal(
            principal_id=self.principal_id,
            tenant_id=self.tenant_id,
        )


class CookieAuth:
    """签发与校验会话 cookie。

    ⚠️ **开发适配器**：签名密钥生产必须来自 KMS / Secret Manager，
    与会话令牌密钥、数据密钥、审计密钥互相分离。
    """

    def __init__(
        self,
        secret: str,
        clock: Clock,
        *,
        previous_secrets: tuple[str, ...] = (),
    ) -> None:
        """
        :param secret: 当前签名密钥，**签发**只用它。
        :param previous_secrets: 轮换窗口内的旧密钥，只用于**验签**。
            轮换密钥后，旧 cookie 在窗口内仍然可用（用户无需重新登录），
            直到自然过期或被集中撤销；新签发的 cookie 一律用新密钥。
            旧密钥验证通过的 cookie 不会自动重签 —— 是否重签由接入层决定，
            本类只回答"这份凭据是否由某个曾信任的密钥签过且未过期"。
        """
        if not secret:
            raise ValueError("cookie 签名密钥不能为空")
        self._secret = secret.encode("utf-8")
        self._verify_secrets = (self._secret,) + tuple(
            old.encode("utf-8") for old in previous_secrets if old
        )
        self._clock = clock

    # ------------------------------------------------------------------ 签发

    def issue(self, session: UserSession) -> str:
        """把一条数据库会话编成 cookie 值。

        声明直接取自会话行 —— cookie 是会话的**引用**，
        不是第二个事实源：两者不一致时以库为准（认证路径每次回库）。
        """
        payload = {
            "session_id": session.session_id,
            "tenant_id": session.tenant_id,
            "principal_id": session.principal_id,
            "issued_at": session.issued_at,
            "expires_at": session.expires_at,
        }
        body = base64.urlsafe_b64encode(
            canonical_json(payload).encode("utf-8")
        ).decode("ascii")
        return f"{body.rstrip('=')}.{self._sign(payload)}"

    def _sign(self, payload: dict) -> str:
        return self._sign_with(self._secret, payload)

    @staticmethod
    def _sign_with(key: bytes, payload: dict) -> str:
        material = canonical_json(payload).encode("utf-8")
        return hmac.new(key, material, sha256).hexdigest()

    # ------------------------------------------------------------------ 校验

    def verify(self, raw: str, *, now: datetime) -> SessionClaims:
        """验签 + 时效。失败**不区分**原因（格式/签名/过期同一种拒绝）。"""
        if len(raw.encode("utf-8", errors="ignore")) > MAX_COOKIE_BYTES:
            raise deny(ErrorCode.AUTH_REQUIRED, "会话凭据无效")

        try:
            body, signature = raw.rsplit(".", 1)
            padded = body + "=" * (-len(body) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded))
            if not isinstance(data, dict):
                raise ValueError("载荷不是 JSON 对象")
            session_id = data["session_id"]
            tenant_id = data["tenant_id"]
            principal_id = data["principal_id"]
            issued_at = datetime.fromisoformat(data["issued_at"])
            expires_at = datetime.fromisoformat(data["expires_at"])
            if not (
                isinstance(session_id, str)
                and isinstance(tenant_id, str)
                and isinstance(principal_id, str)
                and session_id
                and tenant_id
                and principal_id
            ):
                raise ValueError("声明字段缺失或为空")
            if issued_at.tzinfo is None or expires_at.tzinfo is None:
                raise ValueError("时间戳必须带时区")
        except Exception as exc:
            raise deny(ErrorCode.AUTH_REQUIRED, "会话凭据无效") from exc

        # 重建 payload 再验签：只信重算出来的签名，不信 cookie 自带的。
        # 当前密钥与轮换窗口内的旧密钥逐个比对（compare_digest 短路无意义，
        # 但必须用它，不能用 == 比较签名）。
        payload = {
            "session_id": session_id,
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
        signature_ok = any(
            hmac.compare_digest(signature, self._sign_with(key, payload))
            for key in self._verify_secrets
        )
        if not signature_ok:
            raise deny(ErrorCode.AUTH_REQUIRED, "会话凭据无效")
        if now >= expires_at:
            raise deny(ErrorCode.AUTH_REQUIRED, "会话凭据无效")

        return SessionClaims(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )
