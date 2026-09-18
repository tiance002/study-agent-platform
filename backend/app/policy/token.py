"""capability token：签发、派生、验签。

设计依据：
- 03 号规格 §3 —— token 包含 tenant、project、run、node instance、audience、
  允许工具、参数边界、`policy_version`、`revocation_epoch`、`budget_account_id` 和过期时间。
- 不变量 #4 —— 当前 token 只能缩小能力；扩权必须结束当前授权域并重新评估签发新 token。

实现要点：
- 用 HMAC-SHA256 签名（生产应由 KMS 管理密钥，本版用开发密钥）。
- **「只能收缩」不是靠约定，而是靠 `derive()` 里的三条断言强制**：
  工具集必须为父集子集、过期不得晚于父、run 与 tenant 不得改变。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256

from app.core.errors import ErrorCode, deny
from app.core.hashing import canonical_json, content_hash
from app.core.ids import new_id


@dataclass(frozen=True)
class CapabilityToken:
    """绑定主体、run、node、工具与过期时间的短期凭证。不等于租户权限总表。"""

    token_id: str
    tenant_id: str
    project_id: str
    run_id: str
    node_instance_id: str
    audience: str
    allowed_tools: frozenset[str]
    policy_version: str
    registry_version: str
    revocation_epoch: int
    budget_account_id: str
    issued_at: datetime
    expires_at: datetime
    param_bounds: dict = field(default_factory=dict)
    signature: str = ""

    def signing_payload(self) -> dict:
        """签名覆盖的全部字段。任何字段被改动都会导致验签失败。"""
        return {
            "token_id": self.token_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "node_instance_id": self.node_instance_id,
            "audience": self.audience,
            "allowed_tools": sorted(self.allowed_tools),
            "policy_version": self.policy_version,
            "registry_version": self.registry_version,
            "revocation_epoch": self.revocation_epoch,
            "budget_account_id": self.budget_account_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "param_bounds": self.param_bounds,
        }

    def digest(self) -> str:
        return content_hash(self.signing_payload())

    def holds(self, tool_id: str) -> bool:
        return tool_id in self.allowed_tools


@dataclass
class TokenIssuer:
    """签发与派生 token。生产密钥来自 KMS/Secret Manager。"""

    secret: str

    def _sign(self, token: CapabilityToken) -> str:
        material = canonical_json(token.signing_payload()).encode("utf-8")
        return hmac.new(self.secret.encode("utf-8"), material, sha256).hexdigest()

    def issue(
        self,
        *,
        tenant_id: str,
        project_id: str,
        run_id: str,
        node_instance_id: str,
        audience: str,
        allowed_tools: set[str] | frozenset[str],
        policy_version: str,
        registry_version: str,
        revocation_epoch: int,
        budget_account_id: str,
        issued_at: datetime,
        expires_at: datetime,
        param_bounds: dict | None = None,
    ) -> CapabilityToken:
        if expires_at <= issued_at:
            raise deny(ErrorCode.CAPABILITY_ESCALATION_DENIED, "token 过期时间必须晚于签发时间")
        token = CapabilityToken(
            token_id=new_id("cap"),
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            node_instance_id=node_instance_id,
            audience=audience,
            allowed_tools=frozenset(allowed_tools),
            policy_version=policy_version,
            registry_version=registry_version,
            revocation_epoch=revocation_epoch,
            budget_account_id=budget_account_id,
            issued_at=issued_at,
            expires_at=expires_at,
            param_bounds=dict(param_bounds or {}),
        )
        return replace(token, signature=self._sign(token))

    def derive(
        self,
        parent: CapabilityToken,
        *,
        allowed_tools: set[str] | frozenset[str],
        expires_at: datetime,
        node_instance_id: str | None = None,
        audience: str | None = None,
    ) -> CapabilityToken:
        """派生一个能力不增加的新 token。子任务（ChildRun）也必须走这里。"""
        requested = frozenset(allowed_tools)
        escalated = requested - parent.allowed_tools
        if escalated:
            raise deny(
                ErrorCode.CAPABILITY_ESCALATION_DENIED,
                f"派生请求扩大了能力：{sorted(escalated)}；"
                f"扩权必须结束当前授权域并重新评估",
                escalated=sorted(escalated),
            )
        if expires_at > parent.expires_at:
            raise deny(
                ErrorCode.CAPABILITY_ESCALATION_DENIED,
                "派生 token 的过期时间不得晚于父 token",
                parent_expires_at=parent.expires_at,
                requested=expires_at,
            )
        if expires_at <= parent.issued_at:
            raise deny(ErrorCode.CAPABILITY_ESCALATION_DENIED, "派生 token 的过期时间无效")
        token = replace(
            parent,
            token_id=new_id("cap"),
            allowed_tools=requested,
            expires_at=expires_at,
            node_instance_id=node_instance_id or parent.node_instance_id,
            audience=audience or parent.audience,
            signature="",
        )
        return replace(token, signature=self._sign(token))

    def verify(
        self,
        token: CapabilityToken,
        *,
        now: datetime,
        current_revocation_epoch: int,
    ) -> None:
        """验签 + 时效 + 撤销检查。任何一项不通过即拒绝，不做部分放行。"""
        expected = self._sign(token)
        if not hmac.compare_digest(token.signature, expected):
            raise deny(ErrorCode.CAPABILITY_NOT_HELD, "token 签名不匹配，可能被篡改")
        if now >= token.expires_at:
            raise deny(
                ErrorCode.CAPABILITY_NOT_HELD,
                f"token 已于 {token.expires_at.isoformat()} 过期",
                token_id=token.token_id,
            )
        if current_revocation_epoch > token.revocation_epoch:
            raise deny(
                ErrorCode.CAPABILITY_NOT_HELD,
                "token 已被撤销 epoch 作废",
                token_epoch=token.revocation_epoch,
                current_epoch=current_revocation_epoch,
            )
        if token.registry_version == "":
            raise deny(ErrorCode.CAPABILITY_NOT_HELD, "token 缺少注册表版本，无法归因")
