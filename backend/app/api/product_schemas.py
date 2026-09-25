"""产品路由的共享请求模型与字段上限。

三个领域路由（会话/计划、资料/检索、学习闭环）共用这里的 Pydantic 模型：
字段上限是**公共契约**，放在一起才能一眼看出哪些边界是全产品一致的，
也避免某个领域悄悄放宽上限。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.knowledge.fetch_policy import FetchPolicyError, validate_fetch_target
from app.knowledge.models import MAX_DOCUMENT_BYTES, require_document_content
from app.product.models import MessageRole

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


class KnowledgeSearchBody(BaseModel):
    """检索请求。

    `query` 上下限与 `limit` 上限都是**硬边界**，不是建议值：检索要在
    项目内全部片段上打分，而这两个参数都直接乘进工作量 ——
    不设界就是一个客户端可控的放大入口。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=QUERY_MAX_CHARS)
    limit: int = Field(default=10, ge=1, le=20)


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
