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

CODES = ('syntax', 'range', 'overlap', 'outputs', 'encrypted', 'damaged', 'too_large', 'pages',
         'total')

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
PDF_MAX_STARTXREF = 32         # "startxref" keywords (one per saved revision of the file)
PDF_MAX_PREV = 16              # "/Prev" links inside trailers / cross-reference stream dictionaries
PDF_MAX_XREF_STREAMS = 32      # "/Type /XRef" cross-reference streams
PDF_MAX_OBJSTM = 4096          # "/Type /ObjStm" object streams (a big file has one per ~100 objects)
# Seconds of work the library may spend on one file (reading it, walking the page tree, counting
# the pages). It is checked at every call of Python code, so a loop in the library is cut off.
PDF_PARSE_SECONDS = 10.0
# Most "/Prev" keys of any kind that are looked at; a file with more is refused as damaged (a long
# outline has thousands of them, the cost of each one is a constant).
PDF_MAX_PREV_KEYS = 100000
# Flate streams are inflated here, in bounded steps, before the library sees the file. The caps grow
# with the size of the file, because a bomb is a small file that decodes to a lot, and a scan is a
# large file whose images decode to a lot more than the pages suggest. All streams of a file may
# decode to at most the smallest of ``PDF_DECODED_MAX_BYTES``, ``PDF_INFLATE_RATIO`` times the size
# of the file (a floor of ``PDF_INFLATE_FLOOR`` keeps tiny files usable) and the larger of
# ``PDF_DECODED_BASE`` plus ``PDF_DECODED_PER_PAGE`` for every page the file shows (a page that
# stands as text) and ``PDF_DECODED_BASE`` plus ``PDF_DECODED_PER_BYTE`` times the size of the file
# (pages inside object streams are not seen). One stream may decode to at most the larger of
# ``PDF_STREAM_MAX_BYTES`` and ``PDF_DECODED_PER_BYTE`` times the size of the file, and never more
# than all streams together. More than the larger of ``PDF_MAX_FLATE_STREAMS`` and one stream for
# every ``PDF_BYTES_PER_STREAM`` bytes of the file is refused, and so is a file whose streams make
# the check read more than ``PDF_INFLATE_INPUT_RATIO`` times its size: the check itself is bounded
# work. These refusals are about size (code ``too_large``), not about a damaged file.
PDF_STREAM_MAX_BYTES = 128 * 1024 * 1024
PDF_DECODED_MAX_BYTES = 2048 * 1024 * 1024
PDF_DECODED_BASE = 64 * 1024 * 1024
PDF_DECODED_PER_PAGE = 48 * 1024 * 1024
PDF_DECODED_PER_BYTE = 40
PDF_INFLATE_RATIO = 250
PDF_INFLATE_FLOOR = 1024 * 1024
PDF_MAX_FLATE_STREAMS = 50000
PDF_BYTES_PER_STREAM = 64
PDF_INFLATE_INPUT_RATIO = 4
_INFLATE_STEP = 64 * 1024

_PREV = re.compile(rb'/Prev\s+([0-9]+)')
_XREF_TYPE = re.compile(rb'/Type\s*/XRef(?![A-Za-z])')
_OBJSTM_TYPE = re.compile(rb'/Type\s*/ObjStm(?![A-Za-z])')
_BOUNDARY = re.compile(rb'(?<![A-Za-z])(?:obj|trailer)(?![A-Za-z])')
_PREV_LOOKBACK = 4096
_LAST_BOUNDARY = re.compile(rb'.*(?<![A-Za-z])(obj|trailer)(?![A-Za-z])', re.DOTALL)
_PAGE_TYPE = re.compile(rb'/Type\s*/Page(?![A-Za-z])')
# The keyword that starts the data of a stream, as the library accepts it: spaces, then an end of line.
_STREAM = re.compile(rb'(?<![A-Za-z])stream[ ]*(?:\r\n|\r|\n)')


