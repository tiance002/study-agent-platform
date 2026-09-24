"""进程内工作流幂等组件：占用、等待与重放的单一实现。

从 `workflow/runtime.py` 抽出的职责：`(tenant_id, idempotency_key)` 占用表、
条件变量、指纹与 claim/complete/release。组件不反向依赖运行器；
把占用结果转换成 `InteractionResult` 是运行器的职责。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash

if TYPE_CHECKING:
    from app.workflow.runtime import InteractionRequest, InteractionResult

#: 等待同键请求完成的上限。超时返回 `IDEMPOTENCY_IN_PROGRESS`（可重试），
#: 而不是无限等待 —— 一个卡住的执行不该把所有同键请求一起拖住。
IDEMPOTENCY_WAIT_SECONDS = 30.0


@dataclass
class IdempotencyEntry:
    """一次幂等请求的**占用**记录。

    三态，比"有没有结果"多一个中间态：

    - `pending`：已被某个请求占用、正在执行。后到的同键请求等待它。
    - `completed`：结果已落定，后到者直接拿重放结果。
    - `released`：占用者失败或抛错后放弃。等待者会被唤醒并**接手执行**，
      而不是干等到超时 —— 否则一次失败会把同键请求一起拖住。

    ⚠️ 没有"缓存失败结果"这一态：失败不缓存，否则参数修好后的重试
    会永远拿到旧的拒绝。
    """

    fingerprint: str
    state: str = "pending"
    result: InteractionResult | None = None

    def mark_completed(self, result: InteractionResult) -> None:
        """结果与状态**一并**设置。

        分成两行写、其中一行被漏掉时，等待者会看到 `state="completed"`
        却读到 `result=None` —— 那正是这个类存在的意义（表达占用进度）被破坏。
        用方法而不是两处赋值，是为了让"两件事必须一起发生"落在代码结构上，
        而不是落在调用方的记忆里。
        """
        self.result = result
        self.state = "completed"

    def mark_released(self) -> None:
        """放弃占用，且**不留结果**。"""
        self.result = None
        self.state = "released"


# claim 的四种结果。运行器据此决定执行 / 重放 / 拒绝 / 可重试失败。
CLAIM_EXECUTE = "execute"
CLAIM_REPLAY = "replay"
CLAIM_VIOLATION = "violation"
CLAIM_IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class IdempotencyClaim:
    """`claim` 的返回值：组件只描述事实，不构造业务响应。"""

    outcome: str
    result: InteractionResult | None = None


class WorkflowIdempotency:
    """幂等占用表：`(tenant_id, idempotency_key)` → 占用记录。

    两点都是审查发现的，且都**不会报错**，只会静默地做错事：

    1) 键必须**按租户划分**。只用客户端字符串做键时，租户 A 用 `common-key`
       成功之后，租户 B 用同名键会收到 `IDEMPOTENCY_VIOLATION` ——
       一个租户能"占住"另一个租户的键，属于跨租户可用性干扰。
       `principal_id` / 项目 / 内容继续进指纹，负责发现同一作用域内的误用。

    2) 必须是**原子占用**，不能"先查缓存、执行完再写"。
       中间隔着整个执行流程，两个并发同键请求会同时看到"未缓存"、
       各自执行一遍。实测（压小 GIL 切换间隔 + handler 做 I/O）可复现。

    ⚠️ 这是**开发适配器**：进程内、重启即失、多 worker 不共享。
    生产实现应由 PostgreSQL 唯一约束保证：先 INSERT
    `(tenant_id, idempotency_key)` 抢占用，冲突时读该行并等待状态推进。
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], IdempotencyEntry] = {}
        self._cond = threading.Condition()

    def claim(
        self,
        key: tuple[str, str],
        *,
        fingerprint: str,
        wait_seconds: float = IDEMPOTENCY_WAIT_SECONDS,
    ) -> IdempotencyClaim:
        """原子占用幂等键。返回 `CLAIM_EXECUTE` 表示"这次请求由你执行"。

        为什么必须是原子占用：读缓存与写结果之间隔着**整个执行流程**
        （策略、预算、工具调用）。分成"先查后写"两步时，两个并发同键请求会
        同时看到"未缓存"并各自执行一遍 —— 实测（压小 GIL 切换间隔、
        让 handler 做 I/O）能稳定复现，两次都返回 `idempotent_replay=false`。

        四种非执行结果：
        - `CLAIM_REPLAY`：已完成 → 携带缓存结果；
        - `CLAIM_VIOLATION`：指纹不同（同一把钥匙开两扇门）；
        - `CLAIM_IN_PROGRESS`：仍在处理且超时（**可重试**，与"键被误用"严格区分）。

        指纹检查必须对**三种状态一致生效**，所以放在状态分派之前。

        改前它排在 "released" 分支之后，而那个分支会直接替换记录 ——
        于是同一个错误在不同状态下有不同结果（实测）：
          占用者失败后复用同键换参数 → 静默接受，正常执行；
          占用者成功后复用同键换参数 → IDEMPOTENCY_VIOLATION。
        客户端据此会得出"key 复用没问题"的结论，而事实只有一半。
        """
        deadline = time.monotonic() + wait_seconds

        with self._cond:
            while True:
                entry = self._entries.get(key)

                if entry is not None and entry.fingerprint != fingerprint:
                    return IdempotencyClaim(CLAIM_VIOLATION)

                if entry is None or entry.state == "released":
                    # 无人占用，或占用者已放弃（失败/拒绝）—— 由本次请求接手。
                    self._entries[key] = IdempotencyEntry(fingerprint=fingerprint)
                    return IdempotencyClaim(CLAIM_EXECUTE)

                if entry.state == "completed":
                    # 用显式检查而不是 `assert`：`-O` 会把断言整个剥掉，
                    # 而那正是这条不变量最需要被守住的时候（生产）。内部
                    # 不变量被破坏是代码缺陷，不该伪装成一次正常的业务拒绝，
                    # 所以这里抛出而不是返回一个结果。
                    if entry.result is None:
                        raise deny(
                            ErrorCode.ILLEGAL_STATE_TRANSITION,
                            "幂等记录为 completed 却没有结果：占用表被非法改动",
                        )
                    return IdempotencyClaim(CLAIM_REPLAY, result=entry.result)

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return IdempotencyClaim(CLAIM_IN_PROGRESS)
                # 释放条件变量再等待，让占用者能推进。
                self._cond.wait(timeout=remaining)

    def complete(self, key: tuple[str, str], result: InteractionResult) -> None:
        """写入结果并唤醒等待者。

        结果与状态标记由 `mark_completed` 一并设置 —— 否则等待者可能被唤醒后
        看到 `completed` 却读到 `None`。
        """
        with self._cond:
            entry = self._entries.get(key)
            if entry is None:
                # 不该发生：占用者一定持有记录。这里显式忽略而不是静默新建，
                # 因为静默新建会把"占用表被谁改过"这件事盖住。
                return
            entry.mark_completed(result)
            self._cond.notify_all()

    def release(self, key: tuple[str, str]) -> None:
        """放弃占用。等在这把键上的请求会被唤醒并接手执行。"""
        with self._cond:
            entry = self._entries.get(key)
            if entry is not None:
                entry.mark_released()
            self._cond.notify_all()

    @staticmethod
    def fingerprint(request: InteractionRequest) -> str:
        """把幂等键绑定到**主体、项目与请求内容**。

        两处绑定都不可省：

        - **绑主体/项目**：否则另一个人猜到（或复用）了同一个 key 就能读到别人的结果
          —— 那等于把幂等缓存变成跨租户读取通道。
        - **绑内容**：否则同一个 key 换个参数复用会静默返回上一次的结果，
          客户端以为自己发了新请求。

        ⚠️ `confirmation_id` 也要绑：它是请求语义的一部分（哪一条授权、覆盖哪些参数）。
        漏掉它的话，「同键 + 换一条确认记录」会被当成重放 ——
        第二条确认**永远不会被消费**，而客户端以为自己的第二次授权生效了。
        这是一次自查发现的缺口：五个字段都绑了，唯独漏了这个。
        """
        return content_hash(
            {
                "tenant_id": request.tenant_id,
                "project_id": request.learning_project_id,
                "principal_id": request.principal_id,
                "node_id": request.node_id,
                "user_input": request.user_input,
                "params": request.params,
                "confirmation_id": request.confirmation_id,
            }
        )
