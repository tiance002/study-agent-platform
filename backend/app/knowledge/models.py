"""资料摄取的事实契约：原文、摄取任务、可引用片段。

设计依据：02 号规格（路由检索）、不变量 #16（`EvidenceEvent` 是掌握事实源，
资料原文是引用的最终依据）。第 4 轮范围见
`docs/superpowers/plans/2026-09-20-round-4-source-ingestion-retrieval.md`。

## 三个模型分别回答什么

| 模型 | 回答的问题 | 可变性 |
|---|---|---|
| `SourceDocument` | 「用户上传的原文是什么」 | **不可变**（一版一行） |
| `IngestionJob` | 「这一版处理到哪一步了」 | 唯一可变的事实（唯一状态机） |
| `StoredChunk` | 「可引用的最小单元是哪一段」 | **不可变**（生成后只读） |

## 两条刻意的设计

### 1. `content_hash` 是**派生属性**，不是构造参数

原文与片段的指纹都由 `content_hash(content)` 算出来，所以「哈希与内容不一致」
这件事在类型上无法表达 —— 而不是"靠写入方记得同步更新"。

⚠️ 这不是"少存一个字段"：数据库列 `content_hash` 仍然存在（它是引用的锚点，
检索结果、`ArtifactRef`、跨进程校验都读它）。区别在于**Python 契约层不接收它**，
于是它不可能与内容漂移。若做成构造参数，"字段存在"与"字段生效"就会分家：
传一个假哈希进去，对象照样构造成功，而所有引用校验都会以它为真。

### 2. `span_end - span_start == len(content)` 是硬不变量

片段内容**就是原文的一个切片**。把这条关系收进 `__post_init__`，
「引用能回读原文」就不再依赖切块器写对，而是这类对象根本构造不出错的那种。
切块器一旦算错偏移，第一个片段构造时就炸，而不是等到用户点击引用时才发现引用的
是别处的文字。

## 时间戳一律带时区

naive `datetime` 参与比较不会报错，只会按本地时区解释 —— 跨进程重放与
"从库里读回来再比"这两种场景下它会静默差几小时。校验收在 `core.contracts`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.core.artifacts import ArtifactRef, DisplayPolicy
from app.core.contracts import (
    require_aware,
    require_id,
    require_non_negative,
    require_positive,
    require_text,
)
from app.core.errors import ErrorCode, deny
from app.core.hashing import content_hash
from app.policy.taint import TaintedValue, TaintSource, mark_tainted

#: 单版原文的硬上限（字节）。与迁移里的 `CHECK (octet_length(content) <= ...)` 同值：
#: 数据库是最后一道，应用层必须在**入队之前**就拒绝 —— 否则用户要等一个
#: 注定失败的任务跑完才收到错误。
MAX_DOCUMENT_BYTES = 1_048_576

#: 本轮支持的媒体类型。闭集：未知类型必须被拒绝，而不是当成纯文本放过去。
TEXT_MEDIA_TYPES = ("text/plain", "text/markdown")

#: 原文侧解析器版本。纯文本/Markdown 以字节原样入库，**不做任何改写**
#: （不变量：解析与切块只能增加结构元数据，不得改写证据原文）。
DOCUMENT_PARSER_VERSION = "text/v1"

#: 结构切块器版本。切块规则变化必须改这个值并重跑检索夹具
#: （`backend/tests/fixtures/retrieval_v1.json` 的退出门要求）。
CHUNK_PARSER_VERSION = "structure/v1"

#: 本轮唯一的获取方式。与 `derived_from` 分开保存：一个是"怎么来的"，
#: 一个是"由谁派生"。合并成一列会让"用户上传的原文"和"由原文派生的摘要"
#: 看起来是同一件事。
ACQUISITION_METHOD_UPLOAD = "upload"

#: BCP-47 的宽松子集：`zh` / `en` / `zh-Hans` / `pt-BR` 都合法。
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]+)*$")


class IngestionStatus(StrEnum):
    """摄取任务的状态。

    `succeeded` 与 `failed` 是**终态**：终态任务不再被认领，
    因此「失败重试」只能通过重新上传（新版本）发生 —— 而不是让一个
    已经失败的任务无限循环。这条约束让"任务最终一定会停下来"成为可判定的事实。
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (IngestionStatus.SUCCEEDED, IngestionStatus.FAILED)


