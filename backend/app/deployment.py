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
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from urllib.parse import urlsplit

from app.db.settings import DEFAULT_APP_DSN, DEFAULT_WORKER_DSN
from app.identity.limits import MAX_SESSION_TTL

#: 仓库里公开的开发占位密钥。生产启动时只要还在用其中任何一个就拒绝启动。
DEV_SESSION_SECRET = "dev-only-session-secret-change-me"
DEV_COOKIE_SECRET = "dev-only-cookie-secret-change-me"
DEV_TOKEN_SECRET = "dev-only-placeholder-change-me"
_DEV_SECRETS = frozenset({DEV_SESSION_SECRET, DEV_COOKIE_SECRET, DEV_TOKEN_SECRET})

#: 生产密钥的最短字节数。HMAC 密钥的有效熵上限是其字节长度；
#: 一字节的密钥可以被在线穷举，任何"格式正确"的校验都救不了它。
#: 32 字节 = 256 位，与主流 HMAC-SHA256 推荐下限一致。
MIN_SECRET_BYTES = 32

#: 反代模式下信任的代理链（IP / CIDR，逗号分隔）。behind_proxy=1 时必须显式配置：
#: 没有可信代理清单，"信任 X-Forwarded-For" 就等于"信任任意客户端的自报家门"，
#: 限流键、CSRF Origin 全部可以被轮换伪造（审查实测：换一个 XFF 值即重置限流桶）。

#: 邀请兑换的默认限流：每个客户端键每个窗口允许的尝试次数。
DEFAULT_EXCHANGE_LIMIT = 20
DEFAULT_EXCHANGE_WINDOW_SECONDS = 600

#: 教学 provider 的合法开关值（闭集）。`scripted` 仅供测试/演练注入模拟器；
#: 真实云 provider 接入后在此追加（见 ADR-015）。空值 = disabled。
TEACHING_PROVIDER_CHOICES = frozenset({"disabled", "scripted", "openai"})

#: 教学输出的默认上限（token）。环境变量可调小；调大不受此默认值限制，
#: 但生产启动要求显式配置（见 configuration_problems）。
DEFAULT_TEACHING_MAX_INPUT_TOKENS = 8000
DEFAULT_TEACHING_MAX_OUTPUT_TOKENS = 2000
#: 单项目教学预算的默认值（微单位；1 micro = 计费货币的 10^-6）。
DEFAULT_TEACHING_PROJECT_BUDGET_MICRO = 5_000_000


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