def _spelled(text):
    """A pattern for the name ``text`` (without its slash) in any spelling the library reads.

    The library reads ``#xx`` inside a name as the character with that hex code, so a name can be
    written with any of its letters escaped. The pattern accepts a letter or its escape (digits of
    the code in either case) and ends the name at a delimiter or a space.
    """
    parts = []
    for char in text:
        code = ord(char)
        forms = {char, '#%02x' % code, '#%02X' % code}
        parts.append('(?:%s)' % '|'.join(re.escape(form) for form in sorted(forms)))
    return rb'/' + ''.join(parts).encode() + rb'(?![^\s/<>\[\](){}%])'


_HEX_ESCAPE = re.compile(rb'#([0-9A-Fa-f]{2})')
_NAME_END = rb'(?![^\s/<>\[\](){}%])'
# The values of a stream's "/Filter" that cannot do harm while the library decodes the stream: one
# flate filter, or one of the filters that make no more data than they are given (or that the
# library does not decode at all), as a name or as an array of that one name.
_SAFE_FILTERS = (rb'(?:FlateDecode|Fl|DCTDecode|DCT|JPXDecode|CCITTFaxDecode|CCF|JBIG2Decode'
                 rb'|RunLengthDecode|RL|ASCII85Decode|A85|ASCIIHexDecode|AHx|Crypt)' + _NAME_END)
_HAZARDOUS_FILTER = re.compile(
    _spelled('Filter') + rb'(?!\s*(?:/' + _SAFE_FILTERS + rb'|\[\s*(?:/' + _SAFE_FILTERS
    + rb'\s*)?\]))')
# What makes the library decode a stream while it opens a file: a "/Type" that is "/XRef" or
# "/ObjStm", or that is not written as a name (a reference may stand for any value).
_DECODED_TYPE = re.compile(_spelled('Type') + rb'\s*(?:' + _spelled('XRef') + rb'|' + _spelled('ObjStm')
                           + rb'|[0-9]+\s+[0-9]+\s+R)')
# How far apart (bytes) a "/Type" and a "/Filter" may be and still belong to one dictionary.
PDF_DICTIONARY_REACH = 1024 * 1024
_DECODED_POSITIONS_MAX = 2 * (PDF_MAX_XREF_STREAMS + PDF_MAX_OBJSTM)
# The sections of the chain of cross-reference sections ("startxref", "/Prev", "/XRefStm").
_STARTXREF = re.compile(rb'startxref\s*([0-9]{1,18})')
_SPACE = re.compile(rb'[\x00\t\n\x0c\r ]*')
_OBJ_HEADER = re.compile(rb'[0-9]+\s+[0-9]+\s+obj')
_PREV_VALUE = re.compile(rb'/Prev\s+([0-9]{1,18})')
_XREFSTM_VALUE = re.compile(rb'/XRefStm\s+([0-9]{1,18})')
_SECTION_WINDOW = 64 * 1024


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


def _count_up_to(pattern, data, limit):
    """How many matches of ``pattern`` there are in ``data``, counting no further than ``limit + 1``."""
    count = 0
    for _found in pattern.finditer(data):
        count += 1
        if count > limit:
            break
    return count


