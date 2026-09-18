"""应用入口：装配各层组件并暴露 HTTP 接口。

运行：

```bash
# 在仓库根目录
uvicorn app.main:app --app-dir backend --reload
# 然后打开 http://127.0.0.1:8000/
```

⚠️ 本版是「批次一最小闭环的**开发适配器版**」：核心边界逻辑完整且可测，
   但持久化、隔离沙箱、真实检索管线、模型调用、KMS 与 PostgreSQL RLS
   均**未实现**。详见 README 的「未实现项」一节。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api.routes import error_response, router
from app.audit.sink import AuditSink
from app.budget.ledger import BudgetLedger
from app.core.clock import SystemClock
from app.core.errors import PlatformError
from app.execution.state_machine import ActionStateMachine
from app.knowledge.retrieval import ChunkIndex
from app.learning.evidence import EvidenceLog
from app.learning.projector import Projector
from app.policy.gateway import PolicyGateway
from app.policy.token import TokenIssuer
from app.registry.registry import Registry
from app.workflow.catalog import build_registry
from app.workflow.runtime import InteractionRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"
VAR_DIR = REPO_ROOT / "var"


@dataclass
class PlatformState:
    registry: Registry
    gateway: PolicyGateway
    ledger: BudgetLedger
    audit: AuditSink
    machine: ActionStateMachine
    tokens: TokenIssuer
    projector: Projector
    clock: SystemClock
    chunk_index: ChunkIndex
    evidence_log: EvidenceLog
    runtime: InteractionRuntime


def build_platform(
    *,
    audit_available: bool = True,
    var_dir: Path | None = None,
) -> PlatformState:
    """装配平台。参数用于故障注入与测试隔离。"""
    base = var_dir or VAR_DIR
    registry = build_registry()
    gateway = PolicyGateway()
    ledger = BudgetLedger()
    machine = ActionStateMachine()
    clock = SystemClock()
    tokens = TokenIssuer(
        # 生产密钥必须来自 KMS / Secret Manager，并与数据、审计密钥分离。
        secret=os.environ.get("STUDY_PLATFORM_TOKEN_SECRET", "dev-only-placeholder-change-me")
    )
    projector = Projector()
    chunk_index = ChunkIndex()
    evidence_log = EvidenceLog()
    audit = AuditSink(base / "audit", available=audit_available)

    runtime = InteractionRuntime(
        registry=registry,
        gateway=gateway,
        ledger=ledger,
        audit=audit,
        machine=machine,
        tokens=tokens,
        projector=projector,
        clock=clock,
        chunk_index=chunk_index,
        evidence_log=evidence_log,
    )
    return PlatformState(
        registry=registry,
        gateway=gateway,
        ledger=ledger,
        audit=audit,
        machine=machine,
        tokens=tokens,
        projector=projector,
        clock=clock,
        chunk_index=chunk_index,
        evidence_log=evidence_log,
        runtime=runtime,
    )


def create_app(*, platform: PlatformState | None = None) -> FastAPI:
    app = FastAPI(
        title="Agent 工程学习规划平台（批次一开发适配器版）",
        version="0.1.0",
        description=(
            "核心边界逻辑的可用实现：策略网关、capability token、taint/endorsement、"
            "预算树、幂等执行状态机、审计哈希链、证据与掌握投影、子任务运行时。"
            "持久化与沙箱为开发适配器，生产适配器未实现。"
        ),
    )
    app.state.platform = platform or build_platform()
    app.include_router(router)

    @app.exception_handler(PlatformError)
    async def _platform_error(_: Request, exc: PlatformError) -> JSONResponse:
        return error_response(exc)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    return app


app = create_app()
