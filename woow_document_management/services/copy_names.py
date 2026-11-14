# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Name of a duplicated document. Pure Python."""
from __future__ import annotations

from .constants import FILENAME_MAX_CHARS

_EXTENSION_MAX = 16


def copy_name(name: str, suffix: str, keep_extension: bool = True) -> str:
    """``name`` with ``suffix`` added before the extension, at most FILENAME_MAX_CHARS long.

    A file keeps its extension (``report.pdf`` -> ``report (copy).pdf``), so the copy passes the
    same name rules as the original; a name without a usable extension, and a link, get the suffix
    at the end. When the result would be too long the stem is cut, never the suffix or the
    extension.
    """
    name = str(name or '').strip()
    suffix = str(suffix or '').strip()
    stem, dot, extension = name.rpartition('.')
    if keep_extension and stem and extension and len(extension) <= _EXTENSION_MAX \
            and not any(ch.isspace() for ch in extension):
        tail = ' ' + suffix + dot + extension
    else:
        stem, tail = name, ' ' + suffix
    room = max(FILENAME_MAX_CHARS - len(tail), 1)
    return stem[:room].rstrip() + tail
