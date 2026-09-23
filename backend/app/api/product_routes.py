"""会话、消息、计划与资料登记的 HTTP 入口（任务 5）。

归属与认证边界与项目路由共享（`authenticate_request` 唯一入口）。

**服务端负责分配一切标识**：conversation_id / message_id / plan_id /
milestone_id / source_id 全部由服务端生成并随响应返回——客户端不提供 id，
提供 id 就等于提供了一种"猜中别人的资源"的方式（会被 404 拦，但没必要开门）。

**资料去重**：`identity_hash` 由服务端从 `acquisition` 规范化 JSON 派生 ——
同一项目内同一获取方式重复登记返回既有记录（幂等成功）。
"""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.hashing import canonical_json
from app.core.ids import new_id
from app.identity.models import Principal
from app.knowledge.acquisition import AcquisitionRequest, SourceCandidate
from app.knowledge.discovery import SearchResult
from app.knowledge.fetch_policy import FetchPolicyError, validate_fetch_target
from app.knowledge.models import MAX_DOCUMENT_BYTES, require_document_content
from app.knowledge.retrieval import RANKING_VERSION
from app.learning.evidence import Direction, EvidenceKind, Validity
from app.learning.ports import COMPONENTS_V1, TaskAssessment
from app.product.models import (
    LearningPlan,
    LearningTask,
    MessageRole,
    Milestone,
    PlanBundle,
    PlanStatus,
    TaskStatus,
)

router = APIRouter()

CONTENT_MAX_CHARS = 32_000
TITLE_MAX_CHARS = 200
GOAL_MAX_CHARS = 2000
# Provider calls have a bounded per-account rate; this is independent of the
# platform's monthly monetary budget, which remains uncapped.
SOURCE_SEARCH_RATE_LIMIT = 20
SOURCE_SEARCH_WINDOW_SECONDS = 600
#: 资料标题上限。比会话标题宽松：文献标题常带编号与副标题，200 字容易不够。
DOCUMENT_TITLE_MAX_CHARS = 300
#: 查询串上限。单次检索的工作量 ≈ 查询项数 × 项目内片段数，两头都要有界。
QUERY_MAX_CHARS = 2000


def _state(request: Request):
    return request.app.state.platform


def _actor(request: Request) -> Principal:
    from app.api.auth_routes import authenticate_request

    return authenticate_request(request)


# ---------------------------------------------------------------- 请求模型


class ConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=TITLE_MAX_CHARS)


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = Field(min_length=1, max_length=CONTENT_MAX_CHARS)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)


class MilestoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tasks: list[TaskInput] = Field(default_factory=list)


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=GOAL_MAX_CHARS)
    milestones: list[MilestoneInput] = Field(min_length=1)


class TaskTransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Literal 让非法状态名在请求校验层就 422，不进业务层。
    expected_status: Literal["pending", "in_progress", "done", "skipped"]
    next_status: Literal["pending", "in_progress", "done", "skipped"]


class SourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    media_type: str = Field(default="", max_length=100)
    acquisition: dict = Field(default_factory=dict)


