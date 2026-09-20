"""内部标识生成。

设计依据：04 号规格 §9 —— 日志中的租户标识使用内部不可猜测 ID；
威胁模型中的「ID 猜测」是明确的跨租户读取入口。

约束：禁止使用可枚举的自增整数作为对外可见或写入日志的标识。
"""

from __future__ import annotations

import secrets


def new_id(prefix: str) -> str:
    """生成不可猜测的内部标识，形如 `proj_9xK3p...`。

    使用 128 位熵（`token_urlsafe(16)`），使 ID 猜测无法成为越权读取的入口。
    """
    if not prefix or not prefix.isalnum():
        raise ValueError("prefix 必须是非空字母数字串")
    return f"{prefix}_{secrets.token_urlsafe(16)}"


def new_request_id() -> str:
    """请求级关联标识，用于贯穿 trace、审计与错误响应。"""
    return new_id("req")
