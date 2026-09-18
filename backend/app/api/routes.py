"""HTTP 路由。

命名与错误码遵循 `docs/skills/contracts/protocol.md`：
- 读投影与命令接口分离，读接口不改变任何状态；
- 所有错误返回稳定错误码 + `request_id` + 可重试标记。

⚠️ **降级声明**：本版把 `tenant_id` 放在请求体里，**真实系统必须从会话/令牌解析**，
   绝不能让客户端自报租户（那等于没有隔离）。这一点属于「未实现项」。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.audit.sink import RiskLevel
from app.core.errors import ErrorCode, PlatformError
from app.core.ids import new_request_id
from app.knowledge.retrieval import Chunk
from app.policy.taint import TaintSource
from app.tenancy.context import TenantContext, tenant_scope
from app.workflow.runtime import InteractionRequest

router = APIRouter()


class InteractionBody(BaseModel):
    tenant_id: str
    principal_id: str
    learning_project_id: str
    node_id: str
    user_input: str
    params: dict = Field(default_factory=dict)
    # ⚠️ 演示简化：真实系统中确认必须由**服务端确认记录**驱动，
    #    不能由客户端自报，否则 A2/A3 的确认环节形同不存在。
    confirmed_tools: list[str] = Field(default_factory=list)


class IngestBody(BaseModel):
    tenant_id: str
    learning_project_id: str
    source_id: str
    chunks: list[str]
    origin: str = str(TaintSource.UPLOADED_SOURCE)


def _state(request: Request):
    return request.app.state.platform


@router.get("/healthz")
def healthz(request: Request) -> dict:
    state = _state(request)
    return {
        "status": "ok",
        "registry_version": state.registry.version,
        "policy_version": state.gateway.policy_version,
        "components": {
            "policy_gateway": "available",
            "audit_sink": "available" if state.audit.available else "unavailable",
            "retrieval": "development_adapter",
            "sandbox": "not_implemented",
            "persistence": "in_memory_adapter",
        },
    }


@router.get("/registry")
def registry_view(request: Request) -> dict:
    """暴露工具与 node 的边界声明。这是给人看「边界画在哪」的接口。"""
    state = _state(request)
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
    """摄取资料片段。写入必须携带租户上下文。"""
    state = _state(request)
    context = TenantContext(
        tenant_id=body.tenant_id,
        principal_id="ingestion",
        project_id=body.learning_project_id,
    )
    created = []
    with tenant_scope(context):
        for index, text in enumerate(body.chunks):
            chunk = Chunk(
                chunk_id=f"{body.source_id}#{index}",
                tenant_id=body.tenant_id,
                learning_project_id=body.learning_project_id,
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
            )
    return {"project_id": project_id, "ingested": created}


@router.post("/projects/{project_id}/interactions")
def interact(request: Request, project_id: str, body: InteractionBody) -> dict:
    """执行一次交互。node 必须已注册，否则拒绝且不留预算。"""
    state = _state(request)
    result = state.runtime.run(
        InteractionRequest(
            request_id=new_request_id(),
            tenant_id=body.tenant_id,
            principal_id=body.principal_id,
            learning_project_id=body.learning_project_id,
            node_id=body.node_id,
            user_input=body.user_input,
            params=body.params,
            confirmed_tools=tuple(body.confirmed_tools),
        )
    )
    return result.to_dict()


@router.get("/projects/{project_id}/mastery")
def mastery(request: Request, project_id: str, tenant_id: str) -> dict:
    """读掌握投影。只读接口，不改变任何状态。"""
    state = _state(request)
    context = TenantContext(tenant_id=tenant_id, principal_id="reader", project_id=project_id)
    with tenant_scope(context):
        projection = state.runtime.projection()
    return projection.to_dict()


@router.get("/projects/{project_id}/audit")
def audit_view(request: Request, project_id: str) -> dict:
    """审计链校验。应用只有读与追加两种能力，没有删除。"""
    state = _state(request)
    records = state.audit.read_all()
    return {
        "records": len(records),
        "chain_valid": state.audit.verify_chain(),
        "buffered_low_risk": state.audit.buffered_count,
        "tail": records[-5:],
    }


@router.get("/projects/{project_id}/budget")
def budget_view(request: Request, project_id: str) -> dict:
    """预算与未结敞口。`unknown` 动作的敞口必须可见。"""
    state = _state(request)
    return {
        "open_reservations": [
            {
                "reservation_id": r.reservation_id,
                "account_id": r.account_id,
                "dimension": r.dimension,
                "amount": r.amount,
                "state": str(r.state),
            }
            for r in state.ledger.open_reservations()
        ],
        "needs_reconciliation": [
            action.to_dict() for action in state.machine.actions_needing_reconciliation()
        ],
    }


def error_response(exc: PlatformError):
    """把平台错误转成稳定响应。跨租户一律以 404 呈现，不暴露内容存在性。"""
    from fastapi.responses import JSONResponse

    status = 403
    payload = exc.to_payload()
    if exc.code in (
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
    elif exc.code is ErrorCode.AUDIT_SINK_UNAVAILABLE:
        status = 503
    elif exc.code in (ErrorCode.POLICY_GATEWAY_UNAVAILABLE,):
        status = 503
    return JSONResponse(status_code=status, content=payload)
