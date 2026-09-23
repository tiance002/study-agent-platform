"""教学 worker：认领 → 执行 → 落定（与摄取 worker 同一套进程纪律）。

```bash
# 处理一个运行就退出（一次性 / 定时任务 / 容器 job）
python -m app.workers.teaching --once

# 处理到队列为空再退出
python -m app.workers.teaching

# 生产常驻：队列为空时等待后继续检查
python -m app.workers.teaching --daemon
```

## 失败分两类（与摄取 worker 相同的取舍）

| 类别 | 判据 | 处理 |
|---|---|---|
| **领域失败**（service 返回的结果类别） | provider 明确拒绝 / 未送达 / 答案可用 | 已由 service 写终态，worker 只记录 |
| **不可判定**（围栏失效 / 未知异常） | `ILLEGAL_STATE_TRANSITION`、`CROSS_TENANT_DENIED` 等 | **不写终态**，让租约到期后可回收 |

第二类里最典型的 `ILLEGAL_STATE_TRANSITION` 意味着"我们的租约已过期，
运行被别的 worker 接管了"。此时写 `failed` 会把别人**正在正常处理**的
运行打成失败 —— 比不处理更糟。
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import TYPE_CHECKING, Literal

from app.core.errors import ErrorCode, PlatformError
from app.teaching.service import TeachingService

if TYPE_CHECKING:  # 只在类型检查时导入：`app.main` 在导入期就会装配应用实例
    from app.main import PlatformState

#: 租约时长（秒）。要大于一次"上下文构建 + provider 调用"的最坏耗时：
#: 太短会让仍在调模型的 worker 被回收（两个 worker 同时处理一个运行），
#: 太长则崩溃后恢复慢。模型调用可达数十秒，600 秒留出余量。
DEFAULT_LEASE_SECONDS = 600

#: 常驻模式的空转间隔（秒）。队列没有推送机制，这里是轮询。
DEFAULT_IDLE_SLEEP_SECONDS = 2.0

#: 围栏失效 / 运行不属于我们：不写终态，直接放手。
_STALE_CODES = frozenset({ErrorCode.ILLEGAL_STATE_TRANSITION, ErrorCode.CROSS_TENANT_DENIED})

WorkerOutcome = Literal["idle", "stale", "succeeded", "failed", "reconciliation_required"]


def run_once(
    platform: "PlatformState", *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> WorkerOutcome:
    """认领并执行一个教学运行。没有可做的运行时返回 `idle`。"""
    claim = platform.teaching.claim_run(worker_id=worker_id, lease_seconds=lease_seconds)
    if claim is None:
        return "idle"

    service = TeachingService(
        teaching=platform.teaching,
        knowledge=platform.knowledge,
        products=platform.products,
        provider=platform.teaching_provider,
        model_id=_approved_model(platform),
        prompt_version=_approved_prompt_version(platform),
        max_input_tokens=_max_input_tokens(platform),
        max_output_tokens=_max_output_tokens(platform),
        clock=platform.clock,
        platform_budget=_platform_budget_for_worker(platform),
        platform_provider=_provider_name(platform),
        local_query_rewriter=getattr(platform, "local_query_rewriter", None),
    )
    try:
        # attempt 是 durable 派发事实：没有结果不等于没有派发。
        attempt_state = platform.teaching.attempt_state(claim)
        if attempt_state == "completed":
            return service.replay(claim)
        if attempt_state in {"dispatched", "unknown"}:
            platform.teaching.require_reconciliation(
                claim,
                error_code="WORKER_RECOVERED_UNKNOWN_ATTEMPT",
                safe_detail="上一个 worker 已派发但结果未存证；需要人工对账",
            )
            return "reconciliation_required"
        return service.execute(claim)
    except PlatformError as exc:
        if exc.code in _STALE_CODES:
            # 我们的租约已经失效（被接管 / 运行已终态）。放手，不写终态。
            return "stale"
        raise


def _approved_model(platform: "PlatformState") -> str:
    """批准的模型 id：来自部署配置（服务端决策），不是请求参数。"""
    settings = platform.settings
    if settings is not None and settings.teaching_model:
        return getattr(settings, "teaching_model", "") or "unconfigured-model"
    return "unconfigured-model"  # 内存测试环境无部署配置时的占位


def _approved_prompt_version(platform: "PlatformState") -> str:
    from app.teaching.prompts import SYSTEM_PROMPT_VERSION

    return SYSTEM_PROMPT_VERSION


def _max_input_tokens(platform: "PlatformState") -> int:
    settings = platform.settings
    if settings is not None:
        return getattr(settings, "teaching_max_input_tokens", 8000)
    from app.deployment import DEFAULT_TEACHING_MAX_INPUT_TOKENS

    return DEFAULT_TEACHING_MAX_INPUT_TOKENS


def _max_output_tokens(platform: "PlatformState") -> int:
    settings = platform.settings
    if settings is not None:
        return getattr(settings, "teaching_max_output_tokens", 2000)
    from app.deployment import DEFAULT_TEACHING_MAX_OUTPUT_TOKENS

    return DEFAULT_TEACHING_MAX_OUTPUT_TOKENS


def _provider_name(platform: "PlatformState") -> str:
    """Provider identity is server configuration, never request input."""
    settings = platform.settings
    provider = getattr(settings, "teaching_provider", "") if settings is not None else ""
    if provider:
        return provider
    return "unknown"


def _platform_budget_for_worker(platform: "PlatformState"):
    """Apply the platform paid cap only to real paid-provider dispatches.

    Scripted providers are deterministic local/test adapters and do not create
    an external bill.  They must remain usable with the default zero paid cap;
    the cap is a guard on actual provider spend, not on the teaching workflow.
    """
    if _provider_name(platform) != "openai":
        return None
    return getattr(platform, "platform_paid_budget", None)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。进程级装配：build_platform 一次，循环处理到队列为空。"""
    parser = argparse.ArgumentParser(description="教学运行 worker")
    parser.add_argument("--once", action="store_true", help="处理一个运行就退出")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="队列为空时继续等待，供 systemd 等监管进程运行常驻 worker",
    )
    parser.add_argument("--worker-id", default=f"teaching-worker-{time.time_ns() % 1_000_000}")
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--idle-sleep", type=float, default=DEFAULT_IDLE_SLEEP_SECONDS)
    args = parser.parse_args(argv)
    if args.lease_seconds <= 0:
        parser.error("--lease-seconds 必须为正整数")
    if args.idle_sleep < 0:
        parser.error("--idle-sleep 不能为负")
    if args.once and args.daemon:
        parser.error("--once 与 --daemon 不能同时使用")

    from app.main import build_platform

    platform = build_platform()
    if platform.teaching_provider is None:
        print(
            "教学 provider 未启用（STUDY_PLATFORM_TEACHING_PROVIDER）；没有可处理的运行",
            file=sys.stderr,
        )
        return 0
    while True:
        outcome = run_once(platform, worker_id=args.worker_id, lease_seconds=args.lease_seconds)
        if outcome == "idle":
            if not args.daemon:
                return 0
            time.sleep(args.idle_sleep)
            continue
        print(f"处理完成：{outcome}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.idle_sleep)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