def _env_optional_nonnegative_int(
    name: str, default: int | None, env: Mapping[str, str]
) -> int | None:
    """Parse an optional non-negative integer; ``unlimited`` means ``None``."""

    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"none", "null", "unlimited", "infinite"}:
        return None
    value = int(normalized)
    if value < 0:
        raise ValueError(f"{name} 不能为负")
    return value


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
    #: 可信代理链（IP/CIDR）。behind_proxy=True 时必须非空，否则拒绝启动。
    trusted_proxies: tuple[str, ...]
    invalid_proxies: tuple[str, ...]
    behind_proxy: bool
    session_ttl: timedelta
    exchange_limit: int
    exchange_window_seconds: int
    auth_rate_limit_capacity: int = 10_000
    #: DSN 是否由环境**显式**提供（区别于落到本机 trust 默认值）。
    dsn_explicitly_set: bool = False
    #: worker 角色的 DSN（`study_worker`）。默认 `None` = 由 `db.settings` 决定。
    worker_dsn: str | None = None
    worker_dsn_explicitly_set: bool = False
    # -------------------------------------------------------- 教学（第五轮）
    #: 教学 provider 开关。`disabled` 时教学端点显式报"功能未启用"，
    #: 绝不静默退回模拟器 —— "没有凭据还假装能用"比"明确不可用"糟糕得多。
    teaching_provider: str = "disabled"
    #: 批准的模型标识。只有这个值会出现在 ProviderRequest 里；
    #: 客户端无权选择模型（模型是服务端决策，不是请求参数）。
    teaching_model: str = ""
    #: 单次请求的输入/输出 token 上限（服务端强制，非模型自觉）。
    teaching_max_input_tokens: int = DEFAULT_TEACHING_MAX_INPUT_TOKENS
    teaching_max_output_tokens: int = DEFAULT_TEACHING_MAX_OUTPUT_TOKENS
    #: 单项目教学预算默认值（微单位）。租户/项目级预算落库后可按项目覆盖。
    teaching_project_budget_micro: int = DEFAULT_TEACHING_PROJECT_BUDGET_MICRO
    #: Provider transport settings are parsed once with the rest of deployment
    #: configuration. The secret is excluded from repr so diagnostics cannot
    #: accidentally print it.
    teaching_api_key: str = field(default="", repr=False)
    teaching_base_url: str = "https://api.openai.com/v1"
    # Open registration and password login are independent kill switches.
    # Missing environment variables deliberately resolve to False.
    registration_enabled: bool = False
    password_login_enabled: bool = False
    paid_dispatch_enabled: bool = False
    auth_argon2_max_concurrency: int = 2
    auth_argon2_queue_limit: int = 16
    auth_argon2_wait_seconds: float = 2.0
    #: ``None`` means no platform-wide monthly monetary rejection.  Accounting
    #: and per-request token/time limits remain active.
    platform_monthly_cap_micro: int | None = 0

    @property
    def is_production(self) -> bool:
        return self.mode is DeploymentMode.PRODUCTION

    @property
    def use_postgres(self) -> bool:
        return self.persistence is PersistenceKind.POSTGRES

    @property
    def teaching_api_key_present(self) -> bool:
        return bool(self.teaching_api_key)

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

        # 可信代理链：只做"形状"校验（非空、无空白），IP/CIDR 的语义校验在
        # 使用处（ipaddress 解析失败即视为不可信，不会放大权限）。
        # TestClient 的对端标识不是 IP（如 "testclient"），所以这里不能
        # 硬性要求 IP 格式 —— 那会把所有 API 测试拒之门外。
        trusted_proxies: list[str] = []
        invalid_proxies: list[str] = []
        for raw_proxy in env.get("STUDY_PLATFORM_TRUSTED_PROXIES", "").split(","):
            entry = raw_proxy.strip()
            if not entry:
                continue
            if any(ch.isspace() for ch in entry) or "," in entry:
                invalid_proxies.append(entry)
            else:
                trusted_proxies.append(entry)

        ttl_minutes = int(env.get("STUDY_PLATFORM_SESSION_TTL_MINUTES", "480"))
        exchange_limit = int(env.get("STUDY_PLATFORM_EXCHANGE_LIMIT", str(DEFAULT_EXCHANGE_LIMIT)))
        exchange_window = int(
            env.get(
                "STUDY_PLATFORM_EXCHANGE_WINDOW_SECONDS",
                str(DEFAULT_EXCHANGE_WINDOW_SECONDS),
            )
        )

        # 教学 provider：闭集校验放在**解析期**（拼错直接启动失败），
        # 与 STUY_PLATFORM_ENV 同一待遇 —— 拼错的 "diabled" 静默变成
        # "真实 provider 已启用"是最坏情况，不能留给启动自检兜底。
        teaching_provider = env.get("STUDY_PLATFORM_TEACHING_PROVIDER", "disabled").strip().lower()
        if teaching_provider and teaching_provider not in TEACHING_PROVIDER_CHOICES:
            raise RuntimeError(
                "STUDY_PLATFORM_TEACHING_PROVIDER 只能是 "
                f"{sorted(TEACHING_PROVIDER_CHOICES)} 之一，收到 {teaching_provider!r}"
            )

        return cls(
            mode=mode,
            persistence=persistence,
            dsn=env.get("STUDY_PLATFORM_DSN"),
            dsn_explicitly_set="STUDY_PLATFORM_DSN" in env,
            worker_dsn=env.get("STUDY_PLATFORM_WORKER_DSN"),
            worker_dsn_explicitly_set="STUDY_PLATFORM_WORKER_DSN" in env,
            teaching_provider=teaching_provider or "disabled",
            teaching_model=env.get("STUDY_PLATFORM_TEACHING_MODEL", "").strip(),
            teaching_max_input_tokens=int(
                env.get(
                    "STUDY_PLATFORM_TEACHING_MAX_INPUT_TOKENS",
                    str(DEFAULT_TEACHING_MAX_INPUT_TOKENS),
                )
            ),
            teaching_max_output_tokens=int(
                env.get(
                    "STUDY_PLATFORM_TEACHING_MAX_OUTPUT_TOKENS",
                    str(DEFAULT_TEACHING_MAX_OUTPUT_TOKENS),
                )
            ),
            teaching_project_budget_micro=int(
                env.get(
                    "STUDY_PLATFORM_TEACHING_PROJECT_BUDGET_MICRO",
                    str(DEFAULT_TEACHING_PROJECT_BUDGET_MICRO),
                )
            ),
            teaching_api_key=env.get("STUDY_PLATFORM_TEACHING_API_KEY", ""),
            teaching_base_url=env.get(
                "STUDY_PLATFORM_TEACHING_BASE_URL", "https://api.openai.com/v1"
            ).strip(),
            registration_enabled=_env_bool("STUDY_PLATFORM_REGISTRATION_ENABLED", env=env),
            password_login_enabled=_env_bool("STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED", env=env),
            paid_dispatch_enabled=_env_bool("STUDY_PLATFORM_PAID_DISPATCH_ENABLED", env=env),
            auth_argon2_max_concurrency=int(env.get("STUDY_PLATFORM_ARGON2_MAX_CONCURRENCY", "2")),
            auth_argon2_queue_limit=int(env.get("STUDY_PLATFORM_ARGON2_QUEUE_LIMIT", "16")),
            auth_argon2_wait_seconds=float(env.get("STUDY_PLATFORM_ARGON2_WAIT_SECONDS", "2")),
            platform_monthly_cap_micro=_env_optional_nonnegative_int(
                "STUDY_PLATFORM_MONTHLY_CAP_MICRO", 0, env
            ),
            session_secret=session_secret,
            cookie_secret=cookie_secret,
            cookie_previous_secrets=cookie_previous,
            token_secret=token_secret,
            cookie_secure=_env_bool("STUDY_PLATFORM_COOKIE_SECURE", env=env),
            trusted_origins=tuple(dict.fromkeys(trusted)),
            invalid_origins=tuple(invalid_origins),
            trusted_proxies=tuple(dict.fromkeys(trusted_proxies)),
            invalid_proxies=tuple(invalid_proxies),
            behind_proxy=_env_bool("STUDY_PLATFORM_BEHIND_PROXY", env=env),
            session_ttl=timedelta(minutes=ttl_minutes),
            exchange_limit=exchange_limit,
            exchange_window_seconds=exchange_window,
            auth_rate_limit_capacity=int(env.get("STUDY_PLATFORM_AUTH_RATE_LIMIT_CAPACITY", "10000")),
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
        if self.auth_rate_limit_capacity <= 0:
            problems.append("认证限流桶容量必须为正")
        if self.auth_argon2_max_concurrency <= 0:
            problems.append("Argon2 并发上限必须为正")
        if self.auth_argon2_queue_limit < 0:
            problems.append("Argon2 队列上限不能为负")
        if self.auth_argon2_wait_seconds <= 0:
            problems.append("Argon2 等待时限必须为正")
        if (
            self.platform_monthly_cap_micro is not None
            and self.platform_monthly_cap_micro < 0
        ):
            problems.append("平台月度额度不能为负")
        for raw in self.invalid_origins:
            problems.append(f"可信 Origin 无法解析（应为 scheme://host[:port]）：{raw!r}")
        for raw in self.invalid_proxies:
            problems.append(
                f"可信代理条目不能包含空白或逗号（应为 IP 或 CIDR）：{raw!r}"
            )

        # 教学配置的形状校验（任何模式都查 —— 数值荒谬的配置不该等上线才暴露）。
        if self.teaching_max_input_tokens <= 0 or self.teaching_max_output_tokens <= 0:
            problems.append(
                "教学输入/输出 token 上限必须为正"
                "（STUDY_PLATFORM_TEACHING_MAX_INPUT_TOKENS / "
                "STUDY_PLATFORM_TEACHING_MAX_OUTPUT_TOKENS）"
            )
        if self.teaching_project_budget_micro <= 0:
            problems.append(
                "教学项目预算必须为正微单位"
                "（STUDY_PLATFORM_TEACHING_PROJECT_BUDGET_MICRO）"
            )
        if self.teaching_provider == "openai":
            # 真实 provider：模型必须显式批准 + 凭据必须存在。
            # key 的值不进这里（秘密不进配置），只查存在性。
            if not self.teaching_model:
                problems.append(
                    "启用教学 provider 时必须显式配置 STUDY_PLATFORM_TEACHING_MODEL"
                    "（模型是服务端批准的服务端决策，不允许默认值）"
                )
            if not self.teaching_api_key_present:
                problems.append(
                    "启用教学 provider 时必须提供 STUDY_PLATFORM_TEACHING_API_KEY"
                    "（凭据只由服务端读取，不进仓库/日志/数据库）"
                )
        if self.behind_proxy and not self.trusted_proxies:
            problems.append(
                "开启 STUDY_PLATFORM_BEHIND_PROXY=1 必须同时配置 "
                "STUDY_PLATFORM_TRUSTED_PROXIES（可信代理 IP/CIDR 清单）："
                "没有可信清单时 X-Forwarded-For 完全由客户端可控，"
                "限流键与转发头都可被轮换伪造"
            )

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

        # worker 是**另一条凭据边界**，不是"应用连接的别名"：队列的跨租户
        # 可见性只授予 `study_worker`（0008 迁移的 `TO study_worker`），
        # 应用角色拿它认领不到任务。少了这条检查，生产会静默用本机无密码默认值
        # 去连 worker —— 那正是本模块存在的理由（配置缺失但服务照常启动）。
        if not self.worker_dsn_explicitly_set:
            problems.append(
                "生产模式必须显式提供 STUDY_PLATFORM_WORKER_DSN"
                "（worker 与 API 是两条凭据边界，不能共用一个角色）"
            )
        elif self.worker_dsn == DEFAULT_WORKER_DSN:
            problems.append(
                "生产模式的 STUDY_PLATFORM_WORKER_DSN 不能是本机开发默认值"
            )
        if self.worker_dsn and self.dsn and self.worker_dsn == self.dsn:
            problems.append(
                "STUDY_PLATFORM_WORKER_DSN 不能与 STUDY_PLATFORM_DSN 相同："
                "两者必须是不同角色的连接串（0008 把队列的跨租户权限只授予 worker 角色）"
            )

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
            elif len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
                problems.append(
                    f"{name} 至少需要 {MIN_SECRET_BYTES} 字节随机材料"
                    f"（当前 {len(secret.encode('utf-8'))} 字节）："
                    "短密钥可被在线穷举，格式校验救不了熵不足"
                )

        if self.cookie_secret == self.session_secret:
            problems.append(
                "cookie 签名密钥必须与会话令牌密钥分离（泄露隔离）："
                "请单独设置 STUDY_PLATFORM_COOKIE_SECRET"
            )

        # 全量交叉复用检查：session / cookie / token / 历史 cookie 密钥
        # 两两互不相同。任一用途泄漏都不应波及其他用途的凭证 ——
        # 只查 cookie != session 一对，挡不住 token_secret 复用 cookie 密钥。
        named = {
            "SESSION": self.session_secret,
            "COOKIE": self.cookie_secret,
            "TOKEN": self.token_secret,
        }
        for index, old in enumerate(self.cookie_previous_secrets):
            named[f"COOKIE_PREVIOUS[{index}]"] = old
        seen: dict[str, str] = {}
        for label, secret in named.items():
            if not secret:
                continue
            if secret in seen:
                problems.append(
                    f"{label} 密钥与 {seen[secret]} 密钥重复："
                    "所有用途的密钥必须两两互异（泄露隔离）"
                )
            else:
                seen[secret] = label
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