class SourceContentBody(BaseModel):
    """上传一版原文并把摄取任务入队。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=DOCUMENT_TITLE_MAX_CHARS)
    #: ⚠️ 这里的 `max_length` 是**字符**上限，只是字节上限的**粗筛**：
    #: UTF-8 每个字符至少一字节，所以字符数 ≤ 字节数 —— 超过字节上限的输入
    #: 必然也超过字符上限，粗筛不会漏（它的作用是不把巨量输入解码进内存）。
    #: 反方向不成立（中文一字三字节），因此真正的判定按字节做，
    #: 由下面的校验器调用 `require_document_content` —— **同一个出口**，
    #: 否则 40 万字中文能过应用层校验、却在数据库 CHECK 上炸成 500。
    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_BYTES)
    media_type: Literal["text/plain", "text/markdown"]
    language: str = Field(default="zh", pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]+)*$")

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        return require_document_content(value)


class CandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=300)
    snippet: str = Field(default="", max_length=2000)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        try:
            # Candidate creation is intentionally DNS-free: the worker repeats
            # real DNS and public-address checks immediately before connecting.
            target = validate_fetch_target(
                value,
                resolver=lambda _hostname, _port: ("8.8.8.8",),
            )
        except FetchPolicyError as exc:
            raise ValueError("资料来源 URL 不符合安全策略") from exc
        return target.url


class AcquisitionSelectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    media_type: Literal["text/plain", "text/markdown"]
    language: str = Field(default="zh", pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]+)*$")


class SourceSearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=8, ge=1, le=10)


class DiagnosisBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experience_level: Literal["beginner", "familiar", "experienced"]
    weekly_hours: int = Field(ge=1, le=40)
    preferred_style: Literal["reading", "practice", "mixed"]


class GeneratePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["self_report"]
    content: str = Field(min_length=1, max_length=20_000)


# ---------------------------------------------------------------- 会话与消息


@router.post("/projects/{project_id}/conversations", status_code=201, response_model=None)
def create_conversation(request: Request, project_id: str, body: ConversationBody) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        conversation = state.products.create_conversation(
            guard.principal, project_id, conversation_id=new_id("conv"), title=body.title
        )
        result = conversation.to_dict()
        guard.complete(201, result)
        return result


@router.get("/projects/{project_id}/conversations")
def list_conversations(request: Request, project_id: str) -> dict:
    state = _state(request)
    rows = state.products.list_conversations(_actor(request), project_id)
    return {"conversations": [c.to_dict() for c in rows]}


@router.post(
    "/projects/{project_id}/conversations/{conversation_id}/messages",
    status_code=201,
    response_model=None,
)
def append_message(
    request: Request, project_id: str, conversation_id: str, body: MessageBody
) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        message = state.products.append_message(
            guard.principal,
            project_id,
            conversation_id,
            message_id=new_id("msg"),
            role=body.role,
            content=body.content,
        )
        result = message.to_dict()
        guard.complete(201, result)
        return result


@router.get("/projects/{project_id}/conversations/{conversation_id}/messages")
def list_messages(request: Request, project_id: str, conversation_id: str) -> dict:
    state = _state(request)
    rows = state.products.list_messages(_actor(request), project_id, conversation_id)
    return {"messages": [m.to_dict() for m in rows]}


# ---------------------------------------------------------------- 计划


def _build_bundle(actor: Principal, project_id: str, body: PlanBody, *, now) -> PlanBundle:
    """把用户输入翻译成**完整契约**：所有 id 服务端生成（任务 1 端口约定：
    写入方法接收已生成的 id）。version 由仓储分配，这里填占位值 0……
    不行，契约要求正整数 —— 填 1，replace 会忽略它。"""
    plan_id = new_id("plan")
    milestones: list[Milestone] = []
    tasks: list[LearningTask] = []
    for order, milestone_input in enumerate(body.milestones):
        milestone_id = new_id("mile")
        milestones.append(
            Milestone(
                milestone_id=milestone_id,
                tenant_id=actor.tenant_id,
                project_id=project_id,
                plan_id=plan_id,
                order_index=order,
                title=milestone_input.title,
                description=milestone_input.description,
            )
        )
        for task_order, task_input in enumerate(milestone_input.tasks):
            tasks.append(
                LearningTask(
                    task_id=new_id("task"),
                    tenant_id=actor.tenant_id,
                    project_id=project_id,
                    milestone_id=milestone_id,
                    order_index=task_order,
                    title=task_input.title,
                    status=TaskStatus.PENDING,
                )
            )
    return PlanBundle(
        plan=LearningPlan(
            plan_id=plan_id,
            tenant_id=actor.tenant_id,
            project_id=project_id,
            version=1,  # 占位：replace 的契约是版本由实现分配
            goal=body.goal,
            status=PlanStatus.ACTIVE,
            created_at=now,
        ),
        milestones=tuple(milestones),
        tasks=tuple(tasks),
    )


@router.put("/projects/{project_id}/plan", response_model=None)
def replace_plan(request: Request, project_id: str, body: PlanBody) -> dict | JSONResponse:
    """整版替换：返回实现分配版本后的完整 bundle。"""
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        bundle = state.products.replace_plan(
            guard.principal,
            project_id,
            _build_bundle(guard.principal, project_id, body, now=state.clock.now()),
        )
        result = {
            "plan": bundle.plan.to_dict(),
            "milestones": [m.to_dict() for m in bundle.milestones],
            "tasks": [t.to_dict() for t in bundle.tasks],
        }
        guard.complete(200, result)
        return result


@router.get("/projects/{project_id}/plan")
def current_plan(request: Request, project_id: str):
    state = _state(request)
    bundle = state.products.current_plan(_actor(request), project_id)
    if bundle is None:
        # 没有计划不是错误 —— 204 说明"还没有"，与"找不到"（404）分开。
        return Response(status_code=204)
    return {
        "plan": bundle.plan.to_dict(),
        "milestones": [m.to_dict() for m in bundle.milestones],
        "tasks": [t.to_dict() for t in bundle.tasks],
    }


@router.get("/projects/{project_id}/plan/history")
def plan_history(request: Request, project_id: str) -> dict:
    state = _state(request)
    rows = state.products.plan_history(_actor(request), project_id)
    return {"plans": [p.to_dict() for p in rows]}


# ---------------------------------------------------------------- 资料


def _identity_hash(acquisition: dict) -> str:
    """从获取方式派生稳定标识。**由服务端计算**：客户端声称的"同一资料"
    不算数，规范化 JSON 的哈希才算——同一 acquisition 必得同一哈希。"""
    digest = sha256(canonical_json(acquisition).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _candidate_domain(url: str) -> str:
    hostname = urlsplit(url).hostname
    if hostname is None:  # CandidateBody has already validated this.
        raise ValueError("资料来源 URL 缺少主机名")
    return hostname.rstrip(".").encode("idna").decode("ascii").lower()


@router.post("/projects/{project_id}/source-candidates", status_code=201, response_model=None)
def create_source_candidate(request: Request, project_id: str, body: CandidateBody) -> dict | JSONResponse:
    """登记一个待用户确认的 URL 候选；这里不访问外网。"""
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        now = state.clock.now()
        candidate = SourceCandidate(
            candidate_id=new_id("cand"),
            tenant_id=guard.principal.tenant_id,
            project_id=project_id,
            url=body.url,
            title=body.title,
            snippet=body.snippet,
            source_domain=_candidate_domain(body.url),
            discovered_at=now,
            expires_at=now + timedelta(days=7),
        )
        stored = state.acquisition.create_candidate(guard.principal, project_id, candidate)
        result = stored.to_dict()
        guard.complete(201, result)
        return result


@router.get("/projects/{project_id}/source-candidates")
def list_source_candidates(request: Request, project_id: str) -> dict:
    state = _state(request)
    rows = state.acquisition.list_candidates(_actor(request), project_id)
    return {"candidates": [row.to_dict() for row in rows]}


@router.post("/projects/{project_id}/source-search", response_model=None)
def search_source_candidates(
    request: Request, project_id: str, body: SourceSearchBody
) -> dict | JSONResponse:
    """Discover bounded metadata candidates and persist them in project scope."""
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        provider = state.source_search_provider
        from app.core.errors import ErrorCode, PlatformError, deny

        if provider is None:
            raise deny(ErrorCode.SOURCE_SEARCH_DISABLED, "资料搜索尚未配置")

        principal = guard.principal
        rate_key = (
            "source_search:"
            + sha256(f"{principal.tenant_id}\0{principal.principal_id}".encode("utf-8")).hexdigest()
        )
        decision = state.rate_limiter.register(
            rate_key,
            now=state.clock.now(),
            limit=SOURCE_SEARCH_RATE_LIMIT,
            window_seconds=SOURCE_SEARCH_WINDOW_SECONDS,
        )
        if not decision.allowed:
            raise PlatformError(
                code=ErrorCode.RATE_LIMITED,
                message="资料搜索过于频繁，请稍后再试",
                retryable=True,
                details={"retry_after_seconds": decision.retry_after_seconds},
            )
        try:
            results: tuple[SearchResult, ...] = provider.search(body.query, limit=body.limit)
        except (RuntimeError, ValueError) as exc:
            from app.core.errors import ErrorCode, deny

            del exc
            error = deny(
                ErrorCode.SOURCE_SEARCH_UNAVAILABLE,
                "搜索结果暂时无法确认；再次搜索会发起新请求，可能产生额外费用",
            )
            payload = error.to_payload()
            guard.complete(503, payload)
            return JSONResponse(status_code=503, content=payload)

        now = state.clock.now()
        candidates: list[SourceCandidate] = []
        seen_urls: set[str] = set()
        for result in results:
            try:
                safe_url = CandidateBody(url=result.url, title=result.title, snippet=result.snippet).url
                if safe_url in seen_urls:
                    continue
                seen_urls.add(safe_url)
                candidate = SourceCandidate(
                    candidate_id=new_id("cand"),
                    tenant_id=guard.principal.tenant_id,
                    project_id=project_id,
                    url=safe_url,
                    title=result.title[:300],
                    snippet=result.snippet[:2000],
                    source_domain=_candidate_domain(safe_url),
                    discovered_at=now,
                    expires_at=now + timedelta(days=7),
                )
                candidates.append(state.acquisition.create_candidate(guard.principal, project_id, candidate))
            except (ValueError, FetchPolicyError):
                continue
        payload = {"candidates": [candidate.to_dict() for candidate in candidates]}
        guard.complete(200, payload)
        return payload


@router.post(
    "/projects/{project_id}/source-candidates/{candidate_id}/select",
    status_code=202,
    response_model=None,
)
def select_source_candidate(
    request: Request,
    project_id: str,
    candidate_id: str,
    body: AcquisitionSelectBody,
) -> dict | JSONResponse:
    """显式授权候选后创建 durable 下载任务；HTTP 请求不执行网络访问。"""
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        candidate = state.acquisition.get_candidate(guard.principal, project_id, candidate_id)
        source = state.products.register_source(
            guard.principal,
            project_id,
            source_id=new_id("src"),
            display_name=body.display_name,
            media_type=body.media_type,
            identity_hash=_identity_hash({"kind": "web", "url": candidate.url}),
            acquisition={"kind": "web", "url": candidate.url},
        )
        key = request.headers.get("Idempotency-Key", "").strip() or new_id("acq-key")
        queued = state.acquisition.select(
            guard.principal,
            project_id,
            AcquisitionRequest(
                acquisition_id=new_id("acq"),
                tenant_id=guard.principal.tenant_id,
                project_id=project_id,
                source_id=source.source_id,
                candidate_id=candidate.candidate_id,
                requested_by=guard.principal.principal_id,
                url=candidate.url,
                title=candidate.title,
                media_type=body.media_type,
                language=body.language,
                idempotency_key=key,
                requested_at=state.clock.now(),
            ),
        )
        result = {
            "candidate": state.acquisition.get_candidate(guard.principal, project_id, candidate_id).to_dict(),
            "source": source.to_dict(),
            "acquisition": queued.to_dict(),
        }
        guard.complete(202, result)
        return result


@router.get("/projects/{project_id}/acquisition-jobs/{acquisition_id}")
def get_acquisition_job(request: Request, project_id: str, acquisition_id: str) -> dict:
    state = _state(request)
    job = state.acquisition.get_job(_actor(request), project_id, acquisition_id)
    return job.to_dict()


@router.post("/projects/{project_id}/sources", status_code=201, response_model=None)
def register_source(request: Request, project_id: str, body: SourceBody) -> JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        record = state.products.register_source(
            guard.principal,
            project_id,
            source_id=new_id("src"),
            display_name=body.display_name,
            media_type=body.media_type,
            identity_hash=_identity_hash(body.acquisition),
            acquisition=body.acquisition,
        )
        guard.complete(201, record.to_dict())
        return JSONResponse(status_code=201, content=record.to_dict())


@router.get("/projects/{project_id}/sources")
def list_sources(request: Request, project_id: str) -> dict:
    state = _state(request)
    rows = state.products.list_sources(_actor(request), project_id)
    return {"sources": [s.to_dict() for s in rows]}


@router.post(
    "/projects/{project_id}/sources/{source_id}/content",
    status_code=202,
    response_model=None,
)
def upload_source_content(
    request: Request, project_id: str, source_id: str, body: SourceContentBody
) -> dict | JSONResponse:
    """登记一版原文并入队一个摄取任务。**返回 202，请求内不切块。**

    切块是 worker 的重活，写在请求里的话：一次 1 MiB 的 Markdown 解析会占住
    一个 web worker 若干秒，客户端超时重试又压一份进来 —— 而重试本该是安全的。
    真正的进度看 `GET /projects/{project_id}/ingestion-jobs/{job_id}`。

    原文与任务在**一次调用**里写入（`enqueue` 的契约）：拆成两次会留下
    "原文在库里、没人处理"的半完成状态，而它看起来完全合法 ——
    用户看到的是"上传成功但永远搜不到"。
    """
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        document, job = state.ingestion.enqueue(
            guard.principal,
            project_id,
            source_id,
            document_id=new_id("doc"),
            job_id=new_id("job"),
            title=body.title,
            content=body.content,
            media_type=body.media_type,
            language=body.language,
        )
        result = {"document": document.to_dict(), "job": job.to_dict()}
        guard.complete(202, result)
        return result


@router.get("/projects/{project_id}/ingestion-jobs/{job_id}")
def get_ingestion_job(request: Request, project_id: str, job_id: str) -> dict:
    """摄取任务的稳定状态。只含状态、尝试次数、安全错误码与时间戳。

    **不含原文与片段**：这个端点会被客户端轮询，把原文放进来就是一个
    客户端可控的响应体放大入口（同一条理由让 `SourceDocument.to_dict()`
    不含 `content`）。原文回读走引用（`source_id + span + content_hash`）。
    """
    state = _state(request)
    job = state.ingestion.get_job(_actor(request), project_id, job_id)
    return job.to_dict()


@router.get("/projects/{project_id}/ingestion-jobs")
def list_ingestion_jobs(request: Request, project_id: str) -> dict:
    state = _state(request)
    jobs = state.ingestion.list_jobs(_actor(request), project_id)
    return {"jobs": [job.to_dict() for job in jobs]}


# ------------------------------------------------------------- 检索与引用


class KnowledgeSearchBody(BaseModel):
    """检索请求。

    `query` 上下限与 `limit` 上限都是**硬边界**，不是建议值：检索要在
    项目内全部片段上打分，而这两个参数都直接乘进工作量 ——
    不设界就是一个客户端可控的放大入口。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=QUERY_MAX_CHARS)
    limit: int = Field(default=10, ge=1, le=20)


