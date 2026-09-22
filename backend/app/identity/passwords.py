"""Username and password credential primitives.

The validation rules in this module are deliberately independent of API models:
callers get the original username for display/storage and a normalized value for
exact lookup, while passwords are passed unchanged to Argon2id.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

UNICODE_DATA_VERSION = "15.0.0"
if unicodedata.unidata_version != UNICODE_DATA_VERSION:
    raise RuntimeError(
        f"unsupported Unicode data version {unicodedata.unidata_version}; "
        f"expected {UNICODE_DATA_VERSION}"
    )
ARGON2_PARAMETERS = {
    "memory_cost": 19_456,
    "time_cost": 2,
    "parallelism": 1,
}
MIN_PASSWORD_CODEPOINTS = 12
MAX_PASSWORD_CODEPOINTS = 128

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


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if not MIN_PASSWORD_CODEPOINTS <= len(password) <= MAX_PASSWORD_CODEPOINTS:
        raise ValueError("password must contain 12 to 128 code points")


def hash_password(password: str) -> str:
    _validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def _verify_hash(encoded_hash: str, password: str) -> bool:
    return _PASSWORD_HASHER.verify(encoded_hash, password)


def verify_password_diagnostic(password: object, encoded_hash: object) -> tuple[bool, str]:
    """Internal-safe result: reason names contain no password or hash material."""

    try:
        if not isinstance(password, str) or not MIN_PASSWORD_CODEPOINTS <= len(password) <= MAX_PASSWORD_CODEPOINTS:
            _verify_hash(DUMMY_PASSWORD_HASH, password if isinstance(password, str) else "")
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