def _chain_links(data):
    """The values of "/Prev" keys that continue the chain of cross-reference sections.

    Only a "/Prev" in a trailer dictionary or in the dictionary of a cross-reference stream
    ("/Type /XRef") makes the library walk to an earlier section. The same key is also used, with
    an object reference, by outline (bookmark) entries, structure elements and the like, and a
    normal document has hundreds of them, so those are not counted. A key counts when the closest
    "trailer" or "obj" keyword before it is "trailer", or when the object it sits in (up to its
    "endobj" or "stream") has "/Type /XRef". A key that cannot be placed is counted.

    The work is linear in the size of the file: the keys are found in one pass, and the keyword
    before a key is looked for only between the previous key and this one (at most the last
    ``_PREV_LOOKBACK`` bytes of that stretch), so no byte is read twice. More than
    ``PDF_MAX_PREV_KEYS`` keys is refused as damaged.
    """
    links = []
    keys = 0
    last_end = 0           # where the previous key ends: the stretch of this key starts here
    last_counted = True    # what was decided for the previous key
    for found in _PREV.finditer(data):
        keys += 1
        if keys > PDF_MAX_PREV_KEYS:
            raise PdfToolError('damaged')
        pos = found.start()
        window_start = max(last_end, pos - _PREV_LOOKBACK)
        before = data[window_start:pos]
        marker = _LAST_BOUNDARY.match(before)
        if marker is not None:
            counted = True
            if marker.group(1) == b'obj':
                region = before[marker.end(1):] + data[pos:pos + 512]
                skip = len(before) - marker.end(1)
                for stop in (b'endobj', b'stream'):
                    cut = region.find(stop, skip)
                    if cut >= 0:
                        region = region[:cut]
                counted = bool(_XREF_TYPE.search(region))
        elif last_end and window_start == last_end:
            counted = last_counted     # no keyword since the previous key: the same object
        else:
            counted = True
        last_end = found.end()
        last_counted = counted
        if counted:
            links.append(found.group(1))
    return links


def _section_links(data, offset):
    """The offsets that the section at ``offset`` leads to, or ``None`` if no section starts there.

    A section is a cross-reference table (its "trailer" dictionary is read) or the object of a
    cross-reference stream (its dictionary is read). The links are the "/Prev" values and, for a
    hybrid file, the "/XRefStm" values: ``([prev, ...], [xrefstm, ...])``. Only a window of
    ``_SECTION_WINDOW`` bytes after the keyword is looked at, so the cost is one search.
    """
    size = len(data)
    pos = _SPACE.match(data, offset).end()
    if data.startswith(b'xref', pos):
        keyword = data.find(b'trailer', pos)
        if keyword < 0:
            return None
        end = min(size, keyword + _SECTION_WINDOW)
        cut = data.find(b'startxref', keyword, end)
        text = data[keyword:end if cut < 0 else cut]
    elif _OBJ_HEADER.match(data, pos):
        end = min(size, pos + _SECTION_WINDOW)
        cut = data.find(b'stream', pos, end)
        text = data[pos:end if cut < 0 else cut]
        if b'#' in text:
            text = _HEX_ESCAPE.sub(_unescaped, text)
        if not _XREF_TYPE.search(text):
            return None
    else:
        return None
    return ([int(value) for value in _PREV_VALUE.findall(text)],
            [int(value) for value in _XREFSTM_VALUE.findall(text)])


def _check_xref_chain(data):
    """Refuse a chain of cross-reference sections that runs in a circle or is too long.

    The library follows "/Prev" (and "/XRefStm") from the section that the last "startxref" names,
    and does not notice that it has been there before, so a section that points at itself keeps it
    busy until its time is up. The walk here does what the library does, once: a section that is
    reached a second time, more than ``PDF_MAX_PREV`` links of either kind, and an offset outside the
    file are refused as damaged. An offset that does not point at a section ends that branch of the
    walk without a refusal (the library finds sections whose offsets are a little off), and the
    other checks decide about such a file. The work is at most one search for each section.
    """
    if b'/Prev' not in data and b'/XRefStm' not in data:
        return
    last = data.rfind(b'startxref')
    found = _STARTXREF.match(data, last) if last >= 0 else None
    if found is None:
        return
    size = len(data)
    todo = [int(found.group(1))]
    seen = set()
    prev_hops = 0
    stream_hops = 0
    while todo:
        offset = todo.pop()
        if offset >= size or offset in seen:
            raise PdfToolError('damaged')
        seen.add(offset)
        links = _section_links(data, offset)
        if links is None:
            continue
        prev_hops += len(links[0])
        stream_hops += len(links[1])
        if prev_hops > PDF_MAX_PREV or stream_hops > PDF_MAX_PREV:
            raise PdfToolError('damaged')
        todo.extend(links[0])
        todo.extend(links[1])


