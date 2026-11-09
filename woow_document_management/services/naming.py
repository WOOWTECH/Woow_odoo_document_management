# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""File and ZIP segment naming (IF-NAME, owned by upload-quota).

Pure Python. Day-0 stub: constants are final, functions are stubs.
"""
from __future__ import annotations

from collections.abc import Sequence
import unicodedata

from .constants import FILENAME_MAX_CHARS, SEGMENT_MAX_BYTES

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
    if not isinstance(name, str):
        raise ValueError('invalid filename')
    base = unicodedata.normalize('NFC', name.replace('\\', '/').rsplit('/', 1)[-1])
    base = ''.join(ch for ch in base if ch != ':' and not _unsafe(ch))
    base = base.lstrip('. -')
    if not base:
        raise ValueError('empty filename')
    stem, dot, extension = base.rpartition('.')
    if not stem or not extension:
        stem, dot, extension = base, '', ''
    if stem.upper() in WINDOWS_RESERVED:
        stem = '_' + stem
    suffix = dot + extension
    if len(suffix) >= FILENAME_MAX_CHARS:
        raise ValueError('extension is too long')
    return stem[:FILENAME_MAX_CHARS - len(suffix)] + suffix


def sanitize_segment(name: str, empty_label: str) -> str:
    """Return one ZIP path segment.

    NFC; strips '/', '\\', NUL, C0/C1, U+202A-202E and U+2066-2069; drops '.', '..', leading
    dots and spaces; utf-8 length <= SEGMENT_MAX_BYTES; empty -> ``empty_label`` (already
    translated by the caller).
    """
    value = unicodedata.normalize('NFC', str(name or ''))
    value = ''.join(ch for ch in value if ch not in '/\\' and not _unsafe(ch))
    value = value.lstrip('. ')
    if not value:
        value = str(empty_label)
    out = []
    size = 0
    for ch in value:
        width = len(ch.encode('utf-8'))
        if size + width > SEGMENT_MAX_BYTES:
            break
        out.append(ch)
        size += width
    return ''.join(out) or str(empty_label)[:SEGMENT_MAX_BYTES]


def dedupe(names: Sequence[str]) -> list[str]:
    """Suffix ' (2)', ' (3)', ... to names repeated in one level (case-insensitive)."""
    seen = set()
    result = []
    for name in names:
        stem, dot, extension = name.rpartition('.')
        if not stem or not extension:
            stem, dot, extension = name, '', ''
        suffix = dot + extension
        candidate = _fit_segment(stem, '', suffix)
        index = 2
        while candidate.casefold() in seen:
            candidate = _fit_segment(stem, ' (%d)' % index, suffix)
            index += 1
        seen.add(candidate.casefold())
        result.append(candidate)
    return result


def _fit_segment(stem: str, number: str, extension: str) -> str:
    fixed = number + extension
    budget = SEGMENT_MAX_BYTES - len(fixed.encode('utf-8'))
    if budget < 1:
        raise ValueError('extension is too long')
    out = []
    size = 0
    for ch in stem:
        width = len(ch.encode('utf-8'))
        if size + width > budget:
            break
        out.append(ch)
        size += width
    return ''.join(out) + fixed


def _unsafe(ch: str) -> bool:
    point = ord(ch)
    return (unicodedata.category(ch).startswith('C') or 0x202A <= point <= 0x202E
            or 0x2066 <= point <= 0x2069)
