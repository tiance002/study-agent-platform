"""教学运行的端口（ports）。

`TeachingProvider` 是 L4 能力层与外部模型服务之间**唯一**的边界：
编排层、校验层、持久化层都只认识这个协议；供应商 SDK 只允许出现在
实现它的适配器里。换 provider 或加 provider，不改任何调用方。

## 为什么 `generate` 是同步单次调用

- **没有隐藏重试**：SDK 的自动重试会把"一次调用"变成"N 次计费"，而账本
  只记了一笔 —— 费用事实与调用事实从此对不上。重试是编排层的显式决策
  （复用同一个 durable attempt 标识），不是适配器的私有行为。
- **没有流式**：首版只回传**已校验**的最终答案（02 号规格的边界：
  未校验文本不得冒充已引用结论）。原生 token 流属另设的隔离预览协议。
- **没有异步回调**：结果要么在 `deadline` 前同步拿到，要么返回
  `TIMEOUT`（结果未知）。回调式接口会把"结果在哪"变成分布式状态问题，
  首版不值得。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.teaching.models import ProviderRequest, ProviderResult


@runtime_checkable
class TeachingProvider(Protocol):
    """模型服务的端口。实现必须满足：

    1. `generate` 恰好发起**至多一次**上游调用（不自动重试）；
    2. 返回的 `ProviderResult` 只陈述 provider 侧事实，不做任何校验判断；
    3. 任何异常只在"连结果分类都无法给出"时抛出 —— 能分类的失败
       （超时、拒绝、畸形响应）一律用 `ProviderStatus` 表达。
    """

    def generate(self, request: ProviderRequest) -> ProviderResult: ...
