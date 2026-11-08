# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Extension and MIME tables (IF-NAME, owned by upload-quota).

Pure Python. Day-0 stub: tables carry the values of the design (arch section 11),
functions are stubs.
"""
from __future__ import annotations

from collections.abc import Mapping

_OOXML = 'application/vnd.openxmlformats-officedocument.'
_ODF = 'application/vnd.oasis.opendocument.'

DEFAULT_EXTENSIONS: frozenset[str] = frozenset({
    'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'txt', 'csv', 'doc', 'docx', 'xls', 'xlsx',
    'ppt', 'pptx', 'odt', 'ods', 'odp', 'zip',
})
OPTIONAL_EXTENSIONS: frozenset[str] = frozenset({'mp4', 'mov', 'heic', 'rtf', '7z'})
BLOCKED_EXTENSIONS: frozenset[str] = frozenset({
    'html', 'htm', 'xhtml', 'svg', 'xml', 'js', 'mjs', 'exe', 'dll', 'bat', 'cmd', 'ps1', 'sh',
    'jar', 'msi', 'hta', 'vbs', 'scr', 'lnk', 'iso', 'swf',
})
EXT_TO_MIMES: Mapping[str, frozenset[str]] = {
    'pdf': frozenset({'application/pdf'}),
    'png': frozenset({'image/png'}),
    'jpg': frozenset({'image/jpeg'}),
    'jpeg': frozenset({'image/jpeg'}),
    'gif': frozenset({'image/gif'}),
    'webp': frozenset({'image/webp'}),
    'txt': frozenset({'text/plain'}),
    'csv': frozenset({'text/csv', 'text/plain'}),
    'doc': frozenset({'application/msword'}),
    'docx': frozenset({_OOXML + 'wordprocessingml.document'}),
    'xls': frozenset({'application/vnd.ms-excel'}),
    'xlsx': frozenset({_OOXML + 'spreadsheetml.sheet'}),
    'ppt': frozenset({'application/vnd.ms-powerpoint'}),
    'pptx': frozenset({_OOXML + 'presentationml.presentation'}),
    'odt': frozenset({_ODF + 'text'}),
    'ods': frozenset({_ODF + 'spreadsheet'}),
    'odp': frozenset({_ODF + 'presentation'}),
    'zip': frozenset({'application/zip'}),
    'mp4': frozenset({'video/mp4'}),
    'mov': frozenset({'video/quicktime'}),
    'heic': frozenset({'image/heic'}),
    'rtf': frozenset({'application/rtf', 'text/rtf'}),
    '7z': frozenset({'application/x-7z-compressed'}),
}
COMPRESSED_MIMES: frozenset[str] = frozenset({
    'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/zip', 'application/pdf',
    _OOXML + 'wordprocessingml.document', _OOXML + 'spreadsheetml.sheet',
    _OOXML + 'presentationml.presentation',
    _ODF + 'text', _ODF + 'spreadsheet', _ODF + 'presentation',
})


def acceptable_mimes(ext: str) -> frozenset[str]:
    """MIME types accepted for an extension (empty set when the extension is unknown)."""
    raise NotImplementedError  # WOOW-STUB


def is_compressed(mimetype: str) -> bool:
    """True when the type is already compressed (stored without deflate in ZIP files)."""
    raise NotImplementedError  # WOOW-STUB
