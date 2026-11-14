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
import sys
import time
import unicodedata
import zlib

from odoo.tools import pdf as odoo_pdf

from .constants import PDF_MAX_OUTPUTS, PDF_MAX_PAGES

RANGES_MAX_CHARS = 1000        # the text a user types is cut off here before it is looked at

CODES = ('syntax', 'range', 'overlap', 'outputs', 'encrypted', 'damaged', 'pages', 'total')

# The page tree of a file is walked by ``_check_page_tree`` before the library flattens it. A tree
# with more nodes than this (a page counts as one) holds either too many pages or little else
# than empty branches, and is refused.
PAGE_TREE_NODES_MAX = 4 * PDF_MAX_PAGES
# Most pages one merge puts together. The library copies the pages of a merge in time that grows
# faster than the page count (about 4 s for 2,500 pages, 12 s for 5,000), so the total is capped
# even though every source is within ``PDF_MAX_PAGES``.
MERGE_MAX_PAGES = 5 * PDF_MAX_PAGES

# Limits on the raw bytes, checked before the library reads the file. The markers below drive the
# work the library does while it builds the reader (it follows the chain of earlier cross-reference
# sections, opens object streams, and searches the whole file when the table is broken). A file
# that carries more of them than any real file is refused as damaged.
PDF_MAX_STARTXREF = 4          # "startxref" keywords (one per saved revision of the file)
PDF_MAX_PREV = 16              # "/Prev" links to an earlier cross-reference section
PDF_MAX_XREF_STREAMS = 32      # "/XRef" cross-reference streams
PDF_MAX_OBJSTM = 512           # "/ObjStm" object streams
# Seconds of work the library may spend on one file (reading it, walking the page tree, counting
# the pages). It is checked at every call of Python code, so a loop in the library is cut off.
PDF_PARSE_SECONDS = 10.0
# Flate streams are inflated here, in bounded steps, before the library sees the file. One stream
# may decode to at most ``PDF_STREAM_MAX_BYTES``, all streams of a file to at most
# ``PDF_DECODED_MAX_BYTES``, and neither more than ``PDF_INFLATE_RATIO`` times the size of the
# file (a floor of ``PDF_INFLATE_FLOOR`` keeps tiny files usable).
PDF_STREAM_MAX_BYTES = 64 * 1024 * 1024
PDF_DECODED_MAX_BYTES = 256 * 1024 * 1024
PDF_INFLATE_RATIO = 250
PDF_INFLATE_FLOOR = 1024 * 1024
_INFLATE_STEP = 64 * 1024

_PREV = re.compile(rb'/Prev\s+([0-9]+)')
_STREAM = re.compile(rb'(?<![A-Za-z])stream\r?\n')

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


def _resolved(value):
    """The object behind an indirect reference (the method name differs between versions)."""
    getter = getattr(value, 'get_object', None) or getattr(value, 'getObject', None)
    return getter() if getter else value


def _check_page_tree(reader):
    """Refuse a page tree that is too large or shared, before the library flattens it.

    The library answers the number of pages by visiting every leaf of the tree, once for every
    way to reach it. A few hundred bytes can describe a tree whose nodes list the same child
    twice, to a depth where that is more leaves than the machine can count, so the count of
    the file (or of the root) cannot be asked first. The walk here visits a node at most once,
    stops after ``PAGE_TREE_NODES_MAX`` nodes, and refuses a node that is reached a second time
    (a cycle, or a branch shared on purpose). The tree that the library flattens afterwards is
    therefore a real tree of at most that size.
    """
    root = reader.trailer['/Root']['/Pages']
    stack = [root]
    seen = set()
    nodes = 0
    leaves = 0
    while stack:
        ref = stack.pop()
        idnum = getattr(ref, 'idnum', None)
        if idnum is not None:
            key = (idnum, getattr(ref, 'generation', 0))
            if key in seen:
                raise PdfToolError('damaged')
            seen.add(key)
        nodes += 1
        if nodes > PAGE_TREE_NODES_MAX:
            raise PdfToolError('damaged')
        node = _resolved(ref)
        if not hasattr(node, 'keys'):
            raise PdfToolError('damaged')
        if '/Kids' not in node:
            leaves += 1
            if leaves > PDF_MAX_PAGES:
                raise PdfToolError('pages', limit=PDF_MAX_PAGES)
            continue
        kids = _resolved(node['/Kids'])
        if not isinstance(kids, list):
            raise PdfToolError('damaged')
        if len(kids) > PAGE_TREE_NODES_MAX:
            raise PdfToolError('pages', limit=PDF_MAX_PAGES)
        stack.extend(reversed(kids))
        if len(stack) > PAGE_TREE_NODES_MAX:
            raise PdfToolError('damaged')


