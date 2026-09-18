"""请求级追踪 id 的上下文。

## 为什么需要它

总设计 §8 要求「所有错误使用稳定错误码、`request_id`、可重试标记」。
但本轮自查发现：`PlatformError.request_id` **从未被任何 raise 点设置过** ——
于是 API 的每一条错误响应里 `request_id` 都是 `null`，401/404/403 全都一样。

字段存在、却永远是空值，正是这个项目反复栽跟头的那一类状态：
它看起来"已经实现了"，于是既不会有人去补，也不会被测试拦住。

## 做法

让**边界层**（HTTP 中间件）生成并绑定 id，错误响应统一从这里取：

- 调用点知道 id 时用自己的（编排层用 `InteractionRequest.request_id`，
  它与审计事件里的 id 同源，可交叉检索）；
- 不知道时由环境提供 —— 保证对外响应永远有值。

## 为什么不接受客户端传来的 `X-Request-Id`

追踪 id 会被写进日志。让客户端决定日志内容等于把日志注入的入口交出去，
而它换来的好处（客户端自定义关联 id）在这个阶段并不需要。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_request_id: ContextVar[str | None] = ContextVar("platform_request_id", default=None)


def bind_request_id(value: str) -> Token:
    """绑定当前请求的追踪 id，返回用于恢复的 token。"""
    return _request_id.set(value)


def reset_request_id(token: Token) -> None:
    _request_id.reset(token)


def current_request_id() -> str | None:
    """当前请求的追踪 id。不在请求上下文中时为 None。"""
    return _request_id.get()