def _check_markers(data):
    """Refuse a file with more revisions, links or object streams than a real file has.

    Every marker is counted in its structural form ("/Type /XRef", "/Type /ObjStm", "/Prev" in a
    trailer or cross-reference stream dictionary) so that ordinary content, such as the hundreds of
    "/Prev" keys of a document outline, is never taken for a parsing hazard. The counting stops at
    the limit, so the work does not depend on how many markers an attacker writes. The chain of
    cross-reference sections is then walked (``_check_xref_chain``).
    """
    if (data.count(b'startxref') > PDF_MAX_STARTXREF
            or _count_up_to(_XREF_TYPE, data, PDF_MAX_XREF_STREAMS) > PDF_MAX_XREF_STREAMS
            or _count_up_to(_OBJSTM_TYPE, data, PDF_MAX_OBJSTM) > PDF_MAX_OBJSTM):
        raise PdfToolError('damaged')
    links = _chain_links(data) if b'/Prev' in data else []
    if len(links) > PDF_MAX_PREV or len(set(links)) != len(links):
        raise PdfToolError('damaged')
    _check_xref_chain(data)
    _check_decoded_chains(data)


def _unescaped(found):
    """The character that a ``#xx`` escape of a name stands for."""
    return bytes([int(found.group(1), 16)])


def _positions(pattern, data, limit):
    """Where ``pattern`` matches in ``data``; more than ``limit`` matches are refused as damaged."""
    found = []
    for match in pattern.finditer(data):
        found.append(match.start())
        if len(found) > limit:
            raise PdfToolError('damaged')
    return found


def _check_decoded_chains(data):
    """Refuse a file that has a stream, decoded by the library, with a chain of filters it cannot bound.

    Which streams does the library decode while a file is opened, and while pages are copied and
    written (the paths of ``open_pdf`` and of the split and merge wizards)? Only two kinds: the
    cross-reference streams (``/Type /XRef``) and the object streams (``/Type /ObjStm``); content,
    fonts and images are copied as they are. The library runs the whole chain of filters of such a
    stream and inflates every flate step in one piece, so a chain of two flate steps (or a text
    filter and a flate step) turns a few bytes into gigabytes of memory, and ``_check_inflated_size``
    sees only the first layer. A stream of these two kinds may have no filter or one flate filter
    (with or without parameters); anything else is refused. Because "/Type" may be an indirect
    reference, a name written with ``#xx`` escapes, or sit anywhere in a padded dictionary, the
    stream's own dictionary is not looked for: a file is refused when a "/Type" that can mean one of
    the two kinds and a "/Filter" that is not one flate step (or one harmless filter) lie within
    ``PDF_DICTIONARY_REACH`` bytes of each other. Real files of the two kinds use one flate step.
    """
    if b'/' not in data:
        return
    decoded = _positions(_DECODED_TYPE, data, _DECODED_POSITIONS_MAX)
    if not decoded:
        return
    hazards = _positions(_HAZARDOUS_FILTER, data, _DECODED_POSITIONS_MAX)
    index = 0
    for position in hazards:
        while index < len(decoded) and decoded[index] < position - PDF_DICTIONARY_REACH:
            index += 1
        if index < len(decoded) and decoded[index] <= position + PDF_DICTIONARY_REACH:
            raise PdfToolError('damaged')