@router.post("/projects/{project_id}/knowledge/search")
def search_knowledge(request: Request, project_id: str, body: KnowledgeSearchBody) -> dict:
    """项目内中文关键词检索。返回候选、谱系引用与**保守的**证据判定。

    ⚠️ 这里的 POST 是"用请求体传查询条件"，不是命令：它**没有副作用**，
    因此**不要求 `Idempotency-Key`**。给查询套上幂等守卫不会保护任何东西
    （没有可重复的副作用），只会让客户端多一个必填头 ——
    而"必填但无意义"的字段迟早会被一路照抄到真正需要它的地方，那时它已经不表示什么了。

    `evidence` 与 `retrieval_health` 是两个**不同的问题**，不要合读：
    前者答"核心结论有没有足够证据"，后者答"这次检索有没有按预期跑完"。
    本轮没有冻结的核心结论标注集，所以前者必然是
    `insufficient` + `MISSING_SUPPORT` —— 这是正确结果，不是待修的缺陷。
    """
    state = _state(request)
    actor = _actor(request)
    hits = state.knowledge.search(actor, project_id, body.query, limit=body.limit)
    assessment = state.knowledge.assess(hits)
    return {
        **assessment.to_dict(),
        "ranking_version": RANKING_VERSION,
        "hits": [hit.to_dict() for hit in hits],
    }


