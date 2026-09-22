"""Small account credential value object used by the registration boundary."""

from __future__ import annotations

from dataclasses import dataclass

from .passwords import hash_password, normalize_username


@dataclass(frozen=True)
class AccountRegistration:
    username_original: str
    username_normalized: str
    password_hash: str
    display_name: str


def register_account(*, username: str, password: str) -> AccountRegistration:
    """Prepare account values; API layers copy the original username as display name."""

    prepared = normalize_username(username)
    return AccountRegistration(
        username_original=prepared.original,
        username_normalized=prepared.normalized,
        password_hash=hash_password(password),
        display_name=prepared.original,
    )
