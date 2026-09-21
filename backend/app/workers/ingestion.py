"""摄取 worker：认领 → 读取原文 → 切块 → 落定。

```bash
# 处理一个任务就退出（一次性 / 定时任务 / 容器 job）
python -m app.workers.ingestion --once

# 处理到队列为空再退出
python -m app.workers.ingestion

# 生产常驻：队列为空时等待后继续检查
python -m app.workers.ingestion --daemon
```

## 为什么摄取必须是独立进程

"API 不同步执行摄取重活"是一条可靠性要求，不只是性能要求：HTTP 请求的生命周期
一结束，进程内的后台任务就随之消失（滚动发布、重启、被 OOM 杀掉），
而**已经登记的任务必须仍然有人做**。所以登记（API）与执行（worker）之间靠
PostgreSQL 里的一行 durable 队列连接，而不是靠同一个进程的内存。

代价是"至少一次"投递：worker 可能在写完片段之后、把任务标成功之前被杀掉。
这由两端共同兜住 —— 认领带租约（崩溃后到期可回收），完成写入幂等
（`complete` 对已是 `succeeded` 的任务直接返回，不追加第二套片段）。

## 失败分成两类，处理方式**完全不同**

| 类别 | 判据 | 处理 | 理由 |
|---|---|---|---|
| **确定性失败** | 原文不满足摄取契约，或切块结果违反片段契约 | 立刻 `fail` 进终态 | 同一个输入跑一百次都是同一个结果 —— 留在队列里只会每次租约到期重跑一遍 |
| **不可判定失败** | 数据库连不上、状态迁移冲突、未知异常 | **不写终态**，让租约到期后可回收，进程以非零码退出 | 我们不知道业务有没有提交。替它下结论（写 failed）会盖掉另一个 worker 正在做的事 |

第二类里最典型的例子是 `ILLEGAL_STATE_TRANSITION`：它意味着"我们的租约已经过期，
任务被别的 worker 认领了"。这时若调用 `fail`，就会把一个**别人正在正常处理**的任务
改成失败 —— 那比不处理更糟，因为它把可恢复的状态变成了终态。

## 为什么错误描述是**闭集**而不是 `str(exc)`

`error_detail` 会通过状态接口原样返回给用户。原始异常文本里可能有文件路径、
SQL 片段、内部类型名。所以这里只写死的、按阶段预先想好的话术。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.core.errors import ErrorCode, PlatformError
from app.core.ids import new_id
from app.knowledge.models import IngestionJob
from app.knowledge.processor import DocumentProcessor

if TYPE_CHECKING:  # 只在类型检查时导入：`app.main` 在导入期就会装配一个应用实例
    from app.main import PlatformState

#: 租约时长（秒）。取值要**大于一次切块的最坏耗时**：太短会让仍在运行的 worker
#: 被回收（于是同一个任务被两个进程同时处理），太长则崩溃后恢复得慢。
#: 单版原文上限 1 MiB，纯文本切块是毫秒级的，300 秒留了三个数量级的余量。
DEFAULT_LEASE_SECONDS = 300

#: 常驻模式的空转间隔（秒）。队列本身没有推送机制，这里是轮询。
DEFAULT_IDLE_SLEEP_SECONDS = 2.0

#: 确定性失败里"原文侧"与"片段侧"的安全话术。
#: 分成两句而不是一句：状态接口是排障的唯一入口，用户/运维要能区分
#: "这份资料本身不合格"和"我们的切块器有 bug"。
_DETAIL_DOCUMENT_CONTRACT = (
    "原文不满足摄取契约（编码、长度或媒体类型），未生成片段；"
    "请以 UTF-8 纯文本或 Markdown 重新上传"
)
_DETAIL_CHUNK_CONTRACT = (
    "切块结果未通过片段契约校验，已停止写入；这是服务端问题，需要修复切块器后重新上传"
)

#: 可以判定为"确定性失败"的异常类型。
#:
#: `ValueError` 来自契约层（`core.contracts` / `knowledge.models` / `processor`）；
#: `PlatformError` 只有部分错误码属于此类 —— 见 `_DETERMINISTIC_CODES`。
_DETERMINISTIC = (ValueError, PlatformError)

#: 允许进终态的 `PlatformError` 错误码。
#:
#: ⚠️ `ILLEGAL_STATE_TRANSITION` **刻意不在表内**：它意味着租约已经过期、任务被
#: 别的 worker 认领了。此时写 failed 会把别人正在正常处理的任务打成终态。
#: `VERSION_CONFLICT` 同理 —— 那是"再来一次可能就好了"，不是终局。
_DETERMINISTIC_CODES = frozenset(
    {
        ErrorCode.PARAMS_INVALID,
        ErrorCode.CROSS_PROJECT_DENIED,
        ErrorCode.CROSS_TENANT_DENIED,
    }
)

OutcomeKind = Literal["idle", "succeeded", "failed"]


@dataclass(frozen=True)
class Outcome:
    """一次 `run_once` 的结果。进程退出码**不**由它决定，由是否抛异常决定。"""

    kind: OutcomeKind
    job_id: str = ""
    chunk_count: int = 0
    #: 确定性失败时写入任务终态的错误码（稳定闭集）。
    error_code: str = ""

    def describe(self) -> str:
        if self.kind == "idle":
            return "队列为空，没有可处理的摄取任务"
        if self.kind == "succeeded":
            return f"任务 {self.job_id} 完成：生成 {self.chunk_count} 个片段"
        return f"任务 {self.job_id} 判定为不可恢复的失败（{self.error_code}）"


def _stable_code(exc: BaseException) -> str:
    """把异常翻译成稳定错误码。**不看 `str(exc)`**，只看类型与枚举。

    这是"错误码闭集"的落实点：一旦允许从异常文本里取码，
    错误码就会随上游库的措辞漂移，而客户端按码分支的逻辑会静默失效。
    """
    if isinstance(exc, PlatformError) and exc.code in _DETERMINISTIC_CODES:
        return exc.code.value
    return ErrorCode.PARAMS_INVALID.value


def run_once(
    platform: PlatformState,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    processor: DocumentProcessor | None = None,
) -> Outcome:
    """认领最多一个任务并处理完（或判定为不可恢复）。

    **确定性失败不抛异常**：它已经被写进任务终态，是这条命令的正常结局之一。
    只有"我们不知道该怎么办"的异常才会往上抛，由 `main` 转成非零退出码。

    `processor` 可注入，仅为了测试能钉住切块参数；生产路径用默认参数 +
    平台时钟 —— 片段时间戳必须与同一进程里其它事实用同一个时钟。
    """
    job = platform.ingestion.claim_next(worker_id=worker_id, lease_seconds=lease_seconds)
    if job is None:
        return Outcome(kind="idle")
    return _process(
        platform, job, processor=processor or DocumentProcessor(clock=platform.clock)
    )


def _process(
    platform: PlatformState,
    job: IngestionJob,
    *,
    processor: DocumentProcessor,
) -> Outcome:
    """执行一个已认领的任务。

    阶段（load → parse → commit）分开捕获，是为了让错误码能指出**哪一步**坏了。
    合成一个 `except` 就只能给一句笼统的话，而排障时正是"哪一步"最值钱。
    """
    try:
        document = platform.ingestion.load_document(job)
    except _DETERMINISTIC as exc:
        return _mark_failed(
            platform,
            job,
            code=_stable_code(exc),
            detail=_DETAIL_DOCUMENT_CONTRACT,
        )

    try:
        chunks = processor.parse(document)
    except _DETERMINISTIC:
        # 走到这里意味着切块器自己算错了偏移（`StoredChunk` 的长度约束拒绝构造）。
        # 这不是用户输入的问题 —— 原文在入队时已经过同一套契约校验。
        return _mark_failed(
            platform,
            job,
            code=ErrorCode.INTERNAL_CONSISTENCY_ERROR.value,
            detail=_DETAIL_CHUNK_CONTRACT,
        )

    try:
        platform.ingestion.complete(job, chunks)
    except _DETERMINISTIC as exc:
        return _mark_failed(
            platform,
            job,
            code=_stable_code(exc),
            detail=_DETAIL_DOCUMENT_CONTRACT,
        )
    return Outcome(kind="succeeded", job_id=job.job_id, chunk_count=len(chunks))


def _mark_failed(
    platform: PlatformState, job: IngestionJob, *, code: str, detail: str
) -> Outcome:
    """写终态。

    ⚠️ 这里**不吞**异常：写不进去说明存储层坏了，那属于"不可判定失败"，
    必须让进程以非零码退出，而不是打印一句"已失败"就当作处理完了。
    """
    platform.ingestion.fail(job, error_code=code, safe_detail=detail)
    return Outcome(kind="failed", job_id=job.job_id, error_code=code)


def main(argv: list[str] | None = None, *, platform_factory=None) -> int:
    """命令行入口。返回进程退出码。

    `platform_factory` 可注入：测试要在**同一个**平台实例上跑 API 与 worker，
    否则"API 登记的任务"与"worker 认领的任务"是两份内存状态，
    测试会绿得毫无意义。

    退出码约定：0 = 正常（包括"判定为不可恢复的失败"）；非零 = 我们没处理完，
    需要监管进程介入（此时任务**没有**被写终态，租约到期后可被重新认领）。
    """
    args = _parse_args(argv)
    if platform_factory is None:
        from app.main import build_platform  # 延迟导入：导入期会装配一个应用实例

        platform_factory = build_platform
    platform = platform_factory()
    worker_id = args.worker_id or new_id("wk")

    while True:
        try:
            outcome = run_once(
                platform,
                worker_id=worker_id,
                lease_seconds=args.lease_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — 这里正是要"捕获全部并如实报告"
            # 不可判定失败：不写终态、不吞异常信息。监管进程据退出码重试，
            # 而重试的前提是租约到期 —— 所以退出前必须让日志说清楚发生了什么。
            print(
                f"[ingestion-worker {worker_id}] 未预期的失败，任务未被写终态，"
                f"租约到期后可重新认领：{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        if not (args.daemon and outcome.kind == "idle"):
            print(f"[ingestion-worker {worker_id}] {outcome.describe()}")
        if args.once:
            # `--once`：认领一个就退出（包括没认领到）。
            return 0
        if outcome.kind == "idle":
            if not args.daemon:
                return 0
            time.sleep(args.idle_sleep)
            continue
        time.sleep(args.idle_sleep)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.ingestion",
        description="从 durable 队列认领并处理资料摄取任务。",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="认领并处理最多一个任务后退出（默认行为是处理到队列为空）",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="队列为空时继续等待，供 systemd 等监管进程运行常驻 worker",
    )
    parser.add_argument("--worker-id", default="", help="租约持有者标识；缺省时自动生成")
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=DEFAULT_LEASE_SECONDS,
        help=f"租约时长（秒），默认 {DEFAULT_LEASE_SECONDS}",
    )
    parser.add_argument(
        "--idle-sleep",
        type=float,
        default=DEFAULT_IDLE_SLEEP_SECONDS,
        help=f"空转间隔（秒），默认 {DEFAULT_IDLE_SLEEP_SECONDS}",
    )
    args = parser.parse_args(argv)
    if args.lease_seconds <= 0:
        parser.error("--lease-seconds 必须为正整数")
    if args.idle_sleep < 0:
        parser.error("--idle-sleep 不能为负")
    if args.once and args.daemon:
        parser.error("--once 与 --daemon 不能同时使用")
    return args


if __name__ == "__main__":  # pragma: no cover - 由 `-m` 调用
    raise SystemExit(main())
