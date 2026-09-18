"""身份模型。

`Principal` 是**服务端解析出来的**身份，不是客户端声称的身份。
它一旦被构造，就代表「已通过认证」这一事实，因此不允许从请求字段拼装。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    """已认证的主体。身份来自会话令牌，不来自调用方输入。"""

    principal_id: str
    tenant_id: str
    display_name: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)

    def is_admin(self) -> bool:
        return "tenant_admin" in self.roles

    def to_dict(self) -> dict:
        return {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "roles": list(self.roles),
        }
