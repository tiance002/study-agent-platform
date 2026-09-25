"""资料候选、获取、上传与项目内检索路由（产品域之二）。

**资料去重**：`identity_hash` 由服务端从 `acquisition` 规范化 JSON 派生 ——
同一项目内同一获取方式重复登记返回既有记录（幂等成功）。

**网络边界**：登记候选与选择候选都不访问外网；真正的下载由 acquisition
worker 执行，上传原文的切块由 ingestion worker 执行（202 + 任务查询）。
"""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.product_schemas import (
    SOURCE_SEARCH_RATE_LIMIT,
    SOURCE_SEARCH_WINDOW_SECONDS,
    AcquisitionSelectBody,
    CandidateBody,
    KnowledgeSearchBody,
    SourceBody,
    SourceContentBody,
    SourceSearchBody,
)
from app.core.hashing import acquisition_identity_hash
from app.core.ids import new_id
from app.identity.models import Principal
from app.knowledge.acquisition import AcquisitionRequest, SourceCandidate
from app.knowledge.discovery import SearchResult
from app.knowledge.fetch_policy import FetchPolicyError
from app.knowledge.retrieval import RANKING_VERSION

router = APIRouter()


def _state(request: Request):
    return request.app.state.platform


def _actor(request: Request) -> Principal:
    from app.api.auth_routes import authenticate_request

    return authenticate_request(request)


def _identity_hash(acquisition: dict) -> str:
    """从获取方式派生稳定标识。**由服务端计算**：客户端声称的"同一资料"
    不算数，规范化 JSON 的哈希才算——同一 acquisition 必得同一哈希。

    交给 `core.hashing.acquisition_identity_hash` 而不是就地算一遍：
    用户级知识库用**同一个**函数去重，两处各算一次就会在规范化差异出现时
    分叉 —— 那时"知识库里那份材料"与"项目里那份材料"会被当成两份。
    """
    return acquisition_identity_hash(acquisition)


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
            return guard.replay_response()
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
            return guard.replay_response()
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
            return guard.replay_response()
        state = _state(request)
        candidate = state.acquisition.get_candidate(guard.principal, project_id, candidate_id)
        key = request.headers.get("Idempotency-Key", "").strip() or new_id("acq-key")
        source, queued = state.acquisition.select_with_source(
            guard.principal,
            project_id,
            AcquisitionRequest(
                acquisition_id=new_id("acq"),
                tenant_id=guard.principal.tenant_id,
                project_id=project_id,
                source_id=new_id("src"),
                candidate_id=candidate.candidate_id,
                requested_by=guard.principal.principal_id,
                url=candidate.url,
                title=candidate.title,
                media_type=body.media_type,
                language=body.language,
                idempotency_key=key,
                requested_at=state.clock.now(),
            ),
            source_display_name=body.display_name,
            source_identity_hash=_identity_hash({"kind": "web", "url": candidate.url}),
            source_acquisition={"kind": "web", "url": candidate.url},
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
            return guard.replay_response()
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
            return guard.replay_response()
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
