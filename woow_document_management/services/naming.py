# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""File and ZIP segment naming (IF-NAME, owned by upload-quota).

Pure Python. Day-0 stub: constants are final, functions are stubs.
"""
from __future__ import annotations

from collections.abc import Sequence

WINDOWS_RESERVED: frozenset[str] = frozenset(
    {'CON', 'PRN', 'AUX', 'NUL'}
    | {'COM%d' % i for i in range(1, 10)}
    | {'LPT%d' % i for i in range(1, 10)}
)   # compared case-insensitively


def sanitize_filename(name: str) -> str:
    """Return a safe display / download file name.

    Basename only ('/' and '\\' are both separators), NFC, without control, bidi, ':' or NUL
    characters, without leading dots, spaces or '-'; at most FILENAME_MAX_CHARS characters with
    the extension kept; Windows reserved names get a '_' prefix. Empty result -> ValueError.
    """
    raise NotImplementedError  # WOOW-STUB


def sanitize_segment(name: str, empty_label: str) -> str:
    """Return one ZIP path segment.

    NFC; strips '/', '\\', NUL, C0/C1, U+202A-202E and U+2066-2069; drops '.', '..', leading
    dots and spaces; utf-8 length <= SEGMENT_MAX_BYTES; empty -> ``empty_label`` (already
    translated by the caller).
    """
    raise NotImplementedError  # WOOW-STUB


def dedupe(names: Sequence[str]) -> list[str]:
    """Suffix ' (2)', ' (3)', ... to names repeated in one level (case-insensitive)."""
    raise NotImplementedError  # WOOW-STUB
