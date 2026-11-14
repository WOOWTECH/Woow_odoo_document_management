# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Page ranges, and reading and writing PDF files for the split and merge wizards.

Only the PDF code that Odoo ships (``odoo.tools.pdf``) is used. Nothing here touches the
database or the caller's rights: the wizards decide who may read what, this module only
turns bytes into pages and back. Every failure is a ``PdfToolError`` with a ``code`` the
caller turns into a message, so no library exception ever reaches a user.
"""
from __future__ import annotations

import io
import re
import unicodedata

from odoo.tools import pdf as odoo_pdf

from .constants import PDF_MAX_OUTPUTS, PDF_MAX_PAGES

RANGES_MAX_CHARS = 1000        # the text a user types is cut off here before it is looked at

CODES = ('syntax', 'range', 'overlap', 'outputs', 'encrypted', 'damaged', 'pages')

# One part of the text: a page number, or two joined by a dash. ASCII digits only, at most six.
_PART = re.compile(r'\s*([0-9]{1,6})\s*(?:-\s*([0-9]{1,6})\s*)?', re.ASCII)
# Characters that people type for a comma or a dash, brought back to the ASCII ones.
_COMMAS = str.maketrans({'、': ',', ';': ',', '，': ',', '；': ','})
_DASHES = str.maketrans({'‐': '-', '‑': '-', '‒': '-', '–': '-',
                         '—': '-', '−': '-', '〜': '-', '~': '-'})


class PdfToolError(Exception):
    """A split or merge cannot go on; ``code`` is one of ``CODES``, ``params`` fills the message."""

    def __init__(self, code, **params):
        if code not in CODES:
            raise ValueError('unknown PDF tool code')
        super().__init__(code)
        self.code = code
        self.params = params


def parse_ranges(text, page_count):
    """The (first, last) page numbers, counted from 1, of each part of ``text``, in the order typed.

    ``text`` is a list such as ``1-3, 4, 5-8``. A part is one page or two pages joined by a
    dash; parts are separated by commas (semicolons and the full-width marks are accepted).
    Garbage, an empty part, a page below 1, a range that runs backwards, a page beyond
    ``page_count`` and two parts that share a page are all refused; so are more than
    ``PDF_MAX_OUTPUTS`` parts. The work is bounded by the length of ``text`` that is looked at.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > RANGES_MAX_CHARS:
        raise PdfToolError('syntax')
    cleaned = unicodedata.normalize('NFKC', text).translate(_COMMAS).translate(_DASHES)
    parts = cleaned.split(',')
    if len(parts) > PDF_MAX_OUTPUTS:
        raise PdfToolError('outputs', limit=PDF_MAX_OUTPUTS)
    result = []
    for part in parts:
        found = _PART.fullmatch(part)
        if not found:
            raise PdfToolError('syntax')
        first = int(found.group(1))
        last = int(found.group(2)) if found.group(2) else first
        if first < 1 or last < first or last > page_count:
            raise PdfToolError('range', pages=page_count)
        result.append((first, last))
    ordered = sorted(result)
    for before, after in zip(ordered, ordered[1:]):
        if after[0] <= before[1]:
            raise PdfToolError('overlap')
    return result


def single_pages(page_count):
    """One (page, page) part for every page; ``PDF_MAX_OUTPUTS`` is the most that is made."""
    if page_count > PDF_MAX_OUTPUTS:
        raise PdfToolError('outputs', limit=PDF_MAX_OUTPUTS)
    return [(number, number) for number in range(1, page_count + 1)]


def _encrypted(reader):
    """Whether the reader found an encryption dictionary (the name differs between versions)."""
    flag = getattr(reader, 'is_encrypted', None)
    return bool(reader.isEncrypted if flag is None else flag)


def open_pdf(data):
    """``(reader, number of pages)`` of the PDF in ``data`` (bytes).

    Encrypted files, files that cannot be read, files without a page and files with more than
    ``PDF_MAX_PAGES`` pages are refused. The reader is lenient (``strict=False``), as the
    reader of Odoo's own merge is.
    """
    if not isinstance(data, bytes) or not data:
        raise PdfToolError('damaged')
    try:
        reader = odoo_pdf.PdfReader(io.BytesIO(data), strict=False)
        if _encrypted(reader):
            raise PdfToolError('encrypted')
        count = len(reader.pages)
    except PdfToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - a damaged file can fail in any way the library has
        raise PdfToolError('damaged') from exc
    if count < 1:
        raise PdfToolError('damaged')
    if count > PDF_MAX_PAGES:
        raise PdfToolError('pages', limit=PDF_MAX_PAGES)
    return reader, count


def write_pdf(pages):
    """The bytes of a new PDF made of ``pages`` (page objects of readers from ``open_pdf``)."""
    try:
        writer = odoo_pdf.PdfFileWriter()
        add = getattr(writer, 'add_page', None) or writer.addPage
        for page in pages:
            add(page)
        stream = io.BytesIO()
        writer.write(stream)
        return stream.getvalue()
    except Exception as exc:  # noqa: BLE001 - a page that cannot be copied is a damaged file
        raise PdfToolError('damaged') from exc


def page_range(reader, first, last):
    """The page objects ``first`` to ``last`` (counted from 1, both included) of ``reader``."""
    try:
        pages = reader.pages
        return [pages[index] for index in range(first - 1, last)]
    except Exception as exc:  # noqa: BLE001 - the page tree of a damaged file fails here
        raise PdfToolError('damaged') from exc
