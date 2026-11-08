# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Share tokens and passwords (IF-TOKEN, owned by share).

Pure Python. Day-0 stub: TOKEN_RE and MAX_LINK_LEN are final, ``extract_code`` fails closed
(None), the rest raise NotImplementedError.
"""
from __future__ import annotations

import re

TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
MAX_LINK_LEN = 2048


def new_token() -> str:
    """A new random share token (43 URL-safe characters)."""
    raise NotImplementedError  # WOOW-STUB


def token_hash(k_share: bytes, token: str) -> str:
    """HMAC-SHA256 hex digest of the token."""
    raise NotImplementedError  # WOOW-STUB


def token_hint(k_hint: bytes, token: str) -> str:
    """Six hex characters that let managers tell links apart."""
    raise NotImplementedError  # WOOW-STUB


def verify(k_share: bytes, token: str, stored_hash: str) -> bool:
    """Constant-time comparison of the token against the stored hash."""
    raise NotImplementedError  # WOOW-STUB


def extract_code(raw: str) -> str | None:
    """Return the token from a POSTed token or pasted full link (ER-12), else None.

    Longer than MAX_LINK_LEN -> None; the text after the last '#' is used when present;
    stripped; must fullmatch TOKEN_RE. Anything before '#' is ignored.
    """
    return None  # WOOW-STUB


def hash_password(password: str) -> str:
    """pbkdf2_sha512 hash; length outside PASSWORD_MIN..PASSWORD_MAX -> ValueError."""
    raise NotImplementedError  # WOOW-STUB


def check_password(password: str, stored: str) -> bool:
    """Verify a password against its stored hash."""
    raise NotImplementedError  # WOOW-STUB
