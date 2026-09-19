"""部署模式与启动自检（fail-fast 的唯一入口）。

## 为什么需要显式环境模式

此前应用**永远**装配内存适配器、永远开启 Bearer 兼容通道、默认开发密钥
也能启动 —— 于是"数据库能力存在"和"生产真的在用数据库"之间没有任何
机械联系：配置缺失时服务照常启动，重启即丢登录态。

本模块把这件事收成一个显式判定：

- `STUDY_PLATFORM_ENV=development`（默认）：内存适配器、Bearer 兜底、
  演示种子 —— 零配置本机开发；
- `STUDY_PLATFORM_ENV=production`：PostgreSQL 持久化、仅 cookie 认证、
  密钥必须显式注入且互不相同、Secure cookie、可信 Origin 白名单、
  限流开启 —— **任何一项不满足，启动直接失败**，并一次性报出全部问题
  （不逐个挤牙膏）。

另外允许开发态显式 `STUDY_PLATFORM_PERSISTENCE=postgres` 装配数据库
（用于本机重启恢复演练）；production 下该变量被忽略，必须是 postgres。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from urllib.parse import urlsplit

from app.db.settings import DEFAULT_APP_DSN
from app.identity.limits import MAX_SESSION_TTL

#: 仓库里公开的开发占位密钥。生产启动时只要还在用其中任何一个就拒绝启动。
DEV_SESSION_SECRET = "dev-only-session-secret-change-me"
DEV_COOKIE_SECRET = "dev-only-cookie-secret-change-me"
DEV_TOKEN_SECRET = "dev-only-placeholder-change-me"
_DEV_SECRETS = frozenset({DEV_SESSION_SECRET, DEV_COOKIE_SECRET, DEV_TOKEN_SECRET})

#: 邀请兑换的默认限流：每个客户端键每个窗口允许的尝试次数。
DEFAULT_EXCHANGE_LIMIT = 20
DEFAULT_EXCHANGE_WINDOW_SECONDS = 600


class DeploymentMode(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class PersistenceKind(StrEnum):
    MEMORY = "memory"
    POSTGRES = "postgres"


def _env_bool(
    name: str, default: bool = False, env: Mapping[str, str] | None = None
) -> bool:
    # 标注成 Mapping 而不是 dict：调用方传进来的可能是 os.environ
    # （_Environ 不是 dict 子类），只要求"能按名字取值"才是真实的契约。
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_origin(raw: str) -> str | None:
    """把配置里的 Origin 规范化为 `scheme://host[:port]`（小写、无路径）。

    非法值返回 None —— 由调用方记为一条启动问题，
    而不是带着半对半错的白名单启动。
    """
    text = raw.strip().rstrip("/")
    if not text:
        return None
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


@dataclass(frozen=True)
class DeploymentSettings:
    """一次启动用到的全部部署相关配置，集中、可测、可注入。"""

    mode: DeploymentMode
    persistence: PersistenceKind
    dsn: str | None
    session_secret: str
    cookie_secret: str
    cookie_previous_secrets: tuple[str, ...]
    token_secret: str
    cookie_secure: bool
    trusted_origins: tuple[str, ...]
    invalid_origins: tuple[str, ...]
    behind_proxy: bool
    session_ttl: timedelta
    exchange_limit: int
    exchange_window_seconds: int
    #: DSN 是否由环境**显式**提供（区别于落到本机 trust 默认值）。
    dsn_explicitly_set: bool = False

    @property
    def is_production(self) -> bool:
        return self.mode is DeploymentMode.PRODUCTION

    @property
    def use_postgres(self) -> bool:
        return self.persistence is PersistenceKind.POSTGRES

    @classmethod
    def load(cls, environ: dict[str, str] | None = None) -> DeploymentSettings:
        """从环境变量读取（测试可注入字典）。**本方法只解析，不做生产判定。**"""
        env = os.environ if environ is None else environ

        raw_mode = env.get("STUDY_PLATFORM_ENV", DeploymentMode.DEVELOPMENT).strip().lower()
        if raw_mode not in {m.value for m in DeploymentMode}:
            # 未知模式不静默回退：拼错的 production 静默变成 development 是最坏情况。
            raise RuntimeError(
                f"STUDY_PLATFORM_ENV 只能是 development 或 production，收到 {raw_mode!r}"
            )
        mode = DeploymentMode(raw_mode)

        raw_persistence = env.get("STUDY_PLATFORM_PERSISTENCE", "").strip().lower()
        if raw_persistence and raw_persistence not in {p.value for p in PersistenceKind}:
            raise RuntimeError(
                f"STUDY_PLATFORM_PERSISTENCE 只能是 memory 或 postgres，收到 {raw_persistence!r}"
            )
        if mode is DeploymentMode.PRODUCTION:
            # 生产模式下持久化不是可选项，忽略该变量的任何其他取值。
            persistence = PersistenceKind.POSTGRES
        else:
            persistence = (
                PersistenceKind(raw_persistence) if raw_persistence else PersistenceKind.MEMORY
            )

        session_secret = env.get("STUDY_PLATFORM_SESSION_SECRET", DEV_SESSION_SECRET)
        # cookie 密钥缺省复用会话密钥（仅开发态少配一个变量）。
        cookie_secret = env.get("STUDY_PLATFORM_COOKIE_SECRET", session_secret)
        cookie_previous = tuple(
            part.strip()
            for part in env.get("STUDY_PLATFORM_COOKIE_SECRET_PREVIOUS", "").split(",")
            if part.strip()
        )
        token_secret = env.get("STUDY_PLATFORM_TOKEN_SECRET", DEV_TOKEN_SECRET)

        trusted: list[str] = []
        invalid_origins: list[str] = []
        for raw_origin in env.get("STUDY_PLATFORM_TRUSTED_ORIGINS", "").split(","):
            if not raw_origin.strip():
                continue
            normalized = _normalize_origin(raw_origin)
            if normalized is None:
                invalid_origins.append(raw_origin.strip())
            else:
                trusted.append(normalized)

        ttl_minutes = int(env.get("STUDY_PLATFORM_SESSION_TTL_MINUTES", "480"))
        exchange_limit = int(env.get("STUDY_PLATFORM_EXCHANGE_LIMIT", str(DEFAULT_EXCHANGE_LIMIT)))
        exchange_window = int(
            env.get(
                "STUDY_PLATFORM_EXCHANGE_WINDOW_SECONDS",
                str(DEFAULT_EXCHANGE_WINDOW_SECONDS),
            )
        )

        return cls(
            mode=mode,
            persistence=persistence,
            dsn=env.get("STUDY_PLATFORM_DSN"),
            dsn_explicitly_set="STUDY_PLATFORM_DSN" in env,
            session_secret=session_secret,
            cookie_secret=cookie_secret,
            cookie_previous_secrets=cookie_previous,
            token_secret=token_secret,
            cookie_secure=_env_bool("STUDY_PLATFORM_COOKIE_SECURE", env=env),
            trusted_origins=tuple(dict.fromkeys(trusted)),
            invalid_origins=tuple(invalid_origins),
            behind_proxy=_env_bool("STUDY_PLATFORM_BEHIND_PROXY", env=env),
            session_ttl=timedelta(minutes=ttl_minutes),
            exchange_limit=exchange_limit,
            exchange_window_seconds=exchange_window,
        )

    # ------------------------------------------------------------- 启动自检

    def configuration_problems(self) -> list[str]:
        """返回所有启动问题。空列表 = 可以启动。

        开发模式只校验"数值形状"（TTL、限流为正、Origin 合法）；
        生产模式执行完整清单。
        """
        problems: list[str] = []

        if self.session_ttl <= timedelta(0):
            problems.append("会话 TTL 必须为正（STUDY_PLATFORM_SESSION_TTL_MINUTES）")
        if self.session_ttl > MAX_SESSION_TTL:
            problems.append(
                f"会话 TTL 不得超过硬上限 {MAX_SESSION_TTL.days} 天"
                "（STUDY_PLATFORM_SESSION_TTL_MINUTES）"
            )
        if self.exchange_limit <= 0:
            problems.append("邀请兑换限流次数必须为正（STUDY_PLATFORM_EXCHANGE_LIMIT）")
        if self.exchange_window_seconds <= 0:
            problems.append(
                "邀请兑换限流窗口必须为正秒数（STUDY_PLATFORM_EXCHANGE_WINDOW_SECONDS）"
            )
        for raw in self.invalid_origins:
            problems.append(f"可信 Origin 无法解析（应为 scheme://host[:port]）：{raw!r}")

        if not self.is_production:
            return problems

        # ------------------------------------------------------- 生产专属
        if self.persistence is not PersistenceKind.POSTGRES:
            problems.append("生产模式必须使用 PostgreSQL 持久化")
        if not self.dsn_explicitly_set:
            problems.append(
                "生产模式必须显式提供 STUDY_PLATFORM_DSN（不允许使用本机 trust 默认连接串）"
            )
        elif self.dsn == DEFAULT_APP_DSN:
            problems.append("生产模式的 STUDY_PLATFORM_DSN 不能是本机开发默认值")

        for name, secret in (
            ("STUDY_PLATFORM_SESSION_SECRET", self.session_secret),
            ("STUDY_PLATFORM_COOKIE_SECRET", self.cookie_secret),
            ("STUDY_PLATFORM_TOKEN_SECRET", self.token_secret),
        ):
            if not secret:
                problems.append(f"{name} 不能为空")
            elif secret in _DEV_SECRETS:
                problems.append(
                    f"{name} 仍然是仓库内的开发占位密钥，生产必须由 KMS/Secret Manager 注入"
                )

        if self.cookie_secret == self.session_secret:
            problems.append(
                "cookie 签名密钥必须与会话令牌密钥分离（泄露隔离）："
                "请单独设置 STUDY_PLATFORM_COOKIE_SECRET"
            )
        if not self.cookie_secure:
            problems.append(
                "生产模式必须设置 STUDY_PLATFORM_COOKIE_SECURE=1（HTTPS-only cookie）"
            )
        if not self.trusted_origins:
            problems.append(
                "生产模式必须设置 STUDY_PLATFORM_TRUSTED_ORIGINS"
                "（逗号分隔的外部可信 Origin，即反代后的外部域名）"
            )
        if self.cookie_previous_secrets and any(
            old in _DEV_SECRETS for old in self.cookie_previous_secrets
        ):
            problems.append("STUDY_PLATFORM_COOKIE_SECRET_PREVIOUS 不允许包含开发占位密钥")
        return problems

    def validate_for_startup(self) -> None:
        """配置不完整时**一次性**抛出全部问题。"""
        problems = self.configuration_problems()
        if problems:
            joined = "\n  - ".join(problems)
            raise RuntimeError(
                f"启动自检失败（{self.mode.value}）：\n  - {joined}\n"
                "拒绝以不完整的安全配置启动。"
            )
