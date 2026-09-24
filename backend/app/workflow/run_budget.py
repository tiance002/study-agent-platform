"""运行预算生命周期：Tenant → Project → Run 三级账户的建立与回收。

从 `workflow/runtime.py` 抽出的职责：`ensure_tree`（admission 建账户树）与
`close_run_quietly`（run 账户回收 + 审计记录）。锁与结算/未知结果语义
保持不变；组件不依赖运行器。
"""

from __future__ import annotations

from app.audit.sink import AuditSink, RiskLevel
from app.budget.ledger import BudgetLedger, Dimension
from app.core.errors import PlatformError
from app.registry.models import NodeSpec


class RunBudgetLifecycle:
    """运行级预算账户的创建、关闭与失败观测。"""

    def __init__(self, *, ledger: BudgetLedger, audit: AuditSink) -> None:
        self._ledger = ledger
        self._audit = audit

    def ensure_tree(
        self,
        *,
        tenant_id: str,
        project_id: str,
        node_spec: NodeSpec,
        run_id: str,
    ) -> str:
        """建立 Tenant → Project → Run 三级账户。额度按 node 上限下发。

        ⚠️ 整段必须在一个临界区里。这里的每一步都是「不存在就创建」——
        典型的 check-then-act。只给账本的单个方法加锁是**不够的**：
        两个线程会同时通过 `not in self.ledger._accounts` 检查，
        第二个在创建时抛「账户已存在」。

        实测就是这么暴露的：加了账本级锁之后并发仍然稳定抛
        `BUDGET_TREE_INVALID`。**单个方法原子 ≠ 一组方法原子。**
        """
        tenant_account = f"acct_tenant_{tenant_id}"
        project_account = f"acct_project_{project_id}"
        run_account = f"acct_run_{run_id}"

        with self._ledger.atomic():
            if not self._ledger.has_account(tenant_account):
                self._ledger.open_account(
                    tenant_account,
                    tenant_id=tenant_id,
                    limits={
                        str(Dimension.CURRENCY_MICROS): 1_000_000_000,
                        str(Dimension.TOKENS): 100_000_000,
                        str(Dimension.STEPS): 100_000,
                        str(Dimension.TOOL_CALLS): 100_000,
                        str(Dimension.SANDBOX_SECONDS): 100_000,
                    },
                    completion_reserve={str(Dimension.CURRENCY_MICROS): 10_000_000},
                )
            if not self._ledger.has_account(project_account):
                self._ledger.grant_to_child(
                    tenant_account,
                    project_account,
                    {
                        str(Dimension.CURRENCY_MICROS): 100_000_000,
                        str(Dimension.TOKENS): 10_000_000,
                        str(Dimension.STEPS): 10_000,
                        str(Dimension.TOOL_CALLS): 10_000,
                        str(Dimension.SANDBOX_SECONDS): 10_000,
                    },
                    tenant_id=tenant_id,
                    project_id=project_id,
                )
            if not self._ledger.has_account(run_account):
                # run 账户不传租户/项目，从 project 账户继承 —— 靠继承而非重复声明，
                # 避免"某处漏传导致账目脱离隔离范围"。
                self._ledger.grant_to_child(
                    project_account,
                    run_account,
                    {
                        str(Dimension.CURRENCY_MICROS): node_spec.max_cost_micros,
                        str(Dimension.TOKENS): node_spec.max_tokens,
                        str(Dimension.STEPS): node_spec.max_steps,
                        str(Dimension.TOOL_CALLS): node_spec.max_tool_calls,
                        str(Dimension.SANDBOX_SECONDS): node_spec.max_sandbox_seconds,
                    },
                )
        return run_account

    def close_run_quietly(self, run_account_id: str, *, request_id: str | None = None) -> None:
        """交互结束后回收 run 账户的额度，避免授予额度泄漏到父账户。

        若仍有未结预留（例如存在 `unknown` 动作），这里只记录、不强行释放 ——
        那属于对账流程，不该被「顺手清理」掩盖过去。
        """
        try:
            self._ledger.close_account(run_account_id)
        except PlatformError as exc:
            account = self._ledger._accounts.get(run_account_id)  # noqa: SLF001 — 运维观测
            self._audit.append(
                "run_account_not_closed",
                {
                    "run_account_id": run_account_id,
                    "code": str(exc.code),
                    "message": exc.message,
                },
                risk=RiskLevel.LOW,
                tenant_id=account.tenant_id if account else None,
                project_id=account.project_id if account else None,
                request_id=request_id,
            )
