# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""File and ZIP segment naming (IF-NAME, owned by upload-quota).

Pure Python.
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


class Deduper:
    """Make the names of one level unique one at a time (case-insensitive).

    ``add`` returns the name that ``dedupe`` gives at the same place of the same list: the
    first of the candidates ``name``, ``name (2)``, ``name (3)``, ... (before the extension,
    the stem cut to fit) that no earlier result has taken. Names that are already taken, such
    as a file written to the level by other means, are passed to the constructor.

    Names taken stay taken, so the number to try next is remembered, and a name costs a few
    fits however many names came before it (counting from 2 again for every name costs
    ``n * n`` fits for ``n`` names). It is remembered for all names that have the same numbered
    candidates, not only for names that are spelled the same: names that differ in case, names
    whose stems are cut to the same text, and names that are cut alike only from 10 on (the
    stem is cut one byte shorter at 10, 100, ...). The key is the stem as it is cut for the
    numbers with that many digits, and the extension, both compared case-insensitively. A
    candidate is in the list of at most two keys (with or without an extension), so the numbers
    a name skips were taken by other names, and each is skipped at most twice in all.
    """

    def __init__(self, taken: Sequence[str] = ()):
        self._seen = {name.casefold() for name in taken}
        # (digits, folded cut stem, folded extension) -> the first number with that many digits
        # that may be free (every smaller one of them is taken)
        self._next: dict[tuple[int, str, str], int] = {}

    def add(self, name: str) -> str:
        stem, dot, extension = name.rpartition('.')
        if not stem or not extension:
            stem, dot, extension = name, '', ''
        suffix = dot + extension
        candidate = _fit_segment(stem, '', suffix)
        folded = candidate.casefold()
        if folded not in self._seen:
            self._seen.add(folded)
            return candidate
        # ``cut`` is the stem as _fit_segment cuts it for the numbers with ``digits`` digits.
        # Each digit makes the number one byte longer, so each cut is the longest prefix of the
        # one before it that fits one byte less.
        cut = candidate[:len(candidate) - len(suffix)]
        size = len(cut.encode('utf-8'))
        room = SEGMENT_MAX_BYTES - len(suffix.encode('utf-8'))
        folded_suffix = suffix.casefold()
        digits = 1
        while True:
            room_for_digits = room - 3 - digits       # ' (' and ')' are 3 bytes
            if room_for_digits < 1:
                raise ValueError('extension is too long')
            while size > room_for_digits:
                size -= len(cut[-1].encode('utf-8'))
                cut = cut[:-1]
            key = (digits, cut.casefold(), folded_suffix)
            number = self._next.get(key, 2 if digits == 1 else 10 ** (digits - 1))
            while number < 10 ** digits:
                candidate = _fit_segment(stem, ' (%d)' % number, suffix)
                folded = candidate.casefold()
                if folded not in self._seen:
                    self._seen.add(folded)
                    self._next[key] = number + 1
                    return candidate
                number += 1
            self._next[key] = number
            digits += 1


def dedupe(names: Sequence[str]) -> list[str]:
    """Suffix ' (2)', ' (3)', ... to names repeated in one level (case-insensitive)."""
    deduper = Deduper()
    return [deduper.add(name) for name in names]


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