@router.get(
    "/projects/{project_id}/sources/{source_id}/span",
    # 契约表只有"路径 / 方法 / 说明"三列，所以**必填参数写进说明**：
    # `document_id` 是客户端可见的破坏性变更（少了它请求无法表达"要哪一版"），
    # 让它只出现在 OpenAPI 的 hash 变化里、不出现在契约文字里，等于把
    # "契约必须与权威源有机械联系"这句话放空一半。
    summary="Read Source Span (document_id required)",
)
def read_source_span(
    request: Request,
    project_id: str,
    source_id: str,
    document_id: str,
    start: int,
    end: int,
    content_hash: str = "",
) -> dict:
    """按**引用里的不可变标识**精确回读原文切片 —— 引用可核验的落点。

    `document_id` 是**必填**的。这不是"多一个参数以防万一"：同一来源的两版
    片段可能落在同一个跨度上，只按 `source_id + span` 回读会返回**另一版**
    （R4-03 实测：命中 `doc_v2/'delta!'`、回读拿到 `doc_v1/'bravo!'`），
    而调用方毫无察觉。少了它，请求本身就无法表达"我要哪一版"。

    `content_hash` 可选：给了就核对，对不上**返回同一个 404** —— 客户端
    拿到的不是"另一段内容"，而是"你要的那一段不在这里"。

    找不到、跨项目、跨租户、标识对不上**返回同一个 404 与同一句话术**：
    区分原因等于提供存在性探针，探测者据此能数出别的项目/租户有多少资料。

    响应里的 `content_hash` 由 `content` **派生**（`StoredChunk` 的属性），
    因此它与返回的切片不可能不一致；数据库列上的同名值由适配器在读路径上
    断言相等（`db/ingestion_store.py`）。这里**不另设一个 `verified` 字段**：
    那种字段只能证明"我刚算过一遍自己"，看起来像验证，实际什么也没验证。
    """
    from app.core.errors import ErrorCode, deny

    if start < 0 or end <= start:
        raise deny(ErrorCode.PARAMS_INVALID, "span 必须满足 0 <= start < end")
    state = _state(request)
    chunk = state.knowledge.read_span(
        _actor(request),
        project_id,
        source_id,
        (start, end),
        document_id=document_id,
        content_hash=content_hash,
    )
    if chunk is None:
        raise deny(ErrorCode.CROSS_TENANT_DENIED, "资源不存在", source_id=source_id)
    return {**chunk.to_dict(), "citation": chunk.as_artifact_ref().to_dict()}


