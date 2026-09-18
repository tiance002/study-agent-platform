"""HTTP 接入层。

设计依据：06 号规格 §1（L1 只负责展示与采集，不做业务判断）、不变量 #1。

**本版的关键变化：请求体里不再有 `tenant_id` / `principal_id` / `learning_project_id`。**

身份只能来自 `Authorization: Bearer <token>`，项目归属来自服务端成员关系。
此前这些字段由客户端提供，等于任何调用方都能声称自己是别的租户 ——
那不是"校验不足"，是**根本没有认证**。

三条边界在这里合流，且顺序不可交换：

1. **认证**：解析令牌 → `Principal`（失败即拒绝，不区分原因）
2. **归属**：`membership.assert_can_access` → 该项目是否属于该主体
3. **上下文**：用前两步的结果组装 `TenantContext`，之后才允许碰数据

⚠️ 仍未实现：令牌签发端点（签发是运维动作，见 `tools/issue_session.py`），
   PostgreSQL + RLS（当前成员关系在内存，未受数据库级保护）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.audit.sink import RiskLevel
from app.core.authority import Authority
from app.core.errors import ErrorCode, PlatformError, deny
from app.core.ids import new_request_id
from app.identity.models import Principal
from app.knowledge.retrieval import Chunk
from app.policy.taint import TaintSource
from app.tenancy.context import TenantContext, tenant_scope
from app.workflow.runtime import InteractionRequest

router = APIRouter()


# --------------------------------------------------------------------- 请求模型
# 注意这些模型里**没有** tenant_id / principal_id / learning_project_id。


class InteractionBody(BaseModel):
    node_id: str
    user_input: str
    params: dict = Field(default_factory=dict)
    # 只能引用一条已存在的服务端确认；不能声明「我确认过了」。
    confirmation_id: str | None = None


class IngestBody(BaseModel):
    source_id: str
    chunks: list[str]
    origin: str = str(TaintSource.UPLOADED_SOURCE)


class ConfirmationBody(BaseModel):
    tool_id: str
    params: dict = Field(default_factory=dict)


# --------------------------------------------------------------------- 边界辅助


def _state(request: Request):
    return request.app.state.platform


def _authenticate(request: Request) -> Principal:
    """解析身份。这是唯一能产生 `Principal` 的入口。"""
    return _state(request).auth.authenticate(request.headers.get("Authorization"))


def _project_scope(request: Request, project_id: str) -> tuple[Principal, TenantContext]:
    """认证 + 项目归属校验 + 组装租户上下文。

    **所有项目级接口的唯一入口。** 三步都不接受客户端输入的身份或归属：
    身份来自签名令牌，归属来自服务端成员关系，项目来自路径。
    """
    state = _state(request)
    principal = _authenticate(request)
    state.membership.assert_can_access(principal, project_id)
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
            "auth": "dev_bearer_session",
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
            budget_ceiling={"source": "node_declared_limit"},
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
    """执行一次交互。node 必须已注册，否则拒绝且不留预算。"""
    state = _state(request)
    principal, context = _project_scope(request, project_id)
    result = state.runtime.run(
        InteractionRequest(
            request_id=new_request_id(),
            tenant_id=context.tenant_id,
            principal_id=principal.principal_id,
            learning_project_id=context.require_project(),
            node_id=body.node_id,
            user_input=body.user_input,
            params=body.params,
            confirmation_id=body.confirmation_id,
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
    """
    from fastapi.responses import JSONResponse

    status = 403
    payload = exc.to_payload()
    if exc.code is ErrorCode.AUTH_REQUIRED:
        status = 401
        payload = {
            "code": "UNAUTHENTICATED",
            "message": "未认证或凭据无效",
            "retryable": False,
            "request_id": exc.request_id,
        }
    elif exc.code in (
        ErrorCode.CROSS_TENANT_DENIED,
        ErrorCode.CROSS_PROJECT_DENIED,
        ErrorCode.TENANT_CONTEXT_MISSING,
    ):
        status = 404
        payload = {
            "code": "NOT_FOUND",
            "message": "资源不存在",
            "retryable": False,
            "request_id": exc.request_id,
        }
    elif exc.code in (ErrorCode.AUDIT_SINK_UNAVAILABLE, ErrorCode.POLICY_GATEWAY_UNAVAILABLE):
        status = 503
    return JSONResponse(status_code=status, content=payload)