def _check_inflated_size(data):
    """Refuse a file with a flate stream that inflates far too much.

    Every stream that starts with a zlib or gzip header (the library inflates both) is inflated in
    steps of ``_INFLATE_STEP`` bytes and the output is only counted, never kept. The inflater itself
    finds the end of the stream, so a stream that hides its end marker is counted all the same.
    Anything that is not valid flate data is left to the library. The streams that the library
    decodes with more than one step are refused before (``_check_decoded_chains``), so this check
    sees the one flate layer that the library inflates. The sizes are limited as the comment at
    ``PDF_STREAM_MAX_BYTES`` says; a file over a limit is refused as too large. The work is bounded
    too: a file with more streams than the larger of ``PDF_MAX_FLATE_STREAMS`` and the size of the
    file over ``PDF_BYTES_PER_STREAM`` is refused, and no more than ``PDF_INFLATE_INPUT_RATIO``
    times the size of the file is read, whatever the file holds.
    """
    size_of_file = len(data)
    pages = max(1, min(_count_up_to(_PAGE_TYPE, data, PDF_MAX_PAGES), PDF_MAX_PAGES))
    all_max = min(PDF_DECODED_MAX_BYTES,
                  max(PDF_INFLATE_FLOOR, PDF_INFLATE_RATIO * size_of_file),
                  max(PDF_DECODED_BASE + PDF_DECODED_PER_PAGE * pages,
                      PDF_DECODED_BASE + PDF_DECODED_PER_BYTE * size_of_file))
    one_max = min(all_max, max(PDF_STREAM_MAX_BYTES, PDF_DECODED_PER_BYTE * size_of_file))
    read_max = PDF_INFLATE_INPUT_RATIO * size_of_file
    streams_max = max(PDF_MAX_FLATE_STREAMS, size_of_file // PDF_BYTES_PER_STREAM)
    if data.count(b'stream') > 2 * streams_max:     # every stream has two of the keyword
        raise PdfToolError('too_large')
    view = memoryview(data)
    total = 0
    read = 0
    streams = 0
    for found in _STREAM.finditer(data):
        start = found.end()
        if start + 3 > size_of_file:
            continue
        first, second = data[start], data[start + 1]
        if not ((first & 0x0f == 8 and (first * 256 + second) % 31 == 0)
                or (first == 0x1f and second == 0x8b and data[start + 2] == 8)):
            continue
        streams += 1
        if streams > streams_max:
            raise PdfToolError('too_large')
        inflater = zlib.decompressobj(zlib.MAX_WBITS | 32)
        size = 0
        position = start
        used = 0       # input the inflater took without an error (a chunk that fails is not charged)
        try:
            while position < size_of_file and not inflater.eof:
                chunk = view[position:position + _INFLATE_STEP]
                position += len(chunk)
                piece = inflater.decompress(chunk, _INFLATE_STEP)
                while True:
                    size += len(piece)
                    if size > one_max or total + size > all_max:
                        raise PdfToolError('too_large')
                    tail = inflater.unconsumed_tail
                    if inflater.eof or (len(piece) < _INFLATE_STEP and not tail):
                        break
                    piece = inflater.decompress(tail, _INFLATE_STEP)
                used = position - start - (len(inflater.unused_data) if inflater.eof else 0)
                if read + used > read_max:
                    raise PdfToolError('too_large')
        except zlib.error:
            pass    # not flate data after all: the library decides what to do with it
        read += used
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
        self._own = (self.__enter__.__code__, self.__exit__.__code__)

    def _hook(self, frame, event, arg):
        if frame.f_code in self._own:
            return      # the hook is installed in here, and removed in ``__exit__``: leave both alone
        if time.monotonic() > self.limit:
            self.expired = True
            raise _Timeout()

    def __enter__(self):
        self.previous = sys.getprofile()
        sys.setprofile(self._hook)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            sys.setprofile(self.previous)
        finally:
            if exc_type is not None and issubclass(exc_type, _Timeout):
                self.expired = True
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
    try:
        with _Deadline(PDF_PARSE_SECONDS):
            _check_markers(data)
            _check_inflated_size(data)
            reader = odoo_pdf.PdfReader(io.BytesIO(data), strict=False)
            if _encrypted(reader):
                raise PdfToolError('encrypted')
            _check_page_tree(reader)
            count = len(reader.pages)
    except PdfToolError:
        raise
    except _Timeout as exc:    # the deadline's own signal, in case it ever got past ``__exit__``
        raise PdfToolError('damaged') from exc
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