def _require_enum(value: object, expected: type[StrEnum], name: str) -> None:
    """枚举字段必须真的是那个枚举。

    与 `product.models` 同一约定：**不做字符串到枚举的隐式转换** ——
    自动转换会把"调用方传了裸字符串"这个错误藏起来，而它通常意味着
    上游少了一层校验。
    """
    if not isinstance(value, expected):
        raise ValueError(f"{name} 必须是 {expected.__name__}，收到 {value!r}")


def _require_utf8_size(content: str, limit: int, field_name: str) -> None:
    """按**字节**而不是字符设限，同时拒绝**不是合法 UTF-8** 的字符串。

    中文一个字三个字节：按字符设限时，一段 100 万字符的中文是 3 MiB，
    能通过应用层校验却在数据库 CHECK 上炸掉 —— 用户拿到的是 500，
    而原因是"我们量错了单位"。

    第二条更隐蔽：JSON 允许 `"\\ud800"` 这样的**孤立代理项**转义，
    解出来是一个 Python 能持有、却无法编码成 UTF-8 的 `str`。
    不在这里拦，`encode` 会抛 `UnicodeEncodeError` —— 那不是 ValueError，
    pydantic 不会翻成 422，请求会以 500 结束。**这就是"必须在进入切块器之前
    拒绝非法 UTF-8"这条要求的落点。**
    """
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{field_name} 不是合法的 UTF-8 文本（{exc.reason}）；"
            "本轮只支持 UTF-8 纯文本与 Markdown"
        ) from exc
    if size > limit:
        raise ValueError(
            f"{field_name} 超过单版上限：{size} 字节 > {limit} 字节"
            "（上限按 UTF-8 字节数计，中文一字约三字节）"
        )


def require_document_content(content: str) -> str:
    """原文内容的**唯一**校验出口：非空 + 合法 UTF-8 + 不超过单版字节上限。

    为什么必须是共享函数而不是两处各写一份：HTTP 请求模型要用它把超限翻译成
    422，领域模型要用它守住不变量。两处各写一份的话，"多大算太大"会有两个答案，
    而客户端拿到的是哪一个取决于走了哪条路径 —— 更糟的是，两者不一致时
    请求会通过应用层校验、在数据库 CHECK 上炸掉，用户拿到 500。

    ⚠️ 注意这个函数**只**校验内容本身。`media_type` / `language` 的闭集校验
    在 `SourceDocument.__post_init__` 里，因为它们是文档的属性，不是内容。
    """
    require_text(content, "content")
    _require_utf8_size(content, MAX_DOCUMENT_BYTES, "content")
    return content


