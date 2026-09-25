"""用户级共享知识库路由。

## 这个模块存在的理由

知识库是**用户级持久资源**：在左侧栏面板登记一次，之后所有项目都能直接关联。
产品要求原文是"上传一次、到处可用"，而不是"每个项目再传一遍"。

## 三条边界，都收在服务端

1. **登记幂等**：按 `(tenant_id, principal_id, identity_hash)` ——
   `identity_hash` 由服务端派生（`knowledge.library.library_source_identity_hash`），
   客户端声称的"同一份材料"不算数。登记时**带了原文**的材料，身份里并入
   内容指纹：因此"同名不同内容"是两份材料（不会被幂等命中而丢弃新原文），
   而同一 `(acquisition, content)` 重复登记仍然只落一条。
2. **隔离**：读写都走主体级仓储（PostgreSQL 侧是 FORCE RLS）。
   不可见与不存在**同码同话术**，不给存在性探针留缝。
3. **关联**：要求目标项目的 `membership.get` 通过；服务端在目标项目内
   **复用同一份材料**生成项目级 `sources`（同 `identity_hash`），
   有库内原文就直接入队摄取、没有就入队抓取。项目里的检索、引用与
   项目级 RLS 因此**零改动**。关联是**一条原子命令**：资料登记与
   摄取/抓取入队在同一个事务里完成，第二步失败不会留下"有资料无任务"。

## 为什么关联是"复用同一份材料"而不是复制内容

`identity_hash` 由服务端算出来，所以关联命中的就是项目里已有的那条
（如果有）。重复关联只会在项目里留下一份资料 —— 资料去重与摄取任务去重
都在服务端完成，不依赖客户端"别点两次"。

**但原文是冻结的**：关联那一刻把库内**最新一版**原文拷进项目副本，
之后库侧 `set_content` 出新版本**不会**回溯已关联的项目。响应里的
`library_document_version` 就是"这一次冻结的是哪一版"。
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.ids import new_id
from app.identity.models import Principal
from app.knowledge.acquisition import AcquisitionRequest, CandidateStatus, SourceCandidate
from app.knowledge.library import LibraryRepository, library_source_identity_hash
from app.knowledge.models import (
    ACQUISITION_METHOD_UPLOAD,
    MAX_DOCUMENT_BYTES,
    TEXT_MEDIA_TYPES,
    require_document_content,
)
from app.product.models import SourceRecord

router = APIRouter()

#: 库内原文入队摄取时的语言标签。登记契约里没有 language 字段，
#: 因此用一个稳定的平台默认值（与产品其它端点的默认一致）。
_DEFAULT_LANGUAGE = "zh"

#: 库内材料登记时 `media_type` 是自由文本（上限 100），而摄取只接受
#: `text/plain` / `text/markdown`（未知类型必须被拒绝，而不是当成纯文本放过去）。
#: 两者不是同一个问题：登记的 media_type 是**元数据**，入队时必须是**闭集之一**。
#: 未知取值一律按纯文本处理 —— 把未知内容当 Markdown 解析会凭 `#` 造出
#: 不存在的标题层级，而按纯文本处理只会少一些结构元数据。
_DEFAULT_DOCUMENT_MEDIA_TYPE = "text/plain"

#: 知识库候选的有效期与既有候选端点一致（7 天）。
_CANDIDATE_TTL = timedelta(days=7)


class LibrarySourceBody(BaseModel):
    """登记一份知识库材料。`content` 可选（纯文本原文）。"""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    media_type: str = Field(default="", max_length=100)
    acquisition: dict = Field(default_factory=dict)
    #: 可选原文。给了就随登记一起落库（has_content=true）；
    #: 没给则稍后走 `POST /library/sources/{id}/content`。
    content: str | None = None

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str | None) -> str | None:
        return None if value is None else require_document_content(value)


class LibraryContentBody(BaseModel):
    """存储一版库内原文。与项目级上传共享同一套字节上限与校验出口。"""

    model_config = ConfigDict(extra="forbid")

    #: ⚠️ `max_length` 是**字符**上限，只是字节上限的粗筛：UTF-8 每个字符
    #: 至少一字节，所以字符数 ≤ 字节数 —— 超过字节上限的输入必然也超过
    #: 字符上限，粗筛不会漏（它的作用是不把巨量输入解码进内存）。
    #: 真正的判定按字节做，由下面的校验器调用**同一个** `require_document_content`。
    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_BYTES)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        return require_document_content(value)


def _state(request: Request):
    return request.app.state.platform


def _actor(request: Request) -> Principal:
    from app.api.auth_routes import authenticate_request

    return authenticate_request(request)


def _library(request: Request) -> LibraryRepository:
    return _state(request).library


@router.post(
    "/library/sources",
    status_code=201,
    response_model=None,
    summary="Register Library Source",
)
def register_library_source(request: Request, body: LibrarySourceBody) -> JSONResponse:
    """登记一份知识库材料。已存在时返回既有记录（200），否则 201。"""
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return guard.replay_response()
        record, created = _library(request).register(
            guard.principal,
            library_source_id=new_id("libsrc"),
            display_name=body.display_name,
            media_type=body.media_type,
            identity_hash=library_source_identity_hash(
                body.acquisition, body.content or ""
            ),
            acquisition=body.acquisition,
            content=body.content or "",
            library_document_id=new_id("libdoc"),
        )
        result = record.to_dict()
        status = 201 if created else 200
        guard.complete(status, result)
        return JSONResponse(status_code=status, content=result)


@router.get("/library/sources", summary="List Library Sources")
def list_library_sources(request: Request) -> dict:
    """当前主体的知识库列表（稳定排序）。"""
    rows = _library(request).list_sources(_actor(request))
    return {"sources": [row.to_dict() for row in rows]}


@router.post(
    "/library/sources/{library_source_id}/content",
    status_code=202,
    response_model=None,
    summary="Upload Library Source Content",
)
def upload_library_content(
    request: Request, library_source_id: str, body: LibraryContentBody
) -> dict | JSONResponse:
    """存储一版库内原文；关联时服务端直接摄取，客户端不再上传。**返回 202。**"""
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, body) as guard:
        if guard.replay:
            return guard.replay_response()
        record = _library(request).set_content(
            guard.principal,
            library_source_id,
            content=body.content,
            library_document_id=new_id("libdoc"),
        )
        result = record.to_dict()
        guard.complete(202, result)
        return result


@router.post(
    "/projects/{project_id}/library-sources/{library_source_id}/attach",
    response_model=None,
    summary="Attach Library Source To Project",
)
def attach_library_source(
    request: Request, project_id: str, library_source_id: str
) -> dict | JSONResponse:
    """把知识库材料关联到项目：在目标项目内复用同一份材料并让摄取/抓取入队。

    响应 `{source_id, ingestion_job_id, created, library_document_version}`：

    - `created` 表示项目里这条资料是**本次新建**的还是复用既有的；
    - `ingestion_job_id` 是缺省路径下可直接轮询的摄取任务 id；
    - `library_document_version` 是本次关联读取到的**库内原文最新版本号**
      （只做加法新增；`null` = 这次关联没有库内原文）。首次关联会把它冻结进
      项目副本；库侧之后 `set_content` 出的新版本**不会**回溯已关联的项目
      （见模块 docstring 的「关联即冻结」）；
    - URL 类材料（库里没有原文、`acquisition` 带 url）走**抓取**路径：
      先登记候选并创建 durable 下载任务，因此此刻还没有摄取任务，
      `ingestion_job_id` 为 `null`（下载完成后 worker 才会产生摄取任务）。

    关联要求目标项目的 `membership.get` 通过；重复关联复用既有资料与既有任务，
    不产生第二份资料或第二个摄取。资料登记与摄取/抓取入队是**一条原子命令**
    （见 `IngestionRepository.enqueue_with_source`）：第二步失败不会留下半成品。
    """
    from app.api.http_idempotency import idempotent_write

    with idempotent_write(request, None) as guard:
        if guard.replay:
            return guard.replay_response()
        state = _state(request)
        actor = guard.principal
        # 知识库可见性先行：他人主体的材料与不存在的材料同样是 404。
        library_source = state.library.get_source(actor, library_source_id)
        # 目标项目必须属于调用者（原子命令内部也会判定一次，这里显式前置）。
        state.membership.get(actor, project_id)

        candidate_source_id = new_id("src")
        document_version: int | None = None
        ingestion_job_id: str | None = None
        content = state.library.latest_content(actor, library_source_id)
        if content:
            document_version = state.library.latest_document_version(actor, library_source_id)
            # 原子路径：登记/复用资料 + 入队摄取，同一事务，不留半成品。
            source, job, created = state.ingestion.enqueue_with_source(
                actor,
                project_id,
                source_id=candidate_source_id,
                display_name=library_source.display_name,
                media_type=library_source.media_type,
                identity_hash=library_source.identity_hash,
                acquisition=library_source.acquisition,
                document_id=new_id("doc"),
                job_id=new_id("job"),
                title=library_source.display_name,
                content=content,
                document_media_type=_document_media_type(library_source.media_type),
                language=_DEFAULT_LANGUAGE,
                acquisition_method=ACQUISITION_METHOD_UPLOAD,
            )
            ingestion_job_id = job.job_id
        else:
            # 无库内原文：URL 材料由抓取路径**在它自己的事务里**登记资料 +
            # 创建下载任务（`select_with_source`），避免"先登记资料、抓取失败"。
            source = _enqueue_fetch(state, actor, project_id, candidate_source_id, library_source)
            if source is None:
                # 既没有原文也没有可抓取的 URL：只登记资料，没有可入队的活。
                source = state.products.register_source(
                    actor,
                    project_id,
                    source_id=candidate_source_id,
                    display_name=library_source.display_name,
                    media_type=library_source.media_type,
                    identity_hash=library_source.identity_hash,
                    acquisition=library_source.acquisition,
                )
            created = source.source_id == candidate_source_id

        result = {
            "source_id": source.source_id,
            "ingestion_job_id": ingestion_job_id,
            "created": created,
            "library_document_version": document_version,
        }
        guard.complete(200, result)
        return result


# --------------------------------------------------------------------- 内部


def _document_media_type(media_type: str) -> str:
    """把登记的 `media_type` 收窄成摄取接受的闭集之一（见模块常量说明）。"""
    return media_type if media_type in TEXT_MEDIA_TYPES else _DEFAULT_DOCUMENT_MEDIA_TYPE


def _enqueue_fetch(
    state, actor: Principal, project_id: str, source_id: str, library_source
) -> SourceRecord | None:
    """URL 类材料：登记候选并创建 durable 下载任务（请求内不访问外网）。

    复用 `AcquisitionRepository` 而不是另写一条抓取路径：候选状态机、下载围栏与
    worker 认领全都已经在那里，复制一份只会让两套语义慢慢分叉。
    幂等键由 `library_source_id` 派生，因此重复关联会命中既有下载任务。

    资料登记由 `select_with_source` **在它自己的事务内**完成（与该下载任务一起）：
    因此抓取入队失败不会留下一条孤儿资料。返回登记/复用到的项目资料；
    既没有原文、也没有可抓取的 URL 时返回 `None`（只登记资料，没有可入队的活）。
    """
    url = library_source.acquisition.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    now = state.clock.now()
    candidate = _find_or_discover_candidate(state, actor, project_id, library_source, url, now)
    source, _job = state.acquisition.select_with_source(
        actor,
        project_id,
        AcquisitionRequest(
            acquisition_id=new_id("acq"),
            tenant_id=actor.tenant_id,
            project_id=project_id,
            source_id=source_id,
            candidate_id=candidate.candidate_id,
            requested_by=actor.principal_id,
            url=candidate.url,
            title=candidate.title,
            media_type=_document_media_type(library_source.media_type),
            language=_DEFAULT_LANGUAGE,
            idempotency_key=f"library-attach:{library_source.library_source_id}",
            requested_at=now,
        ),
        source_display_name=library_source.display_name,
        source_identity_hash=library_source.identity_hash,
        source_acquisition=library_source.acquisition,
    )
    return source


def _find_or_discover_candidate(
    state, actor: Principal, project_id: str, library_source, url: str, now
) -> SourceCandidate:
    """复用该项目里同一个 URL 的候选，没有才新建。

    复用的意义：每次关联都新建候选会留下一串永远不会被处理的孤儿行
    （而它们看起来完全合法，还会出现在项目的候选列表里）。

    - `discovered` 且未过期：直接复用（还没授权过下载）；
    - `selected`：也复用 —— 它表示这个 URL 已经授权过下载，而重复关联会命中
      由 `library_source_id` 派生的下载幂等键、直接返回既有任务，
      因此不会再消费一次候选。**不复用** `rejected` / `expired`：
      那两种状态是"这次不要"，新建候选才是正确表达。
    """
    from app.api.product_schemas import CandidateBody
    from app.core.errors import ErrorCode, deny

    try:
        safe = CandidateBody(url=url, title=library_source.display_name[:300], snippet="")
    except ValueError as exc:
        # URL 不符合安全策略（非 HTTP(S) / 私网字面量 / 端口越界）：客户端可修复。
        raise deny(ErrorCode.PARAMS_INVALID, "知识库材料的 URL 不符合安全策略") from exc
    reusable = {CandidateStatus.DISCOVERED, CandidateStatus.SELECTED}
    for existing in state.acquisition.list_candidates(actor, project_id):
        if existing.url == safe.url and existing.title == safe.title and existing.status in reusable:
            if existing.status is CandidateStatus.SELECTED or existing.expires_at > now:
                return existing
    hostname = urlsplit(safe.url).hostname
    assert hostname is not None, "CandidateBody 已校验 URL 必含主机名"
    return state.acquisition.create_candidate(
        actor,
        project_id,
        SourceCandidate(
            candidate_id=new_id("cand"),
            tenant_id=actor.tenant_id,
            project_id=project_id,
            url=safe.url,
            title=safe.title,
            snippet="",
            source_domain=hostname.rstrip(".").encode("idna").decode("ascii").lower(),
            discovered_at=now,
            expires_at=now + _CANDIDATE_TTL,
        ),
    )
