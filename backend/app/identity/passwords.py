"""Username and password credential primitives.

The validation rules in this module are deliberately independent of API models:
callers get the original username for display/storage and a normalized value for
exact lookup, while passwords are passed unchanged to Argon2id.

**单一密码策略**：注册、登录校验与重哈希全部使用同一组常量与同一个入口
`validate_password()`（6–12 码点）。历史上注册曾有一套更宽松的 6–12 规则、
登录/重哈希用 12–128，那个双分支已删除 —— 两套策略并存会让"注册能设的密码
登录时被拒"或"重哈希对合法密码抛错"。

Unicode 归一化行为由测试向量冻结（`tests/test_password_accounts.py`），
**不在导入期硬校验 `unicodedata.unidata_version`**：运行时的 Unicode 数据版本
会随 Python 小版本升级而变，硬失败会让整个服务在一个无害升级后无法启动，
而不是让行为漂移被测试抓住。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TypeGuard

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ARGON2_PARAMETERS = {
    "memory_cost": 19_456,
    "time_cost": 2,
    "parallelism": 1,
}
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 12

_USERNAME_ORIGINAL = re.compile(r"^[A-Za-z0-9_.\-\u3400-\u4DBF\u4E00-\u9FFF\uFF21-\uFF3A\uFF41-\uFF5A]+$")
_USERNAME_NORMALIZED = re.compile(r"^[A-Za-z0-9_.\-\u3400-\u4DBF\u4E00-\u9FFF]+$")
_USERNAME_FIRST = re.compile(r"^[A-Za-z\u3400-\u4DBF\u4E00-\u9FFF\uFF21-\uFF3A\uFF41-\uFF5A]")

_PASSWORD_HASHER = PasswordHasher(
    memory_cost=ARGON2_PARAMETERS["memory_cost"],
    time_cost=ARGON2_PARAMETERS["time_cost"],
    parallelism=ARGON2_PARAMETERS["parallelism"],
    type=Type.ID,
)
# A valid hash lets callers perform the same Argon2 work for unknown users.
DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("__dummy_password_value_never_accepted__")


@dataclass(frozen=True)
class Username:
    original: str
    normalized: str


def _validate_username_shape(value: str, *, original: bool) -> None:
    if not isinstance(value, str):
        raise ValueError("username must be a string")
    if not 1 <= len(value) <= 16:
        raise ValueError("username must contain 1 to 16 code points")
    if not (_USERNAME_ORIGINAL if original else _USERNAME_NORMALIZED).fullmatch(value):
        raise ValueError("username contains a disallowed character")
    if not _USERNAME_FIRST.match(value):
        raise ValueError("username must start with a letter or Han character")


def normalize_username(username: str) -> Username:
    """Validate original input, then NFKC+casefold and validate again."""

    _validate_username_shape(username, original=True)
    normalized = unicodedata.normalize("NFKC", username).casefold()
    _validate_username_shape(normalized, original=False)
    return Username(original=username, normalized=normalized)


def validate_password(password: object) -> None:
    """单一密码策略入口：注册、登录校验与重哈希共用它。

    规则：
    - 恰好 **6–12 个 Unicode 码点**（按码点计数，不做 strip/截断/大小写/NFKC
      归一化 —— 口令原样进 Argon2）；
    - 必须能编码为 UTF-8：含**孤立代理项**的字符串不是合法口令，
      必须在这里被拒绝成 `ValueError`，而不是让 Argon2 抛出未捕获的
      `UnicodeEncodeError`（那会把"口令非法"变成 500）。
    """

    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"password must contain {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} code points"
        )
    try:
        password.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("password must be valid UTF-8 (no lone surrogates)") from exc


def _is_argon2_safe_str(password: object) -> TypeGuard[str]:
    """True 表示这个值可以被安全地交给 Argon2（str 且能编码为 UTF-8）。"""
    if not isinstance(password, str):
        return False
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def hash_password(password: str) -> str:
    validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def _verify_hash(encoded_hash: str, password: str) -> bool:
    return _PASSWORD_HASHER.verify(encoded_hash, password)


def verify_password_diagnostic(password: object, encoded_hash: object) -> tuple[bool, str]:
    """Internal-safe result: reason names contain no password or hash material.

    长度与编码规则用**统一策略入口** `validate_password()`：登录校验与注册/重哈希
    共用同一条规则，所以"验证通过"必然意味着"可以安全地对它重哈希"。
    非法值一律返回 False，绝不向调用方抛出异常。
    """

    try:
        if not _is_argon2_safe_str(password):
            # 非字符串或非法 UTF-8：做一次等价 Argon2 工作（用空串代替，
            # 避免把不可编码的口令喂给 Argon2），失败原因不外泄。
            _verify_hash(DUMMY_PASSWORD_HASH, "")
            return False, "invalid_password"
        try:
            validate_password(password)
        except ValueError:
            _verify_hash(DUMMY_PASSWORD_HASH, password)
            return False, "invalid_password"
        target = encoded_hash if isinstance(encoded_hash, str) and encoded_hash.startswith("$argon2id$") else DUMMY_PASSWORD_HASH
        try:
            ok = _verify_hash(target, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            if target != DUMMY_PASSWORD_HASH:
                _verify_hash(DUMMY_PASSWORD_HASH, password)
            return False, "invalid_hash"
        return (ok, "ok" if ok else "mismatch")
    except (InvalidHashError, VerificationError, VerifyMismatchError, TypeError, ValueError):
        return False, "verification_error"


def verify_password(password: object, encoded_hash: object) -> bool:
    """Return false for every invalid/mismatched input without leaking detail."""

    return verify_password_diagnostic(password, encoded_hash)[0]


def needs_rehash(encoded_hash: str) -> bool:
    try:
        return _PASSWORD_HASHER.check_needs_rehash(encoded_hash)
    except (InvalidHashError, TypeError, ValueError):
        return True
