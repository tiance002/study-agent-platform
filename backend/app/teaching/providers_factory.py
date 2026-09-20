"""教学 provider 的装配工厂 —— "用哪个 provider"的唯一出口。

组合根（`main.py`）调用这里，而不是自己 import 各适配器：
与 `db.settings` 的角色同理，"provider 怎么选"只在一处定义，
不会出现"某个分支忘了判 disabled"。

- `disabled` → `None`：教学端点显式报"功能未启用"；
- `scripted` → `ScriptedProvider`（测试 / 本机演练注入模拟脚本）；
- `openai` → `OpenAIResponsesProvider`；凭据缺失时由启动配置拒绝，
  不静默退回模拟器。
"""

from __future__ import annotations

import os

from app.deployment import DeploymentSettings
from app.teaching.openai_provider import OpenAIResponsesProvider
from app.teaching.ports import TeachingProvider
from app.teaching.provider import ScriptedProvider


def build_teaching_provider(
    settings: DeploymentSettings, *, script: list | None = None
) -> TeachingProvider | None:
    """按部署配置选择 provider。返回 None 表示教学功能显式关闭。"""
    if settings.teaching_provider == "disabled":
        return None
    if settings.teaching_provider == "scripted":
        # script=None 时给一个"空脚本"模拟器：任何调用都会立刻报
        # "脚本已耗尽" —— 比"悄悄成功"诚实。
        return ScriptedProvider(script or [])
    if settings.teaching_provider == "openai":
        return OpenAIResponsesProvider(
            api_key=os.environ.get("STUDY_PLATFORM_TEACHING_API_KEY", ""),
            base_url=os.environ.get(
                "STUDY_PLATFORM_TEACHING_BASE_URL", "https://api.openai.com/v1"
            ),
        )
    raise RuntimeError(
        f"未知的教学 provider：{settings.teaching_provider!r}"
    )  # pragma: no cover - 解析期闭集校验已挡住
