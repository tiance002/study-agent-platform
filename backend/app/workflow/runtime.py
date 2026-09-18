"""交互运行时：一次交互的固定顺序。

设计依据：总设计 §6.2、06 号规格 §3。

固定顺序（**不得调换**）：

1. 身份、租户、学习项目鉴权（上下文装配）
2. 输入预检查（大小与硬禁令）
3. 解析成已注册 typed node —— **失败不留预算**
4. admission 检查并建账户树
5. 选择模型档位（L0/L1/L2）
6. Policy Gateway 决策 + 签发 capability token
7. node handler 生成结果（工具调用只能经受控 `ToolInvoker`）
8. 契约、证据充分性与预算验证
9. 外部动作走 intent → dispatch → outcome
10. 记录证据
11. 返回带来源、假设与限制的结果

失败方向：任何一步失败都**不得静默降级**；拒绝路径必须在产生副作用之前结束。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.audit.sink import AuditSink, RiskLevel
from app.budget.ledger import BudgetLedger, Dimension
from app.core.clock import Clock
from app.core.errors import ErrorCode, PlatformError, deny
from app.core.hashing import content_hash
from app.execution.confirmation import ConfirmationStore
from app.execution.outbox import ToolDispatcher
from app.execution.state_machine import ActionStateMachine
from app.knowledge.retrieval import ChunkIndex
from app.learning.evidence import EvidenceLog
from app.learning.projector import MasteryProjection, Projector
from app.policy.gateway import (
    ExecutorCapabilities,
    Obligation,
    PolicyGateway,
    PolicyInput,
)
from app.policy.token import CapabilityToken, TokenIssuer
from app.registry.models import Authority, NodeSpec
from app.registry.registry import Registry
from app.tenancy.context import TenantContext, current, tenant_scope
from app.workflow import tools_impl
from app.workflow.context import NodeContext

MAX_INPUT_CHARS = 8_000

# 本版执行器声明支持的义务。未声明的一律拒绝执行，而不是"尽力而为"。
SUPPORTED_OBLIGATIONS = frozenset(
    {
        str(Obligation.REQUIRE_CONFIRMATION),
        str(Obligation.REQUIRE_TAINT_ENDORSEMENT),
        str(Obligation.REQUIRE_SIGNED_AUDIT_BATCH),
    }
)


@dataclass(frozen=True)
class InteractionRequest:
    request_id: str
    tenant_id: str
    principal_id: str
    learning_project_id: str
    node_id: str
    user_input: str
    params: dict = field(default_factory=dict)
    # 服务端确认记录的 id（由 `POST /projects/{id}/confirmations` 创建）。
    # 客户端只能**引用**一条已存在的确认，不能声明「我确认过了」。
    confirmation_id: str | None = None


@dataclass(frozen=True)
class InteractionResult:
    request_id: str
    status: str          # ok | denied | failed
    node_id: str
    model_tier: str
    decision_id: str | None
    token_id: str | None
    output: dict
    citations: tuple[dict, ...]
    audit_event_ids: tuple[str, ...]
    error: dict | None = None

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "node_id": self.node_id,
            "model_tier": self.model_tier,
            "decision_id": self.decision_id,
            "token_id": self.token_id,
            "output": self.output,
            "citations": list(self.citations),
            "error": self.error,
        }


class ToolInvoker:
    """node handler 唯一的工具入口。

    handler 拿不到 gateway / ledger / dispatcher 本身，因此**结构上不可能**
    绕过策略检查、预算预留或审计写入。
    """

    def __init__(
        self,
        *,
        runtime: "InteractionRuntime",
        token: CapabilityToken,
        node_spec: NodeSpec,
        run_account_id: str,
        tool_context: NodeContext,
        audit_event_ids: list[str],
        principal_id: str,
        confirmation_id: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._token = token
        self._node_spec = node_spec
        self._run_account_id = run_account_id
        self._ctx = tool_context
        self._audit_event_ids = audit_event_ids
        self._principal_id = principal_id
        self._confirmation_id = confirmation_id
        self.calls: list[str] = []

    @property
    def calls_made(self) -> int:
        return len(self.calls)

    def call(self, tool_id: str, params: dict) -> dict:
        """调用一个工具。六道校验全部在产生副作用之前完成。"""
        # 退出条件 1：工具次数上限。到顶即拒绝，不允许"再试一次"。
        if len(self.calls) >= self._node_spec.max_tool_calls:
            raise deny(
                ErrorCode.BUDGET_EXCEEDED,
                f"node {self._node_spec.node_id} 的工具调用已达上限 "
                f"{self._node_spec.max_tool_calls}",
            )
        # 工具必须在该 node 的允许清单内。
        if tool_id not in self._node_spec.allowed_tools:
            raise deny(
                ErrorCode.TOOL_NOT_ALLOWED_FOR_NODE,
                f"node {self._node_spec.node_id} 未声明工具 {tool_id}",
                tool_id=tool_id,
            )

        spec = self._runtime.registry.tool(tool_id)
        runtime = self._runtime
        now = runtime.clock.now()

        # 高影响动作必须由**服务端确认记录**驱动。
        # 这一步要在构造策略输入之前完成 —— 因为「是否已确认」本身就是策略输入的一部分。
        # 请求体里没有任何字段能声明确认，所以这条路径无法被客户端伪造。
        confirmation_recorded = False
        if spec.min_authority >= Authority.A2:
            if self._confirmation_id is None:
                raise deny(
                    ErrorCode.POLICY_DENIED,
                    f"工具 {tool_id} 属于 {spec.min_authority.label} 高影响动作，"
                    f"必须携带服务端确认记录；请求体无法声明确认",
                    tool_id=tool_id,
                )
            runtime.confirmations.consume(
                self._confirmation_id,
                tenant_id=self._token.tenant_id,
                project_id=self._token.project_id,
                principal_id=self._principal_id,
                tool_id=tool_id,
                params=params,
                now=now,
            )
            confirmation_recorded = True

        # 策略决策：每个工具调用一次，输入为不可变快照。
        decision = runtime.gateway.decide(
            PolicyInput(
                request_id=self._token.run_id,
                principal_id=self._principal_id,
                tenant_id=self._token.tenant_id,
                project_id=self._token.project_id,
                node_id=self._node_spec.node_id,
                tool_id=tool_id,
                authority_required=spec.min_authority,
                authority_ceiling=self._node_spec.authority_ceiling,
                params_hash=content_hash(params),
                data_labels=frozenset({"external_content"}) if spec.returns_external_content else frozenset(),
                budget_available=True,
                audit_available=runtime.audit.available,
                confirmation_recorded=confirmation_recorded,
                is_high_impact=spec.min_authority >= Authority.A2,
                needs_egress=bool(spec.network_domains),
            )
        )
        decision.require_allowed()

        # 预算：两个维度必须**一次原子预留**，不能写成两次独立的 reserve。
        # 若写成两次，第二次失败时第一次的预留会残留 —— 账户关不掉、敞口持续累积，
        # 与「预留失败不得留下部分状态」的设计目标冲突。
        # - TOOL_CALLS 是调用计数：成功或失败都算一次，因为调用确实发生了；
        # - CURRENCY_MICROS 是成本：按工具声明的上界预留，结算时用实际值。
        counter, cost = runtime.ledger.batch_reserve(
            self._run_account_id,
            {
                str(Dimension.TOOL_CALLS): 1,
                str(Dimension.CURRENCY_MICROS): max(1, spec.max_cost_units),
            },
        )

        action = runtime.machine.plan(
            tenant_id=self._token.tenant_id,
            project_id=self._token.project_id,
            run_id=self._token.run_id,
            node_instance_id=self._token.node_instance_id,
            # 逻辑动作 id 由内容决定：相同工具 + 相同参数 = 同一个逻辑动作，
            # 重复请求会命中同一动作而不是新建一个。
            logical_action_id=f"{tool_id}:{content_hash(params).split(':')[-1][:16]}",
            tool_id=tool_id,
        )

        impl = self._runtime.tool_impls.get(tool_id)
        if impl is None:
            self._release_quietly(runtime, counter, cost)
            raise deny(
                ErrorCode.TOOL_NOT_REGISTERED,
                f"工具 {tool_id} 没有可用实现",
                tool_id=tool_id,
            )

        try:
            outcome = runtime.dispatcher.dispatch(
                token=self._token,
                decision=decision,
                action=action,
                # 交给 dispatcher 结算的是**成本**预留；计数在本方法内单独处理。
                reservation_id=cost.reservation_id,
                tool=lambda p: impl(self._ctx, p),
                params=params,
                is_high_impact=spec.min_authority >= Authority.A2,
            )
        except BaseException:
            # 前置校验失败：调用没有真正发生，两个预留都释放。
            self._release_quietly(runtime, counter, cost)
            raise

        # 调用确实发生了，计数预留结算为 1（成功与失败都算）。
        runtime.ledger.mark_in_flight(counter.reservation_id)
        runtime.ledger.settle(counter.reservation_id, 1)

        self.calls.append(tool_id)
        self._audit_event_ids.extend(
            [decision.decision_id, outcome.idempotency_key]
        )
        self._ctx.scratch.setdefault("last_decision_id", decision.decision_id)

        if outcome.status.value != "succeeded":
            raise PlatformError(
                code=ErrorCode.RECONCILIATION_REQUIRED
                if outcome.status.value == "unknown"
                else ErrorCode.ILLEGAL_STATE_TRANSITION,
                message=f"工具 {tool_id} 执行未成功：{outcome.error}",
                retryable=outcome.status.value == "unknown",
            )
        return outcome.payload

    @staticmethod
    def _release_quietly(runtime: "InteractionRuntime", *reservations) -> None:
        """释放多个预留，忽略「已不可释放」的错误。

        已派发的调用必须走对账而不是释放，所以这里的静默仅用于前置校验失败的情形。
        """
        for reservation in reservations:
            try:
                runtime.ledger.release(reservation.reservation_id)
            except PlatformError:
                pass


class InteractionRuntime:
    """编排层入口。把各域串成一次可验证的交互。"""

    def __init__(
        self,
        *,
        registry: Registry,
        gateway: PolicyGateway,
        ledger: BudgetLedger,
        audit: AuditSink,
        machine: ActionStateMachine,
        tokens: TokenIssuer,
        projector: Projector,
        clock: Clock,
        chunk_index: ChunkIndex,
        evidence_log: EvidenceLog,
        confirmations: ConfirmationStore,
        capabilities: ExecutorCapabilities | None = None,
    ) -> None:
        self.registry = registry
        self.gateway = gateway
        self.ledger = ledger
        self.audit = audit
        self.machine = machine
        self.tokens = tokens
        self.projector = projector
        self.clock = clock
        self.chunk_index = chunk_index
        self.evidence_log = evidence_log
        self.confirmations = confirmations
        self.capabilities = capabilities or ExecutorCapabilities(SUPPORTED_OBLIGATIONS)
        self.dispatcher = ToolDispatcher(
            registry=registry,
            ledger=ledger,
            audit=audit,
            machine=machine,
            tokens=tokens,
            capabilities=self.capabilities,
            clock=clock,
        )
        self.tool_impls: dict = {
            "query_competency_graph": tools_impl.query_competency_graph,
            "retrieve_project_chunks": tools_impl.retrieve_project_chunks,
            "read_source_span": tools_impl.read_source_span,
            "fetch_external_url": tools_impl.fetch_external_url,
            "run_validator": tools_impl.run_validator,
            "run_in_sandbox": tools_impl.run_in_sandbox,
            "append_project_evidence": tools_impl.append_project_evidence,
        }
        self._processed: dict[str, InteractionResult] = {}

    # ------------------------------------------------------------------ 入口

    def run(self, request: InteractionRequest) -> InteractionResult:
        context = TenantContext(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            project_id=request.learning_project_id,
        )
        with tenant_scope(context):
            return self._run(request, context)

    def _run(self, request: InteractionRequest, context: TenantContext) -> InteractionResult:
        # 0) 请求级幂等：同一 request_id 只处理一次。
        if request.request_id in self._processed:
            return self._processed[request.request_id]

        # 1) 输入预检查
        if len(request.user_input) > MAX_INPUT_CHARS:
            return self._deny(
                request, None, "intake_goal", "L0", "input_too_large",
                "输入超过上限；超大输入需要先分段摄取而不是直接送入模型",
            )

        # 2) 解析 typed node —— 失败不留预算
        try:
            node_spec = self.registry.node(request.node_id)
        except PlatformError as exc:
            return self._fail(request, exc)

        # 3) 账户树（admission）
        run_id = f"run_{content_hash([request.request_id, request.node_id]).split(':')[-1][:16]}"
        run_account_id = self._ensure_budget_tree(request, node_spec, run_id)

        # 4) 签发 capability token
        now = self.clock.now()
        token = self.tokens.issue(
            tenant_id=request.tenant_id,
            project_id=request.learning_project_id,
            run_id=run_id,
            node_instance_id=f"{node_spec.node_id}:1",
            audience=request.principal_id,
            allowed_tools=set(node_spec.allowed_tools),
            policy_version=self.gateway.policy_version,
            registry_version=self.registry.version,
            revocation_epoch=self.dispatcher.revocation_epoch,
            budget_account_id=run_account_id,
            issued_at=now,
            expires_at=now.replace(year=now.year + 1),
        )

        audit_event_ids: list[str] = []
        tool_context = NodeContext(
            chunk_index=self.chunk_index,
            evidence_log=self.evidence_log,
            audit=self.audit,
            clock=self.clock,
            tenant_id=context.tenant_id,
            project_id=context.require_project(),
            principal_id=context.principal_id,
        )
        invoker = ToolInvoker(
            runtime=self,
            token=token,
            node_spec=node_spec,
            run_account_id=run_account_id,
            tool_context=tool_context,
            audit_event_ids=audit_event_ids,
            principal_id=request.principal_id,
            confirmation_id=request.confirmation_id,
        )

        # 5) 执行。所有工具调用都经受控入口。
        try:
            output = _HANDLERS[node_spec.node_id](invoker, request, tool_context)
        except PlatformError as exc:
            self.audit.append(
                "interaction_rejected",
                {"request_id": request.request_id, "code": str(exc.code), "message": exc.message},
                risk=RiskLevel.HIGH if exc.code.value.startswith("POLICY") else RiskLevel.LOW,
                tenant_id=request.tenant_id,
                project_id=request.learning_project_id,
            )
            self._close_run_quietly(run_account_id)
            return self._fail(request, exc, node_spec=node_spec, token=token)

        # 6) 证据充分性检查：证据不足不得升级模型，只能如实说明。
        evidence_sufficiency = "insufficient" if not output.get("citations") else "supported"

        # 回收本次 run 的额度，避免授予额度泄漏到父账户。
        self._close_run_quietly(run_account_id)

        result = InteractionResult(
            request_id=request.request_id,
            status="ok",
            node_id=node_spec.node_id,
            model_tier=str(node_spec.min_tier),
            decision_id=tool_context.scratch.get("last_decision_id"),
            token_id=token.token_id,
            output={**output, "evidence_sufficiency": evidence_sufficiency},
            citations=tuple(output.get("citations", ())),
            audit_event_ids=tuple(audit_event_ids),
        )
        self._processed[request.request_id] = result
        return result

    # ------------------------------------------------------------------ 内部

    def _ensure_budget_tree(
        self, request: InteractionRequest, node_spec: NodeSpec, run_id: str
    ) -> str:
        """建立 Tenant → Project → Run 三级账户。额度按 node 上限下发。"""
        tenant_account = f"acct_tenant_{request.tenant_id}"
        project_account = f"acct_project_{request.learning_project_id}"
        run_account = f"acct_run_{run_id}"

        if tenant_account not in self.ledger._accounts:  # noqa: SLF001 — 内部编排访问
            self.ledger.open_account(
                tenant_account,
                tenant_id=request.tenant_id,
                limits={
                    str(Dimension.CURRENCY_MICROS): 1_000_000_000,
                    str(Dimension.TOKENS): 100_000_000,
                    str(Dimension.STEPS): 100_000,
                    str(Dimension.TOOL_CALLS): 100_000,
                    str(Dimension.SANDBOX_SECONDS): 100_000,
                },
                completion_reserve={str(Dimension.CURRENCY_MICROS): 10_000_000},
            )
        if project_account not in self.ledger._accounts:  # noqa: SLF001
            self.ledger.grant_to_child(
                tenant_account,
                project_account,
                {
                    str(Dimension.CURRENCY_MICROS): 100_000_000,
                    str(Dimension.TOKENS): 10_000_000,
                    str(Dimension.STEPS): 10_000,
                    str(Dimension.TOOL_CALLS): 10_000,
                    str(Dimension.SANDBOX_SECONDS): 10_000,
                },
                tenant_id=request.tenant_id,
                project_id=request.learning_project_id,
            )
        if run_account not in self.ledger._accounts:  # noqa: SLF001
            # run 账户不传租户/项目，从 project 账户继承 —— 靠继承而非重复声明，
            # 避免"某处漏传导致账目脱离隔离范围"。
            self.ledger.grant_to_child(
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

    def _close_run_quietly(self, run_account_id: str) -> None:
        """交互结束后回收 run 账户的额度，避免授予额度泄漏到父账户。

        若仍有未结预留（例如存在 `unknown` 动作），这里只记录、不强行释放 ——
        那属于对账流程，不该被「顺手清理」掩盖过去。
        """
        try:
            self.ledger.close_account(run_account_id)
        except PlatformError as exc:
            account = self.ledger._accounts.get(run_account_id)  # noqa: SLF001 — 运维观测
            self.audit.append(
                "run_account_not_closed",
                {
                    "run_account_id": run_account_id,
                    "code": str(exc.code),
                    "message": exc.message,
                },
                risk=RiskLevel.LOW,
                tenant_id=account.tenant_id if account else None,
                project_id=account.project_id if account else None,
            )

    def _deny(
        self,
        request: InteractionRequest,
        node_spec: NodeSpec | None,
        node_id: str,
        tier: str,
        code: str,
        message: str,
    ) -> InteractionResult:
        return InteractionResult(
            request_id=request.request_id,
            status="denied",
            node_id=node_spec.node_id if node_spec else node_id,
            model_tier=str(node_spec.min_tier) if node_spec else tier,
            decision_id=None,
            token_id=None,
            output={},
            citations=(),
            audit_event_ids=(),
            error={"code": code, "message": message, "retryable": False},
        )

    def _fail(
        self,
        request: InteractionRequest,
        exc: PlatformError,
        *,
        node_spec: NodeSpec | None = None,
        token: CapabilityToken | None = None,
    ) -> InteractionResult:
        return InteractionResult(
            request_id=request.request_id,
            status="denied" if not exc.retryable else "failed",
            node_id=node_spec.node_id if node_spec else request.node_id,
            model_tier=str(node_spec.min_tier) if node_spec else "L0",
            decision_id=None,
            token_id=token.token_id if token else None,
            output={},
            citations=(),
            audit_event_ids=(),
            error=exc.to_payload(),
        )

    def projection(self, *, graph_version: str = "graph/v1") -> MasteryProjection:
        """产出**当前租户与项目**的掌握投影。

        必须先按作用域过滤再投影。否则要么触发跨项目拒绝（报错），
        要么在检查被放宽时产出混合投影 —— 而掌握度是最不能出错的数据。
        """
        context = current()
        project_id = context.require_project()
        events = self.evidence_log.events_scoped(
            tenant_id=context.tenant_id, project_id=project_id
        )
        corrections = self.evidence_log.corrections_scoped(
            event_ids={event.event_id for event in events}
        )
        return self.projector.project_from(
            events=events, corrections=corrections, graph_version=graph_version
        )


# --------------------------------------------------------------------- handler


def _handle_intake_goal(invoker: ToolInvoker, request: InteractionRequest, ctx: NodeContext) -> dict:
    """接收学习目标。本版不调用任何工具（node 声明了空工具集）。"""
    return {
        "goal": request.user_input.strip(),
        "assumptions": ["目标由用户口述，未做可行性判断"],
        "limitations": ["本版未接入云端模型，目标解析为结构化占位输出"],
        "citations": [],
    }


def _handle_diagnose(invoker: ToolInvoker, request: InteractionRequest, ctx: NodeContext) -> dict:
    """诊断先修缺口。只读图谱，不做任何写入。"""
    targets = request.params.get("targets") or []
    graph = invoker.call("query_competency_graph", {"targets": targets})
    closure = graph.get("closure", [])
    # 用户自评：只能减少题量，不能提升掌握状态（01 号规格 §9）。
    self_reported = set(request.params.get("self_reported", []))
    gaps = [c for c in closure if c not in self_reported]
    return {
        "targets": targets,
        "prerequisite_closure": closure,
        "assumed_from_self_report": sorted(self_reported & set(closure)),
        "gaps": gaps,
        "citations": [],
    }


def _handle_retrieve(invoker: ToolInvoker, request: InteractionRequest, ctx: NodeContext) -> dict:
    """检索资料。外部抓取作为可选步骤，失败记录为 unresolved 而不中断。"""
    retrieval = invoker.call("retrieve_project_chunks", {"query": request.user_input, "limit": 3})
    hits = retrieval.get("hits", [])
    citations = [hit["artifact"] for hit in hits]

    unresolved: list[str] = []
    if request.params.get("also_fetch_external"):
        try:
            external = invoker.call("fetch_external_url", {"url": request.params["also_fetch_external"]})
            unresolved.append(f"外部内容已取回但未纳入引用：{external.get('url')}")
        except PlatformError as exc:
            # 不静默降级：明确记录为未完成，并说明原因。
            unresolved.append(f"外部抓取未完成：{exc.code}")

    return {
        "hits": hits,
        "evidence_state": "supported" if hits else "insufficient",
        "unresolved": unresolved,
        "note": "证据不足时不得通过升级模型解决（02 号规格 §4）",
        "citations": citations,
    }


def _handle_validate_and_record(
    invoker: ToolInvoker, request: InteractionRequest, ctx: NodeContext
) -> dict:
    """验证产物并追加学习证据。此处是 A2 写入路径。"""
    artifact = request.params.get("artifact") or {}
    required_keys = request.params.get("required_keys") or []
    validation = invoker.call("run_validator", {"artifact": artifact, "required_keys": required_keys})
    if not validation.get("passed"):
        return {
            "passed": False,
            "missing_keys": validation.get("missing_keys", []),
            "note": "验证未通过，不产生学习证据；失败事实会保留",
            "citations": [],
        }

    # 参数**原样透传用户提交的内容**（无关键会被工具实现忽略）。
    # 这样客户端创建确认时提交的 params 与执行时的参数完全一致，确认绑定才成立。
    # 若这里再拼装字段或补默认值，两边永远对不上，确认会静默失效。
    recorded = invoker.call("append_project_evidence", dict(request.params))
    return {
        "passed": True,
        "evidence_event_id": recorded.get("event_id"),
        "evidence_seq": recorded.get("seq"),
        "citations": [],
    }


_HANDLERS = {
    "intake_goal": _handle_intake_goal,
    "diagnose_prerequisites": _handle_diagnose,
    "retrieve_material": _handle_retrieve,
    "validate_and_record": _handle_validate_and_record,
}
