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

from app.identity.models import Principal
from app.teaching.models import ProviderRequest, ProviderResult, TokenUsage
from app.teaching.runs import Grounding, RunClaim, RunStatus, TeachingEvent, TeachingRun


@runtime_checkable
class TeachingProvider(Protocol):
    """模型服务的端口。实现必须满足：

    1. `generate` 恰好发起**至多一次**上游调用（不自动重试）；
    2. 返回的 `ProviderResult` 只陈述 provider 侧事实，不做任何校验判断；
    3. 任何异常只在"连结果分类都无法给出"时抛出 —— 能分类的失败
       （超时、拒绝、畸形响应）一律用 `ProviderStatus` 表达。
    """

    def generate(self, request: ProviderRequest) -> ProviderResult: ...


class TeachingRunRepository(Protocol):
    """教学运行的持久化端口（内存 / PostgreSQL 各一实现，同一套契约测试）。

    ## 事务边界即方法边界

    `start_run` 在**一个事务**里完成：追加用户消息、写运行行、写预算预留、
    记首条事件 —— 任何一步失败整体回滚（计划任务 2 的原子性要求）。
    `finish_run` / `fail_run` / `require_reconciliation` 同理在 worker
    事务内完成"消息 + 结算 + 事件 + 终态"。

    ## 预算参数为什么由调用方传入

    预算上限（total / max tokens）是**部署配置**（`deployment.py`），
    存储层不读环境 —— "配置在哪读"只有一个出口。存储层只负责
    把上限落成账户行并执行 `budget.ports.assert_capacity` 这份共享不变量。
    """

    def start_run(
        self,
        actor: Principal,
        project_id: str,
        conversation_id: str,
        *,
        run_id: str,
        question: str,
        model_id: str,
        prompt_version: str,
        ranking_version: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        budget_total_micro: int,
        budget_max_input_tokens: int,
        budget_max_output_tokens: int,
    ) -> TeachingRun:
        """原子创建：用户消息 + 运行 + 预算预留 + 首条事件。

        预算不足抛 `BUDGET_EXCEEDED`；会话不可见抛 CROSS_TENANT_DENIED。
        任何写入失败都必须整体回滚（无部分写入）。
        """
        ...

    def get_run(self, actor: Principal, project_id: str, run_id: str) -> TeachingRun:
        """按 id 读运行。不可见（跨项目/跨租户/不存在）统一拒绝。"""
        ...

    def list_events(
        self, actor: Principal, project_id: str, run_id: str, *, after_seq: int = 0
    ) -> tuple[TeachingEvent, ...]:
        """按 seq 升序回放事件（`after_seq` 之后的）。SSE 的持久化来源。"""
        ...

    def budget_snapshot(self, actor: Principal, project_id: str) -> dict:
        """租户级与项目级账户事实（`BudgetAccountFacts.to_dict`）。"""
        ...

    def get_run_limits(self, claim: RunClaim) -> tuple[int, int]:
        """读取创建时冻结的输入/输出上限，不能使用 worker 当前配置替代。"""
        ...

    def attempt_state(self, claim: RunClaim) -> str:
        """返回该运行唯一 attempt 的状态：none/dispatched/completed/failed/unknown。"""
        ...

    # ------------------------------------------------------------ worker 路径

    def claim_run(self, *, worker_id: str, lease_seconds: int) -> RunClaim | None:
        """跨租户认领（worker 角色）。返回 None = 当前没有可做的运行。"""
        ...

    def mark_dispatched(
        self,
        claim: RunClaim,
        *,
        attempt_id: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        request_payload: dict | None = None,
    ) -> None:
        """派发前持久化：写 attempt 行 + 预算 held → in_flight。

        网络调用**不**持有数据库事务或行锁 —— 本方法返回后才开始调用 provider。
        """
        ...

    def record_result(
        self,
        claim: RunClaim,
        *,
        attempt_id: str,
        provider_request_id: str,
        payload: dict,
    ) -> None:
        """把 provider 结果持久化（final commit 之前的存证）。

        final commit 失败后从这份存证重放落库，**绝不重新调用模型**。
        """
        ...

    def finish_run(
        self,
        claim: RunClaim,
        *,
        attempt_id: str,
        answer_message_id: str,
        answer_text: str,
        grounding: Grounding,
        usage: TokenUsage | None,
        citations: tuple[dict, ...] = (),
        citation_rejections: tuple[dict, ...] = (),
    ) -> TeachingRun:
        """原子落定：唯一 assistant 消息 + 费用结算 + 终态事件 + run succeeded。

        `usage` 为 None 时费用敞口保留（预留停在 in_flight），
        但运行本身成功 —— 用户拿到回答与"费用待对账"是两件独立的事。
        """
        ...

    def fail_run(
        self,
        claim: RunClaim,
        *,
        error_code: str,
        safe_detail: str,
        dispatch_happened: bool,
        usage: TokenUsage | None,
    ) -> TeachingRun:
        """确定性失败。

        - 未派发（或可证明未送达）：释放预留，run → failed；
        - 已派发且拿到权威用量：按用量结算，run → failed；
        - 已派发但用量未知：**拒绝**（抛错）—— 调用方必须改走
          `require_reconciliation`，费用敞口不能在这里被无声抹掉。
        """
        ...

    def require_reconciliation(
        self, claim: RunClaim, *, error_code: str, safe_detail: str
    ) -> TeachingRun:
        """结果或费用未知：run → reconciliation_required，敞口原样保留。"""
        ...

    def find_stored_result(self, claim: RunClaim) -> dict | None:
        """读回该运行已持久化的 provider 结果（重放路径）。"""
        ...

    def run_status_for_worker(self, run_id: str) -> tuple[RunStatus, str]:
        """worker 视角读 (status, claim_token)：重放路径判断用。"""
        ...
