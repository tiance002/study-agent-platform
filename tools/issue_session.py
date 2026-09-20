"""签发会话令牌（运维动作，**不在 API 上暴露**）。

## 为什么没有「登录接口」

如果 API 上有个端点能按 `tenant_id` 签发令牌，那等于把「自报租户」换了个形式 ——
客户端照样能声称自己是任何租户，问题一点没解决。

令牌只能由服务端在**已知成员关系**的前提下签发。所以签发是运维 / 管理动作，
真实部署应替换为 OIDC / SAML，由身份提供方完成用户认证（`AuthProvider` 接口不变）。

## 用法

```bash
# 为演示租户的演示用户签发（默认 8 小时）
python tools/issue_session.py --tenant tenant_demo --principal user_demo

# 自定义有效期与展示名
python tools/issue_session.py --tenant t1 --principal u1 --ttl-minutes 60 --name "张三"

# 只看签发结果，不要提示
python tools/issue_session.py --tenant t1 --principal u1 --quiet
```

签名密钥取自环境变量 `STUDY_PLATFORM_SESSION_SECRET`，必须与服务端一致。
生产应改由 KMS / Secret Manager 提供。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.identity.session import SessionIssuer  # noqa: E402

DEFAULT_SECRET = "dev-only-session-secret-change-me"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="签发开发用会话令牌")
    parser.add_argument("--tenant", required=True, help="租户 ID")
    parser.add_argument("--principal", required=True, help="主体（用户）ID")
    parser.add_argument("--name", default="", help="展示名")
    parser.add_argument("--roles", default="", help="逗号分隔的角色，如 tenant_admin")
    parser.add_argument("--ttl-minutes", type=int, default=480)
    parser.add_argument("--quiet", action="store_true", help="只输出令牌本身")
    args = parser.parse_args(argv)

    secret = os.environ.get("STUDY_PLATFORM_SESSION_SECRET", DEFAULT_SECRET)
    issuer = SessionIssuer(secret=secret)
    token = issuer.issue(
        principal_id=args.principal,
        tenant_id=args.tenant,
        display_name=args.name,
        roles=tuple(r for r in args.roles.split(",") if r),
        issued_at=datetime.now(timezone.utc),
        ttl=timedelta(minutes=args.ttl_minutes),
    )
    raw = issuer.serialize(token)

    if args.quiet:
        print(raw)
        return 0

    print("签发完成")
    print(f"  租户：{args.tenant}")
    print(f"  主体：{args.principal}")
    print(f"  有效期至：{token.expires_at.isoformat()}")
    if secret == DEFAULT_SECRET:
        print("  ⚠️ 正在使用默认开发密钥；生产必须通过 STUDY_PLATFORM_SESSION_SECRET 注入")
    print()
    print("令牌：")
    print(raw)
    print()
    print("用法：")
    print(f'  curl -H "Authorization: Bearer {raw[:24]}..." http://127.0.0.1:8000/me')
    print()
    print("注意：本脚本只负责签名，**不校验成员关系**。")
    print("      请确认该主体确实是该租户成员，且已被授予目标项目（见 app/main.py 的种子数据）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