def _check_markers(data):
    """Refuse a file with more revisions, links or object streams than a real file has."""
    if (data.count(b'startxref') > PDF_MAX_STARTXREF or data.count(b'/Prev') > PDF_MAX_PREV
            or data.count(b'/XRef') > PDF_MAX_XREF_STREAMS
            or data.count(b'/ObjStm') > PDF_MAX_OBJSTM):
        raise PdfToolError('damaged')
    links = _PREV.findall(data)
    if len(set(links)) != len(links):
        raise PdfToolError('damaged')


def _check_inflated_size(data):
    """Refuse a file that holds a flate stream (or flate streams) that inflate far too much.

    Every stream that starts with a zlib header is inflated in steps of ``_INFLATE_STEP`` bytes
    and the output is only counted, never kept. The inflater itself finds the end of the stream,
    so a stream that hides its end marker is counted all the same. Anything that is not valid
    flate data is left to the library.
    """
    one_max = min(PDF_STREAM_MAX_BYTES, max(PDF_INFLATE_FLOOR, PDF_INFLATE_RATIO * len(data)))
    all_max = min(PDF_DECODED_MAX_BYTES, max(PDF_INFLATE_FLOOR, PDF_INFLATE_RATIO * len(data)))
    view = memoryview(data)
    total = 0
    for found in _STREAM.finditer(data):
        start = found.end()
        if start + 2 > len(data) or data[start] & 0x0f != 8 or (data[start] * 256 + data[start + 1]) % 31:
            continue
        inflater = zlib.decompressobj()
        size = 0
        position = start
        try:
            while position < len(data) and not inflater.eof:
                chunk = view[position:position + _INFLATE_STEP]
                position += len(chunk)
                piece = inflater.decompress(chunk, _INFLATE_STEP)
                while True:
                    size += len(piece)
                    if size > one_max or total + size > all_max:
                        raise PdfToolError('damaged')
                    tail = inflater.unconsumed_tail
                    if inflater.eof or (len(piece) < _INFLATE_STEP and not tail):
                        break
                    piece = inflater.decompress(tail, _INFLATE_STEP)
        except zlib.error:
            pass    # not flate data after all: the library decides what to do with it
        total += size


class _Timeout(BaseException):
    """Raised by ``_Deadline``; not an ``Exception``, so that the library cannot swallow it."""


class _Deadline:
    """Cuts off Python code that runs for more than ``seconds``; ``__exit__`` turns that into 'damaged'.

    It uses the profile hook of the running thread, which sees every call of a Python function, so
    it works in any thread (signals do not) and does not touch the timers that the server uses.
    Code that makes no call at all, or a single call into C code, is not interrupted: the sizes
    that C code (zlib) is asked to produce are limited by ``_check_inflated_size``.
    """

    def __init__(self, seconds):
        self.limit = time.monotonic() + seconds
        self.previous = None
        self.expired = False

    def _hook(self, frame, event, arg):
        if frame.f_code is self.__enter__.__code__:
            return      # the hook is installed in here: the first event is the end of this method
        if time.monotonic() > self.limit:
            self.expired = True
            raise _Timeout()

    def __enter__(self):
        self.previous = sys.getprofile()
        sys.setprofile(self._hook)
        return self

    def __exit__(self, exc_type, exc, tb):
        sys.setprofile(self.previous)
        if self.expired:
            raise PdfToolError('damaged') from None
        return False


def open_pdf(data):
    """``(reader, number of pages)`` of the PDF in ``data`` (bytes).

    Encrypted files, files that cannot be read, files without a page and files with more than
    ``PDF_MAX_PAGES`` pages are refused. The reader is lenient (``strict=False``), as the
    reader of Odoo's own merge is.
    """
    if not isinstance(data, bytes) or not data:
        raise PdfToolError('damaged')
    _check_markers(data)
    _check_inflated_size(data)
    try:
        with _Deadline(PDF_PARSE_SECONDS):
            reader = odoo_pdf.PdfReader(io.BytesIO(data), strict=False)
            if _encrypted(reader):
                raise PdfToolError('encrypted')
            _check_page_tree(reader)
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
