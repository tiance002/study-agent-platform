"""租户与项目上下文。

设计依据：04 号规格 §2、不变量 #2。

**关键性质：缺少租户/项目上下文的查询必须失败，不能回落为无过滤查询。**

这是最容易在实现里被悄悄放宽的一条 —— 因为"没上下文就全表查"看起来更方便。
所以这里用 ContextVar 强制，并且**没有提供任何绕过接口**。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from app.core.errors import ErrorCode, deny


@dataclass(frozen=True)
class TenantContext:
    """一次操作所属的租户与项目范围。由 L2 边界层注入，业务代码只读。"""

    tenant_id: str
    principal_id: str
    project_id: str | None = None

    def require_project(self) -> str:
        if not self.project_id:
            raise deny(
                ErrorCode.TENANT_CONTEXT_MISSING,
                "该操作要求项目级上下文，但当前只有租户级上下文",
                tenant_id=self.tenant_id,
            )
        return self.project_id


_active: ContextVar[TenantContext | None] = ContextVar("tenant_context", default=None)


@contextmanager
def tenant_scope(context: TenantContext) -> Iterator[TenantContext]:
    """进入租户作用域。使用 ContextVar，并发请求之间互不串扰。"""
    token = _active.set(context)
    try:
        yield context
    finally:
        _active.reset(token)


def current() -> TenantContext:
    """读取当前上下文。缺失即失败 —— 这是不变量 #2 的执行点。"""
    context = _active.get()
    if context is None:
        raise deny(
            ErrorCode.TENANT_CONTEXT_MISSING,
            "缺少租户上下文；任何数据库查询都必须携带租户/项目上下文，"
            "禁止回落为无过滤查询",
        )
    return context


def current_or_none() -> TenantContext | None:
    """只在需要区分「无上下文」与「上下文错误」的边界代码里使用。"""
    return _active.get()
