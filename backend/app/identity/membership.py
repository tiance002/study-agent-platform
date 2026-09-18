"""租户成员与项目归属。

**补上审查指出的缺口**：接口此前只校验了「路径与请求体一致」，
但没有校验**调用者是否属于该项目**。身份来自令牌之后，还要判断这个身份
有没有资格访问目标项目 —— 否则任意租户的合法用户都能读写别人项目的路径。

三条判定**都不可省略**：
1. 项目存在，且属于该主体的租户；
2. 主体是该租户的成员；
3. 主体被显式授予了该项目。

前两条任一不满足与"项目不存在"返回同一种拒绝，避免暴露存在性。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import ErrorCode, deny
from app.identity.models import Principal


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    tenant_id: str
    name: str = ""


@dataclass
class MembershipStore:
    """成员关系与项目授予。内存实现；生产由 PostgreSQL 承载并受 RLS 保护。"""

    _projects: dict[str, ProjectRecord] = field(default_factory=dict)
    _members: dict[str, set[str]] = field(default_factory=dict)
    _grants: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    # ------------------------------------------------------------------ 写入

    def create_project(self, project_id: str, tenant_id: str, name: str = "") -> ProjectRecord:
        if project_id in self._projects:
            raise deny(ErrorCode.BUDGET_TREE_INVALID, f"项目已存在：{project_id}")
        record = ProjectRecord(project_id=project_id, tenant_id=tenant_id, name=name)
        self._projects[project_id] = record
        return record

    def add_member(self, tenant_id: str, principal_id: str) -> None:
        self._members.setdefault(tenant_id, set()).add(principal_id)

    def grant_project(self, tenant_id: str, principal_id: str, project_id: str) -> None:
        project = self._projects.get(project_id)
        if project is None:
            raise deny(ErrorCode.CROSS_PROJECT_DENIED, f"项目不存在：{project_id}")
        if project.tenant_id != tenant_id:
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "不能把其他租户的项目授予本租户成员",
                project_tenant=project.tenant_id,
                tenant_id=tenant_id,
            )
        self.add_member(tenant_id, principal_id)
        self._grants.setdefault((tenant_id, principal_id), set()).add(project_id)

    # ------------------------------------------------------------------ 查询

    def projects_for(self, principal: Principal) -> tuple[str, ...]:
        return tuple(sorted(self._grants.get((principal.tenant_id, principal.principal_id), set())))

    def assert_can_access(self, principal: Principal, project_id: str) -> ProjectRecord:
        """访问判定。**这是项目边界真正的执行点。**

        不通过时抛出的错误与"项目不存在"一致，调用方在 API 层统一转成 404 ——
        不能让人通过错误差异探测出别的租户有哪些项目。
        """
        project = self._projects.get(project_id)
        if project is None or project.tenant_id != principal.tenant_id:
            raise deny(
                ErrorCode.CROSS_TENANT_DENIED,
                "无权访问该项目",
                project_id=project_id,
            )
        if principal.principal_id not in self._members.get(principal.tenant_id, set()):
            raise deny(
                ErrorCode.AUTH_REQUIRED,
                "该主体不是本租户成员",
                tenant_id=principal.tenant_id,
            )
        granted = self._grants.get((principal.tenant_id, principal.principal_id), set())
        if project_id not in granted:
            raise deny(
                ErrorCode.CROSS_PROJECT_DENIED,
                "该主体未被授予此项目",
                project_id=project_id,
            )
        return project
