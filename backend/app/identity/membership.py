"""租户成员与项目归属（`MembershipRepository` 的内存实现）。

**项目边界真正的执行点是 `get`** —— 三条判定都不可省略：
1. 项目存在，且属于该主体的租户；
2. 主体是该租户的成员；
3. 主体被显式授予了该项目。

所有失败模式与"项目不存在"返回**同一种**拒绝（同一错误码 + 同一句话），
避免暴露存在性：不能让人通过错误差异探测出别的租户有哪些项目。

供给方法（`create_project` / `grant_project`）接收显式的 `SystemContext`：
种子脚本与运维动作必须回答"这次写入是谁授权的"，而不是随手传一个租户 id。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.clock import Clock, SystemClock
from app.core.errors import ErrorCode, deny
from app.identity.models import LearningProject, Principal
from app.identity.ports import SystemContext


@dataclass
class MembershipStore:
    """成员关系与项目授予的内存实现。

    ⚠️ 这里**不再有自己的项目类型**。此前它带一个私有的 `ProjectRecord`，
    而产品层又要一个 `Project` —— 同一行数据两个模型，总有一个会先被改坏。
    现在统一用 `identity.models.LearningProject`，并由测试守着"只有一个"。

    ⚠️ 内存实现是**开发适配器**：没有 RLS，隔离靠本类的判定；
    PostgreSQL 实现（`db.identity_store`）由数据库兜底，两者跑同一套契约测试。
    """

    _projects: dict[str, LearningProject] = field(default_factory=dict)
    _grants: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    #: 时间源。可注入是为了让测试拿到确定的时间戳（`FixedClock`）。
    clock: Clock = field(default_factory=SystemClock)

    # ------------------------------------------------------------------ 供给

    def create_project(
        self,
        context: SystemContext,
        *,
        project_id: str,
        name: str = "",
        goal: str = "",
    ) -> LearningProject:
        """建项目。租户来自显式上下文，不接受散置参数。"""
        if project_id in self._projects:
            raise deny(ErrorCode.BUDGET_TREE_INVALID, f"项目已存在：{project_id}")
        stamp = self.clock.now()
        project = LearningProject(
            project_id=project_id,
            tenant_id=context.tenant_id,
            name=name,
            created_at=stamp,
            updated_at=stamp,
            goal=goal,
        )
        self._projects[project_id] = project
        return project

    def grant_project(
        self, context: SystemContext, *, principal_id: str, project_id: str
    ) -> None:
        """授予访问权。授予即成为成员 —— 成员关系由 `project_grants` 蕴含。

        跨租户授予在此被显式拒绝；PostgreSQL 实现里同样的拒绝由
        组合外键 `(tenant_id, project_id)` 给出 —— 两侧语义一致，
        由契约测试守着。
        """
        project = self._projects.get(project_id)
        if project is None:
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                project_id=project_id,
            )
        if project.tenant_id != context.tenant_id:
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                project_id=project_id,
            )
        self._grants.setdefault((context.tenant_id, principal_id), set()).add(project_id)

    def create_project_for(
        self, actor: Principal, *, project_id: str, name: str, goal: str
    ) -> LearningProject:
        """创建即授予。内存版顺序：先建后授 —— 中途失败留给 GC 无从谈起，
        dict 赋值不会失败；PG 版由同事务保证等价语义（见 identity_store）。
        """
        context = SystemContext(actor.tenant_id, "用户创建项目")
        project = self.create_project(context, project_id=project_id, name=name, goal=goal)
        self.grant_project(context, principal_id=actor.principal_id, project_id=project_id)
        return project

    def update(
        self,
        actor: Principal,
        project_id: str,
        *,
        name: str | None,
        goal: str | None,
        expected_version: int,
    ) -> LearningProject:
        """乐观锁更新。访问判定在前（统一拒绝），版本比对在后。"""
        project = self.get(actor, project_id)
        if project.version != expected_version:
            raise deny(
                ErrorCode.VERSION_CONFLICT,
                "项目已被他人修改；请刷新后基于最新版本编辑",
                expected_version=expected_version,
                current_version=project.version,
            )
        updated = LearningProject(
            project_id=project.project_id,
            tenant_id=project.tenant_id,
            name=name if name is not None else project.name,
            goal=goal if goal is not None else project.goal,
            created_at=project.created_at,
            updated_at=self.clock.now(),
            version=project.version + 1,
        )
        self._projects[project_id] = updated
        return updated

    # ------------------------------------------------------------------ 访问

    def list_for(self, actor: Principal) -> tuple[LearningProject, ...]:
        """该主体被授予的项目，按 project_id 稳定排序。

        返回**完整契约**而不是一串 id：产品接口要直接渲染项目卡片，
        「先拿 id 再逐个查」会让列表页变成 N+1 次查询。
        """
        granted = self._grants.get((actor.tenant_id, actor.principal_id), set())
        return tuple(self._projects[pid] for pid in sorted(granted) if pid in self._projects)

    def get(self, actor: Principal, project_id: str) -> LearningProject:
        """访问判定。一切失败模式同一种拒绝（见模块 docstring）。"""
        project = self._projects.get(project_id)
        if (
            project is None
            or project.tenant_id != actor.tenant_id
            or project_id not in self._grants.get((actor.tenant_id, actor.principal_id), set())
        ):
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                project_id=project_id,
            )
        return project
