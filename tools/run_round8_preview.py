"""Start a disposable in-memory preview for the round 8 browser checks."""

from __future__ import annotations

import argparse
import hashlib
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.deployment import DeploymentSettings
from app.identity.ports import SystemContext
from app.main import DEMO_TENANT, build_platform, create_app
from app.teaching.models import RawCitation
from app.teaching.provider import completed_result
from app.workers.ingestion import run_once as run_ingestion_once
from app.workers.teaching import run_once as run_teaching_once


class PreviewProvider:
    """确定性 provider：引用第一段真实进入上下文的资料。"""

    def generate(self, request):
        citations = ()
        if request.artifacts:
            artifact = request.artifacts[0]
            citations = (
                RawCitation(
                    source_id=artifact.source_id,
                    document_id=artifact.document_id,
                    span_start=artifact.span_start,
                    span_end=artifact.span_end,
                    content_hash=artifact.content_hash,
                ),
            )
        return completed_result(
            attempt_id=request.attempt_id,
            answer="阅读资料后给出可执行的学习解释。",
            citations=citations,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--invite", default="round8-preview-invite")
    parser.add_argument("--var-dir", default="var/round8-preview")
    args = parser.parse_args()

    platform = build_platform(
        var_dir=Path(args.var_dir),
        settings=DeploymentSettings.load(
            {
                "STUDY_PLATFORM_REGISTRATION_ENABLED": "true",
                "STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED": "true",
            }
        ),
    )
    platform.teaching_provider = PreviewProvider()
    now = platform.clock.now()
    platform.invitations.issue(
        SystemContext(DEMO_TENANT, "round 8 browser preview"),
        invitation_id="inv_round8_preview",
        token_hash="sha256:" + hashlib.sha256(args.invite.encode()).hexdigest(),
        issued_by="round8-preview-issuer",
        invitee_principal_id="round8-preview-user",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )

    def workers() -> None:
        while True:
            try:
                run_ingestion_once(platform, worker_id="round8-preview-ingestion")
                run_teaching_once(platform, worker_id="round8-preview-teaching")
            except Exception as error:  # pragma: no cover - preview diagnostics only
                print(f"round8 preview worker error: {error}", file=sys.stderr)
            time.sleep(0.1)

    threading.Thread(target=workers, name="round8-preview-workers", daemon=True).start()
    uvicorn.run(create_app(platform=platform), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
