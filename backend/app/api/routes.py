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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
            "auth": state.auth_mode_label,
            "membership": state.persistence_backend,
            "confirmation": state.persistence_backend,
            "retrieval": "development_adapter",
            "sandbox": "not_implemented",
            "persistence": state.persistence_backend,
            "rls": state.rls_label,
        },
        # 审计 outbox 里尚未投影进链式 sink 的事件数。
        # >0 不是错误（事实已可靠落库），但是必须被消化的积压信号。
        "audit_outbox_pending": (
            state.audit_outbox.pending_count() if state.audit_outbox else 0
        ),
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


@router.post("/projects/{project_id}/retrieval/chunks", response_model=None)
def ingest(request: Request, project_id: str, body: IngestBody) -> dict | JSONResponse:
    """【演示管线夹具】往检索索引塞 chunk。

    ⚠️ 这**不是**产品语义的"资料登记"（那是 `product_routes.register_source`，
    走 SourceRepository 与 0002 的 sources 表）。本端点服务的是检索演示节点
    （retrieve_material 从内存 ChunkIndex 取内容），URL 从 /sources 迁来：
    那个路径现在属于产品资料登记，两个语义不能共用一个 URL。
    第 4 轮 RAG 落地时本夹具与演示节点一并退役。
    """
    from app.api.http_idempotency import idempotent_write

    state = _state(request)
    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        principal = guard.principal
        state.membership.get(principal, project_id)
        context = TenantContext(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            project_id=project_id,
        )
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
        result = {"project_id": project_id, "ingested": created}
        guard.complete(200, result)
        return result