# ------------------------------------------------- 任务流转 / 详情 / 掌握度


@router.get("/projects/{project_id}/tasks/{task_id}")
def get_task(request: Request, project_id: str, task_id: str) -> dict:
    """任务详情。`verified` 是**计算结论**：存在 >=1 条有效正向学习证据。

    它不存库 —— 存库就需要第二处写入者去维护它，而"已验证"的本质是
    对证据的聚合，不是一个新的可变状态。
    """
    state = _state(request)
    actor = _actor(request)
    task = state.products.get_task(actor, project_id, task_id)
    events = state.evidence.events_for(actor, project_id)
    return {**task.to_dict(), "verified": _task_verified(events, task_id)}


def _task_verified(events, task_id: str) -> bool:
    """verified 的唯一定义：该任务存在 kind=LEARNING 且含有效正向裁决的证据。

    无效裁决（INCONCLUSIVE/VOIDED）与负向证据都不能让任务变绿 ——
    否则"掌握度只由合法证据更新"就会在这里漏一个口子。
    """
    for event in events:
        if event.task_id != task_id or event.kind is not EvidenceKind.LEARNING:
            continue
        for verdict in event.verdicts:
            if verdict.assessment_validity is Validity.VALID and verdict.direction is Direction.POSITIVE:
                return True
    return False


