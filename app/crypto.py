"""Symmetric encryption for sensitive credentials stored in the DB (today: the
DCI portal password).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with the `FIELD_ENCRYPTION_KEY` key, a
DEDICATED Fly secret (separate from SECRET_KEY, see acumenauto/settings.py). If
the key is missing or malformed, encryption is disabled: `is_available()` returns
False and encrypt/decrypt raise `EncryptionUnavailable` with a clear message. The
app still boots (settings.py reads the key with .get) and the /settings UI warns.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class EncryptionUnavailable(RuntimeError):
    """FIELD_ENCRYPTION_KEY is unset or malformed: cannot encrypt/decrypt."""


def _build_fernet(key) -> Fernet:
    return Fernet(key.encode() if isinstance(key, str) else key)


def is_available() -> bool:
    """True only if the key exists AND is a valid Fernet key (so the save path
    degrades cleanly instead of 500-ing on a malformed key)."""
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        return False
    try:
        _build_fernet(key)
        return True
    except (ValueError, TypeError):
        return False


def _fernet() -> Fernet:
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        raise EncryptionUnavailable(
            "FIELD_ENCRYPTION_KEY is not set — cannot store/read the encrypted "
            "portal password."
        )
    try:
        return _build_fernet(key)
    except (ValueError, TypeError) as exc:
        raise EncryptionUnavailable(
            "FIELD_ENCRYPTION_KEY is malformed — it must be a Fernet key "
            "(32 url-safe base64 bytes); do not reuse SECRET_KEY."
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a string and return the token (str) to store in the DB."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a stored token. Returns '' if the token is invalid (rotated key /
    corrupt data) so the caller degrades to the env fallback without breaking."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""
