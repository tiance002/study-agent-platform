"""Start an in-memory browser preview with one disposable invitation."""

from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.identity.ports import SystemContext
from app.main import DEMO_PRINCIPAL, DEMO_TENANT, build_platform, create_app


def main() -> None:
    token = "round7-preview-invite"
    platform = build_platform(var_dir=Path("var") / "round7-preview")
    now = platform.clock.now()
    platform.invitations.issue(
        SystemContext(DEMO_TENANT, "round 7 browser preview"),
        invitation_id="inv_round7_preview",
        token_hash="sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        issued_by=DEMO_PRINCIPAL,
        invitee_principal_id=DEMO_PRINCIPAL,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    uvicorn.run(create_app(platform=platform), host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