@router.post(
    "/projects/{project_id}/tasks/{task_id}/transition",
    response_model=None,
)
def transition_task(
    request: Request, project_id: str, task_id: str, body: TaskTransitionBody
) -> dict | JSONResponse:
    """任务状态流转。非法迁移与并发过期都返回 409，但错误码不同：

    ILLEGAL_STATE_TRANSITION（迁移本身不合法）与 VERSION_CONFLICT
    （迁移合法但状态已被别人改掉）混在一起，客户端就无法区分
    "我的请求写错了"和"该刷新重试"。
    """
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        task = state.products.transition_task(
            guard.principal,
            project_id,
            task_id,
            expected_status=TaskStatus(body.expected_status),
            next_status=TaskStatus(body.next_status),
        )
        result = {
            **task.to_dict(),
            "verified": _task_verified(state.evidence.events_for(guard.principal, project_id), task_id),
        }
        guard.complete(200, result)
        return result


# ----------------------------------------------------- 诊断 / 生成 / 提交闭环


def _diagnosis_summary(body: DiagnosisBody) -> str:
    levels = {
        "beginner": "初学",
        "familiar": "有一些基础",
        "experienced": "已有实践经验",
    }
    styles = {"reading": "阅读", "practice": "动手实践", "mixed": "混合方式"}
    return (
        f"{levels[body.experience_level]}；每周可投入 {body.weekly_hours} 小时；"
        f"偏好{styles[body.preferred_style]}。"
    )


@router.post("/projects/{project_id}/diagnosis", status_code=201, response_model=None)
def create_diagnosis(request: Request, project_id: str, body: DiagnosisBody) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        row = _state(request).learning_loop.record_diagnosis(
            guard.principal,
            project_id,
            diagnosis_id=new_id("diag"),
            answers=body.model_dump(mode="json"),
            summary=_diagnosis_summary(body),
        )
        result = row.to_dict()
        guard.complete(201, result)
        return result


