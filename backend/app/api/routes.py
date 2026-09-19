"""HTTP 接入层。

设计依据：06 号规格 §1（L1 只负责展示与采集，不做业务判断）、不变量 #1。

**本版的关键变化：请求体里不再有 `tenant_id` / `principal_id` / `learning_project_id`。**

身份只能来自两条路径（`api.auth_routes.authenticate_request` 是唯一入口）：

1. **签名 cookie**（主路径）：验签 → CSRF 来源检查 → 回库查撤销；
2. **`Authorization: Bearer`**（兼容适配器）：测试与运维显式携带凭据。

此前这些字段由客户端提供，等于任何调用方都能声称自己是别的租户 ——
那不是"校验不足"，是**根本没有认证**。

三条边界在这里合流，且顺序不可交换：

1. **认证**：解析凭据 → `Principal`（失败即拒绝，不区分原因）
2. **归属**：`membership.get` → 该项目是否属于该主体
3. **上下文**：用前两步的结果组装 `TenantContext`，之后才允许碰数据
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth_routes import authenticate_request
from app.audit.sink import RiskLevel
from app.budget.ledger import Dimension
from app.core.authority import Authority
from app.core.errors import ErrorCode, PlatformError, deny, public_error_payload
from app.core.ids import new_request_id
from app.core.request_context import current_request_id
from app.execution.confirmation import BudgetCeiling
from app.identity.models import Principal
from app.knowledge.retrieval import Chunk
from app.policy.taint import TaintSource
from app.tenancy.context import TenantContext, tenant_scope
from app.workflow.runtime import InteractionRequest

router = APIRouter()

# 幂等键长度上限。见 `InteractionBody.idempotency_key` 的说明。
IDEMPOTENCY_KEY_MAX_CHARS = 200


# --------------------------------------------------------------------- 请求模型
#
# 注意这些模型里**没有** tenant_id / principal_id / learning_project_id。
#
# 三个模型都设 `extra="forbid"`：pydantic 默认会**静默忽略**未知字段。
# 静默忽略在这里是有害的 —— 它让「旧客户端以为自己在设置身份」和
# 「有人正拿 tenant_id 字段做探测」这两种情况看起来都像正常请求。
# 身份只能来自令牌，那类字段出现在请求体里就应该当场被拒，
# 而不是"安全地"丢掉（丢掉本身没错，错的是悄无声息）。


class InteractionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    user_input: str
    params: dict = Field(default_factory=dict)
    # 只能引用一条已存在的服务端确认；不能声明「我确认过了」。
    confirmation_id: str | None = None
    # 幂等键：**由客户端提供**，用于表达"这是我上一次那个请求的重试"。
    # 与追踪 id 分工明确 —— 后者由服务端每请求生成，客户端拿不到稳定值，
    # 所以它做不了幂等键（见 `InteractionRequest` 的注释）。
    #
    # 长度上限不可省：这个值会被长期保留在幂等记录里，不设界就是一个
    # 由客户端控制的内存放大入口。同类字段（`user_input`）早有上限，
    # 这里不该成为例外。超限由 pydantic 直接拒为 422。
    idempotency_key: str | None = Field(default=None, max_length=IDEMPOTENCY_KEY_MAX_CHARS)


class IngestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    chunks: list[str]
    origin: str = str(TaintSource.UPLOADED_SOURCE)


class ConfirmationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str
    params: dict = Field(default_factory=dict)


# --------------------------------------------------------------------- 边界辅助


def _state(request: Request):
    return request.app.state.platform


def _authenticate(request: Request) -> Principal:
    """解析身份。这是唯一能产生 `Principal` 的入口。

    cookie 与 bearer 两条路径的全部逻辑（验签、CSRF、撤销检查）
    收在 `api.auth_routes.authenticate_request` —— 认证不许有两个出口。
    """
    return authenticate_request(request)


def _project_scope(request: Request, project_id: str) -> tuple[Principal, TenantContext]:
    """认证 + 项目归属校验 + 组装租户上下文。

    **所有项目级接口的唯一入口。** 三步都不接受客户端输入的身份或归属：
    身份来自签名凭据（cookie 或 bearer），归属来自服务端成员关系，
    项目来自路径。
    """
    state = _state(request)
    principal = _authenticate(request)
    state.membership.get(principal, project_id)
    context = TenantContext(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        project_id=project_id,
    )
    return principal, context


# --------------------------------------------------------------------- 接口


@router.get("/healthz")
def healthz(request: Request) -> dict:
    """健康检查。**如实**标注哪些组件是开发适配器。"""
    state = _state(request)
    return {
        "status": "ok",
        "registry_version": state.registry.version,
        "policy_version": state.gateway.policy_version,
        "components": {
            "policy_gateway": "available",
            "audit_sink": "available" if state.audit.available else "unavailable",
            "auth": "cookie_session_bearer_compat",
            "membership": "in_memory_adapter",
            "confirmation": "server_side_records",
            "retrieval": "development_adapter",
            "sandbox": "not_implemented",
            "persistence": "in_memory_adapter",
            "rls": "not_implemented",
        },
    }


@router.get("/me")
def me(request: Request) -> dict:
    """返回当前认证身份。用于确认「身份来自令牌」这件事。"""
    return _authenticate(request).to_dict()


@router.get("/registry")
def registry_view(request: Request) -> dict:
    """工具与 node 的边界声明。这是给人看「边界画在哪」的接口。"""
    state = _state(request)
    _authenticate(request)
    return {
        "registry_version": state.registry.version,
        "tools": [
            {
                "tool_id": spec.tool_id,
                "intent_tag": spec.intent_tag,
                "sink_class": str(spec.sink_class),
                "owner_module": spec.owner_module,
                "min_authority": spec.min_authority.label,
                "exclusivity": str(spec.exclusivity),
                "idempotency": str(spec.idempotency),
                "network_domains": list(spec.network_domains),
            }
            for spec in (state.registry.tool(t) for t in state.registry.tool_ids())
        ],
        "nodes": [
            {
                "node_id": spec.node_id,
                "allowed_tools": list(spec.allowed_tools),
                "min_tier": str(spec.min_tier),
                "authority_ceiling": spec.authority_ceiling.label,
                "limits": {
                    "max_tool_calls": spec.max_tool_calls,
                    "max_loops": spec.max_loops,
                    "max_steps": spec.max_steps,
                    "max_tokens": spec.max_tokens,
                },
            }
            for spec in (state.registry.node(n) for n in state.registry.node_ids())
        ],
    }


@router.post("/projects/{project_id}/sources")
def ingest(request: Request, project_id: str, body: IngestBody) -> dict:
    """摄取资料片段。归属由认证上下文与路径共同决定，请求体无权指定。"""
    state = _state(request)
    _, context = _project_scope(request, project_id)
    scoped_project = context.require_project()

    created: list[str] = []
    with tenant_scope(context):
        for index, text in enumerate(body.chunks):
            chunk = Chunk(
                chunk_id=f"{body.source_id}#{index}",
                tenant_id=context.tenant_id,
                learning_project_id=scoped_project,
                source_id=body.source_id,
                span=(index * 100, index * 100 + len(text)),
                text=text,
                origin=TaintSource(body.origin),
            )
            state.chunk_index.add(chunk)
            created.append(chunk.chunk_id)
            state.audit.append(
                "source_ingested",
                {"source_id": body.source_id, "chunk_id": chunk.chunk_id},
                risk=RiskLevel.LOW,
                tenant_id=context.tenant_id,
                project_id=scoped_project,
            )
    return {"project_id": project_id, "ingested": created}


@router.post("/projects/{project_id}/confirmations")
def create_confirmation(request: Request, project_id: str, body: ConfirmationBody) -> dict:
    """创建服务端确认记录。

    这是「用户点了确认」在服务端的落点。回应包含确认界面必须展示的内容：
    确切工具、权限档位、影响范围、幂等语义、有无网络边界、有效期。

    只有高影响动作（A2 及以上）才需要确认；对低影响动作创建确认会被拒绝 ——
    否则确认会变成一种"随手点掉"的仪式。
    """
    state = _state(request)
    principal, context = _project_scope(request, project_id)

    spec = state.registry.tool(body.tool_id)
    if spec.min_authority < Authority.A2:
        raise deny(
            ErrorCode.POLICY_DENIED,
            f"工具 {body.tool_id} 属于 {spec.min_authority.label}，不属于高影响动作，无需确认",
            tool_id=body.tool_id,
        )

    with tenant_scope(context):
        record = state.confirmations.create(
            tenant_id=context.tenant_id,
            project_id=project_id,
            principal_id=principal.principal_id,
            tool_id=body.tool_id,
            params=body.params,
            # 成本上界必须是**具体数值**，不能是一句说明。
            # 用户点确认时同意的是这个数；执行时会拿实际预留与它比对，超出即拒绝
            # （见 ConfirmationStore.consume 的 reserved_budget 参数）。
            # 少了这个数，确认就成了一张金额留空的支票。
            budget_ceiling=BudgetCeiling(
                dimension=str(Dimension.CURRENCY_MICROS),
                amount=max(1, spec.max_cost_units),
                note=f"{spec.tool_id} 单次调用的成本上界（工具声明值）",
            ),
            issued_at=state.clock.now(),
        )

    return {
        **record.to_dict(),
        "tool": {
            "tool_id": spec.tool_id,
            "intent_tag": spec.intent_tag,
            "sink_class": str(spec.sink_class),
            "min_authority": spec.min_authority.label,
            "idempotency": str(spec.idempotency),
            "reversible": spec.idempotency.value != "unsafe_to_retry",
            "network_domains": list(spec.network_domains),
        },
    }


@router.post("/projects/{project_id}/interactions")
def interact(request: Request, project_id: str, body: InteractionBody) -> dict:
    """执行一次交互。node 必须已注册，否则拒绝且不留预算。

    追踪 id **复用中间件绑定的那个**，不在这里另生成：否则响应头、响应体、
    错误体与审计事件会各带一个不同的 id，出问题时无法把它们串起来。
    """
    state = _state(request)
    principal, context = _project_scope(request, project_id)
    result = state.runtime.run(
        InteractionRequest(
            request_id=current_request_id() or new_request_id(),
            tenant_id=context.tenant_id,
            principal_id=principal.principal_id,
            learning_project_id=context.require_project(),
            node_id=body.node_id,
            user_input=body.user_input,
            params=body.params,
            confirmation_id=body.confirmation_id,
            idempotency_key=body.idempotency_key,
        )
    )
    return result.to_dict()


@router.get("/projects/{project_id}/mastery")
def mastery(request: Request, project_id: str) -> dict:
    """读掌握投影。作用域来自认证上下文，不来自查询参数。"""
    state = _state(request)
    _, context = _project_scope(request, project_id)
    with tenant_scope(context):
        projection = state.runtime.projection()
    return projection.to_dict()


@router.get("/projects/{project_id}/audit")
def audit_view(request: Request, project_id: str) -> dict:
    """审计链校验，按认证身份所属租户与项目过滤。

    `chain_valid` 是**全局**属性（哈希链是单条链），返回里显式标注。
    """
    state = _state(request)
    _, context = _project_scope(request, project_id)
    with tenant_scope(context):
        records = state.audit.read_scoped(
            tenant_id=context.tenant_id, project_id=project_id
        )
    return {
        "records": len(records),
        "chain_valid": state.audit.verify_chain(),
        "chain_scope": "global",
        "buffered_low_risk": state.audit.buffered_count,
        "tail": records[-5:],
    }


@router.get("/projects/{project_id}/budget")
def budget_view(request: Request, project_id: str) -> dict:
    """预算与未结敞口，按认证身份所属租户与项目过滤。"""
    state = _state(request)
    _, context = _project_scope(request, project_id)
    with tenant_scope(context):
        reservations = state.ledger.reservations_scoped(
            tenant_id=context.tenant_id, project_id=project_id
        )
        pending = state.machine.actions_needing_reconciliation(
            tenant_id=context.tenant_id, project_id=project_id
        )
    return {
        "open_reservations": [
            {
                "reservation_id": r.reservation_id,
                "account_id": r.account_id,
                "dimension": r.dimension,
                "amount": r.amount,
                "state": str(r.state),
            }
            for r in reservations
        ],
        "needs_reconciliation": [action.to_dict() for action in pending],
    }


def error_response(exc: PlatformError):
    """把平台错误转成稳定响应。

    跨租户/跨项目的拒绝一律以 404 呈现：**不能让人通过状态码差异**
    探测出别的租户有哪些项目。

    ⚠️ 对外字段一律由 `public_error_payload` 生成，这里只替换 `code`/`message`，
    **不手写 dict**。手写过的后果很具体：新增 `next_action` 时它只出现在
    部分响应里，同一个端点在不同失败路径上返回不同形状的错误体。
    """
    from fastapi.responses import JSONResponse

    # 追踪 id 兜底：调用点不知道 id 时用中间件绑定的那个。
    # 此前没有任何 raise 点设置过它，导致每条错误响应的 request_id 都是 null。
    request_id = exc.request_id or current_request_id()

    status = 403
    payload = exc.to_payload()
    payload["request_id"] = request_id
    if exc.code is ErrorCode.AUTH_REQUIRED:
        status = 401
        # 对外不复用内部错误码，也不透露具体失败原因。
        payload = public_error_payload(
            "UNAUTHENTICATED", "未认证或凭据无效", request_id=request_id
        )
    elif exc.code is ErrorCode.INVITATION_INVALID:
        # 邀请兑换失败：401，但**保留** INVITATION_INVALID 码与统一话术 ——
        # 未知 / 已过期 / 已消费共用这一个码与同一句话，
        # 区分原因等于告诉探测者"这个 token 存在过"。
        status = 401
        payload = public_error_payload(
            exc.code.value, exc.message, request_id=request_id
        )
    elif exc.code is ErrorCode.CSRF_DENIED:
        # CSRF 拦截：凭据本身有效，是"来源不对" —— 401 会让客户端去重新登录，
        # 那是误导；403 说的是"这个请求不被接受"。
        status = 403
        payload = public_error_payload(
            exc.code.value, exc.message, request_id=request_id
        )
    elif exc.code in (
        ErrorCode.CROSS_TENANT_DENIED,
        ErrorCode.CROSS_PROJECT_DENIED,
        ErrorCode.TENANT_CONTEXT_MISSING,
    ):
        status = 404
        payload = public_error_payload(
            "NOT_FOUND", "资源不存在", request_id=request_id
        )
    elif exc.code in (ErrorCode.AUDIT_SINK_UNAVAILABLE, ErrorCode.POLICY_GATEWAY_UNAVAILABLE):
        status = 503
    elif exc.code is ErrorCode.RECONCILIATION_REQUIRED:
        # 409：请求本身没问题，是动作处于「结果未知」，必须先对账。
        # 用 403 等于说「你不被允许」，那是误导；用 5xx 又会被客户端当故障重试，
        # 而盲目重试正是这条错误要阻止的行为。
        status = 409
    return JSONResponse(status_code=status, content=payload)