@dataclass(frozen=True)
class SourceDocument:
    """一版**不可变**的原文。

    「一版」是关键词：同一份资料（`source_id`）可以有多个版本，
    每个版本一行、互不覆盖。改内容 = 上传新版本，而不是改旧行 ——
    否则已经发出的引用会指向被改过的文字，`content_hash` 立刻失去意义。
    """

    document_id: str
    tenant_id: str
    project_id: str
    source_id: str
    version: int
    document_title: str
    content: str
    media_type: str
    language: str
    observed_at: datetime
    parser_version: str = DOCUMENT_PARSER_VERSION
    acquisition_method: str = ACQUISITION_METHOD_UPLOAD
    #: 来源污点，闭集来自 `TaintSource`。用户上传的原文一律带
    #: `uploaded_source`，且**只增不减**（不变量 #10）。
    taint_sources: tuple[TaintSource, ...] = (TaintSource.UPLOADED_SOURCE,)
    #: 由哪些事实派生而来（跨版本引用）。用户直接上传的原文为空。
    derived_from: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_id(self.document_id, "document_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        require_id(self.source_id, "source_id")
        require_positive(self.version, "version")
        require_text(self.document_title, "document_title")
        require_document_content(self.content)
        if self.media_type not in TEXT_MEDIA_TYPES:
            raise ValueError(
                f"media_type 必须是 {TEXT_MEDIA_TYPES} 之一，收到 {self.media_type!r}；"
                "本轮不支持 PDF / Office / OCR / 网页抓取"
            )
        if not _LANGUAGE_RE.match(self.language):
            raise ValueError(
                f"language 必须是 BCP-47 形式的语言标签（如 zh / en / zh-Hans），"
                f"收到 {self.language!r}"
            )
        require_text(self.parser_version, "parser_version")
        require_text(self.acquisition_method, "acquisition_method")
        require_aware(self.observed_at, "observed_at")
        if not self.taint_sources:
            raise ValueError(
                "taint_sources 不能为空：任何进入系统的内容都有来源，"
                "「无来源」不是一个可表达的状态"
            )
        for source in self.taint_sources:
            _require_enum(source, TaintSource, "taint_sources[]")
        for ref in self.derived_from:
            require_id(ref, "derived_from[]")

    @property
    def content_hash(self) -> str:
        """`sha256:<hex>` 指纹。**派生**，因此不可能与内容不一致。"""
        return content_hash(self.content)

    def as_tainted(self) -> TaintedValue:
        """带污点的值引用。检索与上下文装配只拿它，不拿裸字符串。"""
        return mark_tainted(self.document_id, self.content, self.taint_sources[0])

    def to_dict(self) -> dict:
        """产品视图。**不含 `content`**：原文按需通过引用回读，
        列表与状态响应里塞原文是客户端可控的响应体放大入口。"""
        return {
            "document_id": self.document_id,
            "source_id": self.source_id,
            "version": self.version,
            "document_title": self.document_title,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "language": self.language,
            "parser_version": self.parser_version,
            "acquisition_method": self.acquisition_method,
            "taint_sources": [str(item) for item in self.taint_sources],
            "derived_from": list(self.derived_from),
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass(frozen=True)
class IngestionJob:
    """一次摄取任务。**唯一可变的事实**（状态机只有一个）。

    租约（`lease_owner` / `lease_until`）与状态是**绑定的**：
    只有 `processing` 才允许携带租约。分开校验的话，"已成功的任务还挂着租约"
    会被当成正常行读回来，而它会让回收逻辑把终态任务重新捞起来。
    """

    job_id: str
    tenant_id: str
    project_id: str
    source_id: str
    document_id: str
    status: IngestionStatus
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    #: 持有租约的 worker 标识。空串表示没有租约（DB 里是 NULL）。
    lease_owner: str = ""
    lease_until: datetime | None = None
    #: **认领围栏 token**：每次认领生成一个不可复用的值，`complete` / `fail`
    #: 必须带上它才能落定。
    #:
    #: 为什么不拿 `lease_owner` 当代次：`--worker-id` 由运维提供，同一个进程
    #: 重启后是同一个名字 —— "同一个名字"不等于"同一代持有者"。而租约只规定
    #: "谁能认领"，不规定"谁能落定"：A 超时、B 接管之后，A 迟到的 `fail`
    #: 能把 B 正在处理的任务打成 `failed`（内存适配器实测复现）。
    claim_token: str = ""
    #: 稳定错误码与**安全**描述。原始异常文本可能含路径、SQL 片段等内容，
    #: 一律不进这里 —— 它会被状态接口原样返回给用户。
    error_code: str = ""
    error_detail: str = ""

    def __post_init__(self) -> None:
        require_id(self.job_id, "job_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        require_id(self.source_id, "source_id")
        require_id(self.document_id, "document_id")
        _require_enum(self.status, IngestionStatus, "status")
        require_non_negative(self.attempt_count, "attempt_count")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        require_text(self.lease_owner, "lease_owner", allow_empty=True)
        require_text(self.claim_token, "claim_token", allow_empty=True)
        require_text(self.error_code, "error_code", allow_empty=True)
        require_text(self.error_detail, "error_detail", allow_empty=True)

        if self.status is IngestionStatus.PROCESSING:
            if not self.lease_owner.strip():
                raise ValueError("processing 的任务必须记录租约持有者（worker id）")
            if self.lease_until is None:
                raise ValueError(
                    "processing 的任务必须有租约到期时间 —— 否则崩溃后无人可回收"
                )
            require_aware(self.lease_until, "lease_until")
            if not self.claim_token.strip():
                # 没有 token 的 processing 任务**永远无法落定**：`complete` / `fail`
                # 的条件更新要求 token 匹配，而它匹配不上任何值。任务会静默卡死
                # 到租约回收，然后被重新认领 —— 那时它才拿到 token。
                # 把它挡在构造期，问题就停在写入之前，而不是留给排障。
                raise ValueError(
                    "processing 的任务必须带认领 token（claim_token）—— "
                    "否则这次认领无法被落定，任务只能等租约回收"
                )
        elif self.lease_owner or self.lease_until is not None or self.claim_token:
            raise ValueError(
                f"只有 processing 的任务可以携带租约与认领 token，当前状态是 "
                f"{self.status!r}；终态还挂着它们会让回收逻辑把它重新捞起来"
            )

        if self.status is IngestionStatus.FAILED:
            require_text(self.error_code, "error_code")
            require_text(self.error_detail, "error_detail")
        elif self.error_code or self.error_detail:
            raise ValueError(
                f"只有 failed 的任务可以携带错误码，当前状态是 {self.status!r}"
            )

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def holds_valid_lease(self, now: datetime) -> bool:
        """租约是否仍然有效。**只对 processing 有意义** —— 终态的租约恒为无。"""
        return (
            self.status is IngestionStatus.PROCESSING
            and self.lease_owner != ""
            and self.lease_until is not None
            and now < self.lease_until
        )

    def to_dict(self) -> dict:
        """状态视图。只暴露稳定状态、尝试次数、安全错误码与时间戳。"""
        return {
            "job_id": self.job_id,
            "source_id": self.source_id,
            "document_id": self.document_id,
            "status": str(self.status),
            "attempt_count": self.attempt_count,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class StoredChunk:
    """一个**不可变**的可引用片段。

    `heading_path` 是从文档根到该片段的标题路径（栈式构建）。
    一行标题层级跳级（`# A` 之后直接 `### C`）不会补空节点 ——
    路径只记录真实存在的标题，`heading_level` 因此等于路径深度，
    而不是 Markdown 里 `#` 的个数。这条定义写在这里，是因为
    "层级"若有两种解释，检索加权与展示缩进就会各按一种来。
    """

    chunk_id: str
    tenant_id: str
    project_id: str
    source_id: str
    document_id: str
    chunk_index: int
    heading_path: tuple[str, ...]
    span_start: int
    span_end: int
    content: str
    created_at: datetime
    parser_version: str = CHUNK_PARSER_VERSION
    display_policy: DisplayPolicy = DisplayPolicy.FULL
    heading_level: int = 0

    def __post_init__(self) -> None:
        require_id(self.chunk_id, "chunk_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.project_id, "project_id")
        require_id(self.source_id, "source_id")
        require_id(self.document_id, "document_id")
        require_non_negative(self.chunk_index, "chunk_index")
        require_non_negative(self.span_start, "span_start")
        require_text(self.content, "content")
        require_text(self.parser_version, "parser_version")
        require_aware(self.created_at, "created_at")
        if not isinstance(self.display_policy, DisplayPolicy):
            raise ValueError(
                f"display_policy 必须是 DisplayPolicy，收到 {self.display_policy!r}"
            )
        for title in self.heading_path:
            require_text(title, "heading_path[]")
        require_non_negative(self.heading_level, "heading_level")
        if self.heading_level != len(self.heading_path):
            raise ValueError(
                f"heading_level（{self.heading_level}）必须等于标题路径深度"
                f"（{len(self.heading_path)}）；两者不一致意味着'层级'有两种解释"
            )
        if self.span_end <= self.span_start:
            raise ValueError(
                f"span 必须满足 span_end > span_start，收到 "
                f"[{self.span_start}, {self.span_end})"
            )
        if self.span_end - self.span_start != len(self.content):
            # 这条断言是「引用能回读原文」的机械保证：span 与内容长度必须严丝合缝，
            # 否则原文切片与片段内容就是两段不同的文字，而它看起来完全正常。
            raise ValueError(
                f"span 长度（{self.span_end - self.span_start}）必须等于内容长度"
                f"（{len(self.content)}）—— 片段内容必须是原文的精确切片"
            )

    @property
    def span(self) -> tuple[int, int]:
        return (self.span_start, self.span_end)

    @property
    def content_hash(self) -> str:
        """`sha256:<hex>` 指纹。**派生**，因此不可能与内容不一致。"""
        return content_hash(self.content)

    @property
    def heading_text(self) -> str:
        """标题路径的纯文本形式，用于检索加权与展示。"""
        return " / ".join(self.heading_path)

    def as_tainted(self) -> TaintedValue:
        return mark_tainted(self.chunk_id, self.content, TaintSource.UPLOADED_SOURCE)

    def as_artifact_ref(self) -> ArtifactRef:
        """转成谱系引用。内容本体不进上下文，只带指针与指纹。"""
        return ArtifactRef(
            source_id=self.source_id,
            span=self.span,
            content_hash=self.content_hash,
            parser_version=self.parser_version,
            display_policy=self.display_policy,
        )

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "document_id": self.document_id,
            "chunk_index": self.chunk_index,
            "heading_path": list(self.heading_path),
            "heading_level": self.heading_level,
            "span": list(self.span),
            "content": self.content,
            "content_hash": self.content_hash,
            "parser_version": self.parser_version,
            "display_policy": str(self.display_policy),
        }


def assert_chunks_belong_to_job(
    job: IngestionJob, chunks: tuple[StoredChunk, ...]
) -> None:
    """一批片段是否可以挂到该任务上。

    **这是两个适配器共用的唯一判定出口。** 各写一份的话，
    PostgreSQL 版与内存版会以不同速度演进，而它们表达的是同一条不变量 ——
    于是"内存版拒绝、PG 版放行"这种差异只会在生产上第一次出现。

    两条判定：

    1. **作用域三个维度全部一致**（租户 / 项目 / 原文，外加资料 id）。
       少了它，"worker 把 A 项目的片段挂到 B 项目的原文上"只是一个参数写错，
       它会静默产生一条引用指向别人资料的片段 —— 而引用看起来完全正常。
    2. **序号必须是 0..n-1 的连续整数**。留空洞意味着"有一段时间的原文
       没有对应的可引用片段"，检索结果看不出来，用户只会觉得"这段没搜到"。
    """
    for chunk in chunks:
        if (
            chunk.tenant_id != job.tenant_id
            or chunk.project_id != job.project_id
            or chunk.document_id != job.document_id
            or chunk.source_id != job.source_id
        ):
            raise deny(
                ErrorCode.CROSS_PROJECT_DENIED,
                "片段的作用域与任务不一致；跨项目的片段不得挂到本任务上",
                job_id=job.job_id,
                chunk_id=chunk.chunk_id,
            )
    indices = sorted(chunk.chunk_index for chunk in chunks)
    if indices != list(range(len(chunks))):
        raise deny(
            ErrorCode.PARAMS_INVALID,
            f"片段序号必须是 0..{len(chunks) - 1} 的连续整数，实际为 {indices}",
            job_id=job.job_id,
        )