@router.get("/projects/{project_id}/diagnosis")
def latest_diagnosis(request: Request, project_id: str):
    row = _state(request).learning_loop.latest_diagnosis(_actor(request), project_id)
    if row is None:
        return Response(status_code=204)
    return row.to_dict()


def _generated_bundle(
    actor: Principal, project_id: str, goal: str, summary: str, *, now
) -> tuple[PlanBundle, tuple[TaskAssessment, ...]]:
    plan_id = new_id("plan")
    # 标题有模型层 200 字上限；保留目标与诊断的首段，完整值仍在项目/诊断记录中。
    context = f"{goal.strip() or '完成学习目标'}（{summary}）"[:120]
    stage_specs = (
        ("理解核心概念", f"理解并解释：{context}"),
        ("完成实践练习", f"动手完成一个练习：{context}"),
        ("复盘与迁移", f"总结并迁移到新情境：{context}"),
    )
    milestones: list[Milestone] = []
    tasks: list[LearningTask] = []
    assessments: list[TaskAssessment] = []
    for order, ((milestone_title, task_title), component) in enumerate(
        zip(stage_specs, COMPONENTS_V1, strict=True)
    ):
        milestone_id = new_id("mile")
        task_id = new_id("task")
        milestones.append(
            Milestone(
                milestone_id,
                actor.tenant_id,
                project_id,
                plan_id,
                order,
                milestone_title,
                "",
            )
        )
        tasks.append(
            LearningTask(
                task_id,
                actor.tenant_id,
                project_id,
                milestone_id,
                0,
                task_title,
                TaskStatus.PENDING,
            )
        )
        assessments.append(
            TaskAssessment(
                new_id("asm"),
                task_id,
                component,
                "self-report/v1",
                "graph-v1/task-v1",
            )
        )
    bundle = PlanBundle(
        LearningPlan(
            plan_id,
            actor.tenant_id,
            project_id,
            1,
            goal,
            PlanStatus.ACTIVE,
            now,
        ),
        tuple(milestones),
        tuple(tasks),
    )
    return bundle, tuple(assessments)


@router.post("/projects/{project_id}/plan/generate", status_code=201, response_model=None)
def generate_plan(request: Request, project_id: str, body: GeneratePlanBody) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write
    from app.core.errors import ErrorCode, deny

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        state = _state(request)
        diagnosis = state.learning_loop.latest_diagnosis(guard.principal, project_id)
        if diagnosis is None:
            raise deny(ErrorCode.PARAMS_INVALID, "请先完成基础诊断，再生成学习计划")
        project = state.membership.get(guard.principal, project_id)
        bundle, assessments = _generated_bundle(
            guard.principal,
            project_id,
            project.goal,
            diagnosis.summary,
            now=state.clock.now(),
        )
        saved = state.learning_loop.install_generated_plan(guard.principal, project_id, bundle, assessments)
        result = {
            "generator": "template/graph-v1",
            "plan": saved.plan.to_dict(),
            "milestones": [item.to_dict() for item in saved.milestones],
            "tasks": [item.to_dict() for item in saved.tasks],
        }
        guard.complete(201, result)
        return result


@router.post(
    "/projects/{project_id}/tasks/{task_id}/submissions",
    status_code=201,
    response_model=None,
)
def submit_task(request: Request, project_id: str, task_id: str, body: SubmissionBody) -> dict | JSONResponse:
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return JSONResponse(
                status_code=guard.cached_status_code,
                content=guard.cached_body,
                headers={"X-Idempotent-Replay": "true"},
            )
        submission, event = _state(request).learning_loop.submit_self_report(
            guard.principal,
            project_id,
            task_id,
            submission_id=new_id("sub"),
            content=body.content,
        )
        verdict = event.verdicts[0]
        result = {
            "submission": submission.to_dict(),
            "evidence": {
                "event_id": event.event_id,
                "component_id": verdict.component_id,
                "observation_strength": int(verdict.observation_strength),
                "independence_level": str(verdict.independence_level),
                "confidence_note": "self_report_low_observation",
            },
        }
        guard.complete(201, result)
        return result


@router.get("/projects/{project_id}/tasks/{task_id}/submissions")
def list_task_submissions(request: Request, project_id: str, task_id: str) -> dict:
    rows = _state(request).learning_loop.submissions_for_task(_actor(request), project_id, task_id)
    return {"submissions": [row.to_dict() for row in rows]}
