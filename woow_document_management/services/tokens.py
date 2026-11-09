# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Share token generation, keyed digests, and password verification."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from passlib.context import CryptContext

from .constants import PASSWORD_MAX, PASSWORD_MIN

TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
MAX_LINK_LEN = 2048
_PASSWORD_CONTEXT = CryptContext(schemes=['pbkdf2_sha512'], deprecated='auto')


def new_token() -> str:
    """A new random share token (43 URL-safe characters)."""
    return secrets.token_urlsafe(32)


def token_hash(k_share: bytes, token: str) -> str:
    """HMAC-SHA256 hex digest of the token."""
    if not TOKEN_RE.fullmatch(token):
        raise ValueError('invalid share token')
    return hmac.new(k_share, token.encode('ascii'), hashlib.sha256).hexdigest()


def token_hint(k_hint: bytes, token: str) -> str:
    """Six hex characters that let managers tell links apart."""
    if not TOKEN_RE.fullmatch(token):
        raise ValueError('invalid share token')
    return hmac.new(k_hint, token.encode('ascii'), hashlib.sha256).hexdigest()[:6]


def verify(k_share: bytes, token: str, stored_hash: str) -> bool:
    """Constant-time comparison of the token against the stored hash."""
    if not isinstance(stored_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', stored_hash):
        return False
    try:
        candidate = token_hash(k_share, token)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(candidate, stored_hash)


def extract_code(raw: str) -> str | None:
    """Return the token from a POSTed token or pasted full link (ER-12), else None.

    Longer than MAX_LINK_LEN -> None; the text after the last '#' is used when present;
    stripped; must fullmatch TOKEN_RE. Anything before '#' is ignored.
    """
    if not isinstance(raw, str) or len(raw) > MAX_LINK_LEN:
        return None
    token = raw.rsplit('#', 1)[-1].strip()
    return token if TOKEN_RE.fullmatch(token) else None


def hash_password(password: str) -> str:
    """pbkdf2_sha512 hash; length outside PASSWORD_MIN..PASSWORD_MAX -> ValueError."""
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise ValueError('invalid password length')
    return _PASSWORD_CONTEXT.hash(password)


def check_password(password: str, stored: str) -> bool:
    """Verify a password against its stored hash."""
    if not isinstance(password, str) or not isinstance(stored, str) or \
            not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return False
    try:
        return bool(_PASSWORD_CONTEXT.verify(password, stored))
    except (ValueError, TypeError):
        return False
