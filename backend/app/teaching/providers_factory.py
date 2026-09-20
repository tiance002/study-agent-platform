"""教学 provider 的装配工厂 —— "用哪个 provider"的唯一出口。

组合根（`main.py`）调用这里，而不是自己 import 各适配器：
与 `db.settings` 的角色同理，"provider 怎么选"只在一处定义，
不会出现"某个分支忘了判 disabled"。

- `disabled` → `None`：教学端点显式报"功能未启用"；
- `scripted` → `ScriptedProvider`（测试 / 本机演练注入模拟脚本）；
- `openai` → 真实适配器（凭据到位后接入；当前抛"未接入"——
  见 ADR-015，凭据到位前不假装可用）。
"""

from __future__ import annotations

from app.deployment import DeploymentSettings
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
        # 真实适配器在凭据与 SDK 锁定后接入（ADR-015）。
        # 显式失败而不是退回模拟器：生产配置说 openai 却拿到模拟器，
        # 是最危险的静默降级。
        raise RuntimeError(
            "STUDY_PLATFORM_TEACHING_PROVIDER=openai 的适配器尚未接入"
            "（等待可用凭据后按 ADR-015 落地）；当前请使用 disabled 或 scripted"
        )
    raise RuntimeError(
        f"未知的教学 provider：{settings.teaching_provider!r}"
    )  # pragma: no cover - 解析期闭集校验已挡住
