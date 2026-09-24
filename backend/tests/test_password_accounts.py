import unicodedata

import pytest
from app.identity.accounts import register_account
from app.identity.passwords import (
    ARGON2_PARAMETERS,
    DUMMY_PASSWORD_HASH,
    hash_password,
    needs_rehash,
    normalize_username,
    verify_password,
    verify_password_diagnostic,
)


@pytest.mark.parametrize(
    "username",
    [
        "",
        "a" * 17,
        "1alice",
        "_alice",
        "-alice",
        ".alice",
        "alice!",
        "alice中🙂",
        "１２alice",  # fullwidth digits are not compatibility input
        "Ａ" * 17,
    ],
)
def test_username_rejects_invalid_original_or_normalized_form(username):
    with pytest.raises(ValueError):
        normalize_username(username)


@pytest.mark.parametrize("username", ["alice", "A_1.-", "用户", "㐀用户", "Ａlice"])
def test_username_preserves_original_and_normalizes_for_lookup(username):
    result = normalize_username(username)
    assert result.original == username
    assert result.normalized == unicodedata.normalize("NFKC", username).casefold()
    assert 1 <= len(result.original) <= 16
    assert 1 <= len(result.normalized) <= 16


def test_username_normalized_form_is_revalidated_after_nfkc_casefold():
    # Kelvin sign is not in the original allowlist, and must not become an
    # accepted ASCII letter merely because normalization maps it to k.
    with pytest.raises(ValueError):
        normalize_username("K")


def test_unicode_data_version_is_explicit_and_stable():
    from app.identity.passwords import UNICODE_DATA_VERSION

    assert UNICODE_DATA_VERSION == unicodedata.unidata_version


@pytest.mark.parametrize("username", ["张", "张三", "张" * 16])
def test_username_fixed_valid_vectors(username):
    assert normalize_username(username).normalized == username


@pytest.mark.parametrize("username", ["张 ", "张\n", "张\u200b", "张\u202e"])
def test_username_fixed_invalid_whitespace_and_control_vectors(username):
    with pytest.raises(ValueError):
        normalize_username(username)


def test_fullwidth_ascii_letter_collides_with_ascii_lookup_value():
    assert normalize_username("Ａ").normalized == normalize_username("a").normalized == "a"


@pytest.mark.parametrize("password", ["p" * 11, "p" * 129, "密码" * 65])
def test_password_length_is_checked_as_original_codepoints(password):
    with pytest.raises(ValueError):
        hash_password(password)


def test_password_hash_uses_argon2id_and_frozen_parameters():
    password = "correct horse battery staple dev-only"
    encoded = hash_password(password)
    assert encoded.startswith("$argon2id$")
    assert ARGON2_PARAMETERS == {
        "memory_cost": 19456,
        "time_cost": 2,
        "parallelism": 1,
    }
    assert verify_password(password, encoded) is True
    assert verify_password("wrong", encoded) is False


def test_verify_password_has_uniform_false_for_malformed_or_missing_hash():
    assert verify_password("password", "not-a-hash") is False
    assert verify_password("password", None) is False
    assert verify_password("password", DUMMY_PASSWORD_HASH) is False


def test_verify_password_uses_dummy_hash_for_missing_or_malformed_hash(monkeypatch):
    import app.identity.passwords as passwords

    seen = []
    original_verify = passwords._verify_hash

    def recording_verify(encoded_hash, password):
        seen.append(encoded_hash)
        return original_verify(encoded_hash, password)

    monkeypatch.setattr(passwords, "_verify_hash", recording_verify)
    assert verify_password("password-123", None) is False
    assert verify_password("password-123", "not-a-hash") is False
    assert seen == [DUMMY_PASSWORD_HASH, DUMMY_PASSWORD_HASH]
    assert verify_password_diagnostic("password-123", "not-a-hash") == (False, "invalid_hash")


def test_password_preserves_leading_trailing_space_and_unicode_codepoints():
    password = "  密码密码abcd dev-only  "
    encoded = hash_password(password)
    assert verify_password(password, encoded) is True
    assert verify_password(password.strip(), encoded) is False


def test_needs_rehash_exposes_argon2_check():
    encoded = hash_password("password-123")
    assert needs_rehash(encoded) is False
    assert needs_rehash("not-a-hash") is True


def test_registration_takes_username_and_password_only_and_copies_display_name():
    account = register_account(username="Alice_1", password="password-123-dev-only")
    assert account.username_original == "Alice_1"
    assert account.username_normalized == "alice_1"
    assert account.display_name == "Alice_1"
    with pytest.raises(TypeError):
        register_account(username="Alice_1", password="password-123-dev-only", display_name="other")
