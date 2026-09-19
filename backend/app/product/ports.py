"""产品仓库的**端口**（ports）。

用 `Protocol` 而不是抽象基类：适配器不必继承任何东西就能互换 ——
内存实现与 PostgreSQL 实现在同一套契约测试下跑，靠的是「结构相同」而不是「同源」。

## 为什么每个方法的第一个参数都是 `Principal`

端口层就是「身份从哪来」的边界。**method 签名里没有裸的 `tenant_id` 参数**，
这不是风格问题：一旦允许调用方传 `tenant_id`，就等于允许它自称身份，
而整个 04 号规格的租户隔离就退化成了"调用方记得传对"。

需要"不是当前请求的身份"时（后台任务、种子脚本），走 `SystemContext` 这类
显式类型，而不是让端口接受一个普通字符串 —— 显式的可信上下文和"随手传个 id"
在类型上是两回事。

## 为什么写入方法接收已生成的 id

id 生成属于服务层（`core/ids.py`）。端口接收现成 id 让两件事变简单：
确定性测试（固定 id + 固定时钟 ⇒ 可复现的断言）、
以及"先算好整批 id 再一次性写"的事务内批量插入。

## 为什么方法名带类型后缀（`get_conversation` / `list_sources`）

四个子协议聚合进 `ProductRepository` 时，同名的泛化方法（`get` / `list_for`）
会因为参数签名不同而互不兼容 —— Protocol 多重继承要求同名成员签名一致。
带类型后缀不只是绕开冲突：调用点一眼可见操作对象，比泛化名字更可读。
"""

from __future__ import annotations

from typing import Protocol

from app.identity.models import LearningProject, Principal
from app.product.models import (
    Conversation,
    LearningPlan,
    Message,
    MessageRole,
    PlanBundle,
    SourceRecord,
)


class ProjectRepository(Protocol):
    """项目读写。**每个读方法都必须按成员关系过滤**，不是按租户。"""

    def create(
        self, actor: Principal, *, project_id: str, name: str, goal: str
    ) -> LearningProject: ...

    def list_for(self, actor: Principal) -> tuple[LearningProject, ...]: ...

    def get(self, actor: Principal, project_id: str) -> LearningProject: ...

    def update(
        self,
        actor: Principal,
        project_id: str,
        *,
        name: str | None,
        goal: str | None,
        expected_version: int,
    ) -> LearningProject:
        """改项目。`expected_version` 不匹配时抛冲突 —— 乐观锁而非"最后写入者赢"。"""
        ...


class ConversationRepository(Protocol):
    def create_conversation(
        self, actor: Principal, project_id: str, *, conversation_id: str, title: str
    ) -> Conversation: ...

    def list_conversations(
        self, actor: Principal, project_id: str
    ) -> tuple[Conversation, ...]: ...

    def get_conversation(
        self, actor: Principal, project_id: str, conversation_id: str
    ) -> Conversation: ...


class MessageRepository(Protocol):
    def append_message(
        self,
        actor: Principal,
        project_id: str,
        conversation_id: str,
        *,
        message_id: str,
        role: MessageRole,
        content: str,
    ) -> Message:
        """追加一条消息。`seq` 由实现分配（原子递增会话的序号）并返回。"""
        ...

    def list_messages(
        self, actor: Principal, project_id: str, conversation_id: str
    ) -> tuple[Message, ...]: ...


class PlanRepository(Protocol):
    def current_plan(self, actor: Principal, project_id: str) -> PlanBundle | None:
        """当前计划 = `version` 最大的那一版。没有计划时返回 `None`（不是空 bundle）。"""
        ...

    def replace_plan(self, actor: Principal, project_id: str, bundle: PlanBundle) -> PlanBundle:
        """整版替换。实现必须保证"要么整版换掉、要么什么都不变"；
        版本由实现分配（入参 bundle 的 version 被忽略）。"""
        ...

    def plan_history(self, actor: Principal, project_id: str) -> tuple[LearningPlan, ...]:
        """历史版本，按版本升序。只返回计划头，不含结构。"""
        ...


class SourceRepository(Protocol):
    def register_source(
        self,
        actor: Principal,
        project_id: str,
        *,
        source_id: str,
        display_name: str,
        media_type: str,
        identity_hash: str,
        acquisition: dict,
    ) -> SourceRecord:
        """登记一份资料。

        `identity_hash` 相同即视为同一资料（不重复登记，返回既有记录）。
        本轮**只有登记** —— 不解析、不切块、没有处理状态。
        """
        ...

    def list_sources(
        self, actor: Principal, project_id: str
    ) -> tuple[SourceRecord, ...]: ...

    def get_source(
        self, actor: Principal, project_id: str, source_id: str
    ) -> SourceRecord: ...


class ProductRepository(
    ConversationRepository, MessageRepository, PlanRepository, SourceRepository, Protocol
):
    """聚合协议：装配层持有的产品仓储入口。

    继承四个子协议 —— 结构化类型下，任何满足四个子协议的对象自动满足本协议。
    单独声明它的理由：装配字段需要一个类型名，`InMemory | Postgres` 的联合
    会让"新增第三个适配器"变成到处改注解。
    """