@router.post("/projects/{project_id}/confirmations", response_model=None)
def create_confirmation(
    request: Request, project_id: str, body: ConfirmationBody
) -> dict | JSONResponse:
    """创建服务端确认记录。

    这是「用户点了确认」在服务端的落点。回应包含确认界面必须展示的内容：
    确切工具、权限档位、影响范围、幂等语义、有无网络边界、有效期。

    只有高影响动作（A2 及以上）才需要确认；对低影响动作创建确认会被拒绝 ——
    否则确认会变成一种"随手点掉"的仪式。
    """
    from app.api.http_idempotency import idempotent_write

    state = _state(request)
    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        principal = guard.principal
        state.membership.get(principal, project_id)
        context = TenantContext(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            project_id=project_id,
        )

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

        result = {
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
        guard.complete(200, result)
        return result


@router.post("/projects/{project_id}/interactions", response_model=None)
def interact(
    request: Request, project_id: str, body: InteractionBody
) -> dict | JSONResponse:
    """执行一次交互。node 必须已注册，否则拒绝且不留预算。

    追踪 id **复用中间件绑定的那个**，不在这里另生成：否则响应头、响应体、
    错误体与审计事件会各带一个不同的 id，出问题时无法把它们串起来。

    幂等双层（铁律 27）：``Idempotency-Key`` 头是 HTTP 命令层防重放
    （必填）；请求体里的 ``idempotency_key`` 是 runtime 执行层的业务幂等。
    两层各管各的，不合并。
    """
    from app.api.http_idempotency import idempotent_write

    state = _state(request)
    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        principal = guard.principal
        state.membership.get(principal, project_id)
        context = TenantContext(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            project_id=project_id,
        )
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
        payload = result.to_dict()
        guard.complete(200, payload)
        return payload


@router.get("/projects/{project_id}/mastery")
def mastery(request: Request, project_id: str) -> dict:
    """从全部合法证据重建掌握投影；产品状态不参与计算。"""
    from app.learning.ports import GRAPH_VERSION_V1

    state = _state(request)
    principal, context = _project_scope(request, project_id)
    persisted = state.evidence.events_for(principal, project_id)
    runtime_events = state.evidence_log.events_scoped(
        tenant_id=context.tenant_id, project_id=project_id
    )
    event_ids = {event.event_id for event in (*persisted, *runtime_events)}
    corrections = (
        *state.evidence.corrections_for(principal, project_id, event_ids),
        *state.evidence_log.corrections_scoped(event_ids=event_ids),
    )
    projection = state.projector.project_from(
        events=(*persisted, *runtime_events),
        corrections=corrections,
        graph_version=GRAPH_VERSION_V1,
    )
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
    elif exc.code is ErrorCode.IDEMPOTENCY_VIOLATION:
        # 幂等键被复用于不同内容：409 —— 客户端要换 key，不是重新登录。
        status = 409
        payload = public_error_payload(
            exc.code.value, exc.message, request_id=request_id
        )
    elif exc.code is ErrorCode.IDEMPOTENCY_IN_PROGRESS:
        # 并发重试撞上未完成的占用：409 + Retry-After。
        # 审查发现的缺陷：这里早先复制的还是 IDEMPOTENCY_VIOLATION 分支，
        # IN_PROGRESS 落进默认 403"禁止访问" —— 客户端会以为被拒而放弃
        # 一个本来稍后重试就能成功的请求（铁律 18：两种失败必须可区分）。
        status = 409
        response = JSONResponse(status_code=status, content=payload)
        response.headers["Retry-After"] = "1"
        return response
    elif exc.code is ErrorCode.IDEMPOTENCY_KEY_REQUIRED:
        # 缺 Idempotency-Key：客户端 bug（缺请求头），400 可修复。
        status = 400
        payload = public_error_payload(
            exc.code.value, exc.message, request_id=request_id
        )
    elif exc.code is ErrorCode.PARAMS_INVALID:
        # 参数非法（含 key 超长）：400 —— 可修复的客户端错误，不是 403 禁止。
        status = 400
        payload = public_error_payload(
            exc.code.value, exc.message, request_id=request_id
        )
    elif exc.code is ErrorCode.VERSION_CONFLICT:
        # 乐观锁冲突：409。404 会让客户端误判成权限问题去重新登录；
        # 412 语义上也贴切但极少用 —— 409 是编辑冲突的事实标准。
        status = 409
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
    elif exc.code is ErrorCode.TEACHING_PROVIDER_DISABLED:
        # 教学功能未启用：配置事实，不是权限问题（403 会误导客户端去
        # 检查凭据）也不是故障（5xx 会触发重试风暴）。503 + 稳定码 =
        # "这个功能现在没有，找管理员"。
        status = 503
    elif exc.code is ErrorCode.RATE_LIMITED:
        # 引导端点限流：可重试的 429，带 Retry-After（秒）。
        status = 429
        retry_after = exc.details.get("retry_after_seconds", 0)
        response = JSONResponse(status_code=status, content=payload)
        response.headers["Retry-After"] = str(max(int(retry_after), 0))
        return response
    elif exc.code is ErrorCode.INTERNAL_CONSISTENCY_ERROR:
        # 内部不变量破裂：对外只给通用 500，绝不回显 session_id / 约束名等细节。
        status = 500
        payload = public_error_payload(
            "INTERNAL_ERROR", "服务器内部错误", request_id=request_id
        )
    elif exc.code is ErrorCode.RECONCILIATION_REQUIRED:
        # 409：请求本身没问题，是动作处于「结果未知」，必须先对账。
        # 用 403 等于说「你不被允许」，那是误导；用 5xx 又会被客户端当故障重试，
        # 而盲目重试正是这条错误要阻止的行为。
        status = 409
    return JSONResponse(status_code=status, content=payload)


def _sanitize_utf8(text: str) -> str:
    """把无法编码成 UTF-8 的字符换掉（孤立代理项）。

    这是最后一道兜底。下面只用了 pydantic 的固定话术与字段路径，理论上不含
    请求输入 —— 但"理论上"不该被用来保证一条会把 422 变成 500 的路径。
    """
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def request_validation_response(exc: RequestValidationError) -> JSONResponse:
    """请求**形状**不合法（pydantic 校验失败）的响应。

    与 `error_response` 并列，构成对外错误体的两个出口：一个是"业务拒绝了"
    （`PlatformError`），一个是"请求压根不合法"（`RequestValidationError`）。
    两者共用 `public_error_payload`，形状因此必然一致 —— FastAPI 的默认实现
    返回的是 `{"detail": [...]}`，那是**第二种形状**：客户端得为它单独写一套解析，
    而"新增一个字段只落到一部分出口"的老毛病正是这么来的。

    ⚠️ 这里**绝不回显 `exc.errors()` 的 `input`**。那不是洁癖：JSON 允许
    `"\\ud800"` 这样的孤立代理项转义，解出来是 Python 能持有、却编码不成
    UTF-8 的字符串；把它放进响应体，`JSONResponse` 会在编码阶段抛
    `UnicodeEncodeError` —— 于是一个"应该 422"的请求变成 500，
    而真正的原因（我们没拦非 UTF-8）在日志里完全看不出来。

    只回显**字段位置**与 pydantic 的固定话术：两者都不含请求输入。
    """
    fields: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", ""))
        fields.append(_sanitize_utf8(f"{location}: {message}" if location else message))
    summary = "；".join(fields[:5]) or "无法解析请求"
    if len(fields) > 5:
        summary += f"；…共 {len(fields)} 处"
    return JSONResponse(
        status_code=422,
        content=public_error_payload(
            # 语义是"请求参数不对"。**422 与 400 的差别是层次，不是语义**：
            # 422 = 请求形状（还没进业务层），400 = 业务参数不合法。
            # 两边都用 `PARAMS_INVALID` 是刻意的 —— 客户端按**码**分支，
            # 同一个原因不该有两个码。
            ErrorCode.PARAMS_INVALID.value,
            f"请求参数不符合接口要求：{summary}",
            request_id=current_request_id(),
        ),
    )
