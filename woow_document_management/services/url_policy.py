# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""URL validation for url-type documents (IF-URL, owned by core-models-acl).

Pure Python. Day-0 stub: everything is rejected.
"""
from __future__ import annotations


def validate(url: str, *, allow_mailto: bool = False) -> str:
    """Return the normalised URL or raise ValueError(code) (code maps to M-30)."""
    raise ValueError('stub')  # WOOW-STUB


def is_safe(url: str, *, allow_mailto: bool = False) -> bool:
    """True when ``validate`` accepts the URL."""
    return False  # WOOW-STUB
