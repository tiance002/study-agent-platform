"""In-memory account credential adapter used by the development platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING

from app.audit.sink import RiskLevel
from app.core.clock import Clock
from app.core.errors import ErrorCode, deny
from app.core.ids import new_id
from app.identity.models import UserSession
from app.identity.ports import (
    AccountLookup,
    AccountRegistrationResult,
    MembershipRepository,
    SessionRepository,
    SystemContext,
)

if TYPE_CHECKING:
    from app.audit.outbox import InMemoryAuditOutbox


@dataclass
class InMemoryAccountRepository:
    """Atomic enough for a single-process development adapter.

    The production adapter delegates the same operation to the SECURITY DEFINER
    functions created by migration 0012; keeping this boundary identical prevents
    the API from inventing a second registration transaction.
    """

    membership: MembershipRepository
    sessions: SessionRepository
    clock: Clock
    outbox: "InMemoryAuditOutbox | None" = None
    _by_normalized: dict[str, AccountLookup] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def lookup(self, username_normalized: str) -> AccountLookup | None:
        with self._lock:
            return self._by_normalized.get(username_normalized)

    def register(
        self,
        *,
        username_original: str,
        username_normalized: str,
        password_hash: str,
        hash_version: int,
        session_expires_at: datetime,
    ) -> AccountRegistrationResult:
        with self._lock:
            if username_normalized in self._by_normalized:
                raise deny(ErrorCode.USERNAME_TAKEN, "用户名已注册")
            now = self.clock.now()
            tenant_id = new_id("tenant")
            principal_id = new_id("user")
            credential_id = new_id("cred")
            project_id = new_id("proj")
            session_id = new_id("sess")
            try:
                project = self.membership.create_project(
                    # membership's system context is imported lazily to avoid a cycle
                    SystemContext(tenant_id, "open registration"),
                    project_id=project_id,
                    name="我的学习项目",
                )
                self.membership.grant_project(
                    SystemContext(tenant_id, "open registration"),
                    principal_id=principal_id,
                    project_id=project_id,
                )
                lookup = AccountLookup(
                    credential_id=credential_id,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    username=username_original,
                    password_hash=password_hash,
                    hash_version=hash_version,
                    security_generation=1,
                )
                session = UserSession(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    issued_at=now,
                    expires_at=session_expires_at,
                    credential_id=credential_id,
                    security_generation=1,
                )
                self.sessions.create(session)
            except BaseException:
                self._rollback_project(project_id, tenant_id, principal_id)
                raise
            self._by_normalized[username_normalized] = lookup
            if self.outbox is not None:
                self.outbox.stage(
                    "account_registered",
                    {
                        "principal_id": principal_id,
                        "credential_id": credential_id,
                        "default_project_id": project_id,
                    },
                    risk=RiskLevel.HIGH,
                    tenant_id=tenant_id,
                )
            return AccountRegistrationResult(
                lookup=lookup,
                session=session,
                default_project_id=project.project_id,
                default_project_name=project.name,
            )

    def _rollback_project(self, project_id: str, tenant_id: str, principal_id: str) -> None:
        """Undo the memory adapter's two project writes after a later failure."""
        projects = getattr(self.membership, "_projects", None)
        grants = getattr(self.membership, "_grants", None)
        if isinstance(projects, dict):
            projects.pop(project_id, None)
        if isinstance(grants, dict):
            granted = grants.get((tenant_id, principal_id))
            if isinstance(granted, set):
                granted.discard(project_id)
                if not granted:
                    grants.pop((tenant_id, principal_id), None)

    def complete_login(
        self,
        *,
        lookup: AccountLookup,
        session_expires_at: datetime,
        new_password_hash: str | None = None,
        new_hash_version: int | None = None,
    ) -> UserSession | None:
        with self._lock:
            current = next(
                (item for item in self._by_normalized.values() if item.credential_id == lookup.credential_id),
                None,
            )
            if current is None or current.security_generation != lookup.security_generation or current.disabled_at:
                return None
            if new_password_hash is not None:
                current = AccountLookup(
                    credential_id=current.credential_id,
                    tenant_id=current.tenant_id,
                    principal_id=current.principal_id,
                    username=current.username,
                    password_hash=new_password_hash,
                    hash_version=new_hash_version or current.hash_version,
                    security_generation=current.security_generation,
                    disabled_at=current.disabled_at,
                )
                for key, value in self._by_normalized.items():
                    if value.credential_id == lookup.credential_id:
                        self._by_normalized[key] = current
                        break
            now = self.clock.now()
            session = UserSession(
                session_id=new_id("sess"),
                tenant_id=current.tenant_id,
                principal_id=current.principal_id,
                issued_at=now,
                expires_at=session_expires_at,
                credential_id=current.credential_id,
                security_generation=current.security_generation,
            )
            self.sessions.create(session)
            if self.outbox is not None:
                self.outbox.stage(
                    "password_login_succeeded",
                    {
                        "principal_id": current.principal_id,
                        "credential_id": current.credential_id,
                    },
                    risk=RiskLevel.HIGH,
                    tenant_id=current.tenant_id,
                )
            return session
