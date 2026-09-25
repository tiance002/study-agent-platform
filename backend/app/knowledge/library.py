"""用户级共享知识库的**事实契约与端口**。

## 为什么知识库是「主体级」而不是「项目级」

知识库要回答的是「**我**登记过哪些材料」——它是一个**跨项目**的持久资源：
在面板里登记一次，之后所有项目都能直接关联，不需要重新上传。
把它挂在项目下（加一个 `project_id`）就等于要求用户每个项目都再传一遍，
而这正是这个特性要消除的那件事。

于是这两张表没有 `project_id`，隔离维度是 `tenant_id + principal_id`
（见 `alembic/versions/0021_library_sources.py`）。

## 「关联到项目」为什么不在这张表上写 `project_id`

关联的实现是：服务端在目标项目内**复用同一份材料**生成一条普通项目级
`sources`（同 `identity_hash`），有库内原文时直接入队摄取、没有时入队抓取。
这样检索、引用与项目级 RLS 的不变量**零改动** —— 项目里看到的仍是一条
普通的 `sources`，知识库只是它的来源。

反过来做（给知识库行加 `project_id`、让检索跨项目读它）会同时打破
三件事：`sources` 的组合外键不再覆盖它、项目级 RLS 出现一个绕道、
"每个项目只检索自己的资料"从一条 SQL 谓词退化成应用层的判断。

## 端口签名与其它端口一致

每个方法第一个参数都是 `Principal`（服务端断言的身份），不接受裸 `tenant_id`。
不可见与不存在**同码同话术**（统一拒绝），不给存在性探针留缝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.core.contracts import require_aware, require_id, require_positive, require_text
from app.core.hashing import content_hash
from app.identity.models import Principal
from app.knowledge.models import DOCUMENT_PARSER_VERSION, require_document_content


@dataclass(frozen=True)
class LibrarySource:
    """知识库里的一份材料（**登记元数据**）。

    `identity_hash` 是服务端从 `acquisition` 派生的稳定标识，与项目级
    `sources.identity_hash` **同一算法** —— 关联时靠它命中项目里已有的资料，
    因此"知识库里的材料"与"项目里的材料"是同一个身份，不是两份。

    `has_content` 是**派生事实**（库里有没有原文版本），不是可独立修改的开关：
    适配器从 `library_documents` 是否有行算出来，因此它不可能与库内原文漂移。
    """

    library_source_id: str
    tenant_id: str
    principal_id: str
    display_name: str
    media_type: str
    identity_hash: str
    registered_at: datetime
    acquisition: dict = field(default_factory=dict)
    has_content: bool = False

    def __post_init__(self) -> None:
        require_id(self.library_source_id, "library_source_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.principal_id, "principal_id")
        require_text(self.display_name, "display_name")
        require_text(self.media_type, "media_type", allow_empty=True)
        require_id(self.identity_hash, "identity_hash")
        require_aware(self.registered_at, "registered_at")
        if not isinstance(self.acquisition, dict):
            raise TypeError(
                f"acquisition 必须是字典，收到 {type(self.acquisition).__name__}"
            )

    def to_dict(self) -> dict:
        """对外视图。**不含 `acquisition` 之外的内部字段** —— 契约只有这六个。"""
        return {
            "library_source_id": self.library_source_id,
            "display_name": self.display_name,
            "media_type": self.media_type,
            "identity_hash": self.identity_hash,
            "registered_at": self.registered_at.isoformat(),
            "has_content": self.has_content,
        }


@dataclass(frozen=True)
class LibraryDocument:
    """库内的一版原文。**不可变**（一版一行，改内容 = 存新版本）。

    `content_hash` 是**派生属性**（`sha256(content)`），不是构造参数 ——
    与 `SourceDocument` / `StoredChunk` 同一约定：字段不可能与内容漂移。
    """

    library_document_id: str
    tenant_id: str
    principal_id: str
    library_source_id: str
    version: int
    content: str
    observed_at: datetime
    parser_version: str = DOCUMENT_PARSER_VERSION

    def __post_init__(self) -> None:
        require_id(self.library_document_id, "library_document_id")
        require_id(self.tenant_id, "tenant_id")
        require_id(self.principal_id, "principal_id")
        require_id(self.library_source_id, "library_source_id")
        require_positive(self.version, "version")
        require_document_content(self.content)
        require_text(self.parser_version, "parser_version")
        require_aware(self.observed_at, "observed_at")

    @property
    def content_hash(self) -> str:
        return content_hash(self.content)


class LibraryRepository(Protocol):
    """用户级知识库的读写端口。"""

    def register(
        self,
        actor: Principal,
        *,
        library_source_id: str,
        display_name: str,
        media_type: str,
        identity_hash: str,
        acquisition: dict,
        content: str = "",
        library_document_id: str = "",
    ) -> tuple[LibrarySource, bool]:
        """登记一份材料。返回 `(记录, 是否新建)`。

        按 `(tenant_id, principal_id, identity_hash)` 幂等：已存在时返回**既有**
        记录与 `created=False`（幂等成功，不是报错 —— 登记重试不该变成失败）。
        新建且给了 `content` 时，同时存入第一版库内原文。
        """
        ...

    def list_sources(self, actor: Principal) -> tuple[LibrarySource, ...]:
        """当前主体的知识库列表，按 `registered_at, library_source_id` 稳定排序。"""
        ...

    def get_source(self, actor: Principal, library_source_id: str) -> LibrarySource:
        """取单条记录。不可见（他人主体 / 跨租户）与不存在**同码同话术**。"""
        ...

    def latest_content(self, actor: Principal, library_source_id: str) -> str | None:
        """最新一版库内原文；库里没有原文（或不可见）时返回 `None`。

        **不**把"不可见"翻译成一个空值当作判定出口 —— 授权拒绝由
        `get_source` 抛出那**一个**拒绝码，本方法只回答"有没有原文"。
        """
        ...

    def set_content(
        self,
        actor: Principal,
        library_source_id: str,
        *,
        content: str,
        library_document_id: str,
    ) -> LibrarySource:
        """存入一版新原文（版本 = 当前最大 + 1）并返回更新后的记录。

        资料不可见时抛与 `get_source` 同源的拒绝码。
        """
        ...
