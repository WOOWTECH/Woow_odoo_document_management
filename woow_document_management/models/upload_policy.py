# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Upload validation (IF-UPL, owned by upload-quota).

Name, extension, size, content type, text and image content (header structures and pixels),
then the quota.
"""
from __future__ import annotations

import dataclasses
import io
import re
import struct
import zipfile

from odoo import _, models
from odoo.exceptions import UserError
from odoo.tools import mimetypes as odoo_mimetypes
from PIL import Image, UnidentifiedImageError

from ..services.constants import MIB, PIXEL_LIMIT
from ..services.mime_map import (BLOCKED_EXTENSIONS, DEFAULT_EXTENSIONS,
                                 EXT_TO_MIMES, OPTIONAL_EXTENSIONS,
                                 acceptable_mimes)
from ..services.naming import sanitize_filename

INLINE_WHITELIST: frozenset[str] = frozenset({
    'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf', 'text/plain'})
# When CA-23 does not hold, upload-quota removes 'application/pdf'; the change log goes to
# ci-sweep for docs/CONTRACTS.md (ER-47.14).

REJECT_CODES = ('size', 'filename', 'extension', 'mime', 'content', 'pixels', 'quota',
                'no_libmagic', 'channel', 'stub')   # 'stub' is day-0 only


ZIP_MAX_ENTRIES = 4096
ZIP_MAX_DIRECTORY = 1024 * 1024
ODF_MIMETYPE_MAX_COMPRESSED = 1024    # the compressed size of the ``mimetype`` member of an ODF file
_ZIP_ENTRY = b'PK\x01\x02'


def _zip_directory_ok(data: bytes, start: int, stop: int, entries: int) -> bool:
    """A central directory region ZipFile may walk: in the file, small, few entry headers.

    ZipFile creates one entry per header it reads in the region, whatever the
    declared count; every header starts with the entry signature, so counting
    signatures bounds the entries before any is allocated.
    """
    return (0 <= start <= stop <= len(data) and stop - start <= ZIP_MAX_DIRECTORY
            and entries <= ZIP_MAX_ENTRIES
            and data.count(_ZIP_ENTRY, start, stop) <= ZIP_MAX_ENTRIES)


def _bounded_zip_directory(data: bytes) -> bool:
    """Reject oversized ZIP directories before ZipFile allocates entry objects.

    The classic end record is checked first. When a ZIP64 locator precedes it,
    ZipFile uses the ZIP64 record instead (found through the locator's offset,
    or right before the locator, depending on the Python version), so each
    record found there must pass the same limits; a locator without a record
    is refused.
    """
    if not data.startswith(b'PK\x03\x04'):
        return False
    # EOCD has a 22-byte fixed header and an optional 65535-byte comment.
    end = data.rfind(b'PK\x05\x06', max(0, len(data) - 65557))
    if end < 0 or end + 22 > len(data):
        return False
    disk, disk_cd, on_disk, total, directory_size, directory_offset, comment_size = \
        struct.unpack_from('<HHHHIIH', data, end + 4)
    if not (disk == disk_cd == 0 and on_disk == total
            and directory_offset + directory_size <= end
            and end + 22 + comment_size == len(data)
            and total != 0xffff and directory_size != 0xffffffff
            and directory_offset != 0xffffffff
            and _zip_directory_ok(data, end - directory_size, end, total)):
        return False
    locator = end - 20
    if locator < 0 or data[locator:locator + 4] != b'PK\x06\x07':
        return True
    _signature, locator_disk, record_at, disks = struct.unpack_from('<4sIQI', data, locator)
    if locator_disk != 0 or disks > 1:
        return False
    records = {position for position in (record_at, locator - 56)
               if 0 <= position <= locator - 56 and data[position:position + 4] == b'PK\x06\x06'}
    if not records:
        return False
    for position in records:
        (_signature, _size, _made, _needed, record_disk, record_disk_cd, record_on_disk,
         record_total, record_size, record_offset) = struct.unpack_from('<4sQHHIIQQQQ', data, position)
        if not (record_disk == record_disk_cd == 0 and record_on_disk == record_total
                and record_offset + record_size <= position
                and _zip_directory_ok(data, position - record_size, position, record_total)):
            return False
    return True


# An image library reads the header structures of a file in Python code, one by one,
# and some of them cost more than their size: a GIF appends each comment to the
# previous ones, a JPEG keeps every application segment, a PNG every private chunk.
# A few thousand empty structures of a few bytes each (megabytes of upload) keep a
# worker busy for minutes, so the headers are counted before the library opens the file.
IMAGE_MAX_STRUCTURES = ZIP_MAX_ENTRIES
IMAGE_MAX_JUNK = 4096

# A JPEG can also cost far more than its size through its scans. A progressive (or non-interleaved)
# file may repeat a scan of ten bytes as often as it likes, and the decoder (Odoo decodes the
# images it stores, and every thumbnail of them) goes over the whole picture once for each. Encoders
# write one scan, six for grey, ten for colour and eighteen for four components.
JPEG_MAX_SCANS = 64
_JPEG_MARKER = re.compile(rb'\xff[\x01-\xcf\xd8-\xfe]')  # not stuffing (00), a restart or a fill byte

# A PNG chunk can also cost far more than its size. The library inflates the colour profile
# (iCCP) and compressed text (zTXt, and iTXt when it is compressed): up to 1 MiB for each
# chunk, out of a few hundred bytes, and it caps only the total of the text chunks (64 MiB).
# 4000 profile chunks of a kilobyte each (4 MB) keep a worker busy for seconds. Opening a file
# reads the chunks up to the first image data, but loading it reads the rest up to the end, and
# Odoo loads the PNGs it stores as attachments (``ir.attachment`` reads their orientation, or
# resizes them), so the chunks after the image data cost as much as the ones before it.
# A file has one profile (the PNG specification allows one), a handful of compressed texts and
# fewer than 20 chunks that are not image data; the image data (IDAT, and for an animation the
# frame chunks) is in as many chunks as the encoder likes, but an empty chunk costs the library
# a few microseconds, so beyond 4096 chunks they must average PNG_IMAGE_CHUNK_BYTES bytes.
PNG_MAX_CHUNKS = 256              # chunks that are not image data, in the whole file
PNG_MAX_PROFILES = 1
PNG_MAX_COMPRESSED_TEXTS = 16
PNG_FREE_IMAGE_CHUNKS = 4096
PNG_IMAGE_CHUNK_BYTES = 256
_PNG_COMPRESSED_TEXTS = (b'zTXt', b'iTXt')
_PNG_IMAGE_CHUNKS = (b'IDAT', b'fdAT', b'fcTL')


def _png_structure_ok(data: bytes) -> bool:
    """The chunks of a PNG up to its end: few of them, and none that costs more than its size.

    At most PNG_MAX_CHUNKS that are not image data, one colour profile, PNG_MAX_COMPRESSED_TEXTS
    compressed texts, and image data chunks within PNG_FREE_IMAGE_CHUNKS plus one per
    PNG_IMAGE_CHUNK_BYTES of the file. The walk is as long as the number of chunks it allows.
    """
    size, position = len(data), 8
    others = profiles = texts = images = 0
    image_chunks = PNG_FREE_IMAGE_CHUNKS + size // PNG_IMAGE_CHUNK_BYTES
    while position + 8 <= size:
        kind = data[position + 4:position + 8]
        if kind in _PNG_IMAGE_CHUNKS:
            images += 1
            if images > image_chunks:
                return False
        else:
            others += 1
            if kind == b'iCCP':
                profiles += 1
            elif kind in _PNG_COMPRESSED_TEXTS:
                texts += 1
            if (others > PNG_MAX_CHUNKS or profiles > PNG_MAX_PROFILES
                    or texts > PNG_MAX_COMPRESSED_TEXTS):
                return False
            if kind == b'IEND':
                return True
        position += 12 + int.from_bytes(data[position:position + 4], 'big')
    return True


def _jpeg_structure_ok(data: bytes) -> bool:
    """JPEG segments and scans up to the end of the image, like the decoder walks them.

    At most JPEG_MAX_SCANS scans (SOS). The data of a scan is skipped to the next marker, and what
    follows the end of the image (EOI) is not looked at, as the decoder does not read it either.
    """
    size, position, structures, scans, junk = len(data), 2, 0, 0, 0
    while position < size:
        if data[position] != 0xFF:
            junk += 1
            position += 1
            if junk > IMAGE_MAX_JUNK:
                return False
            continue
        if position + 1 >= size:
            return True
        marker = data[position + 1]
        if marker == 0xD9:
            return True
        structures += 1
        if structures > IMAGE_MAX_STRUCTURES:
            return False
        if marker == 0xFF:
            position += 1               # fill byte
        elif marker in (0x00, 0x01) or 0xD0 <= marker <= 0xD8:
            position += 2               # markers without a length
        else:
            position += 2 + max(int.from_bytes(data[position + 2:position + 4], 'big'), 2)
            if marker == 0xDA:
                scans += 1
                if scans > JPEG_MAX_SCANS:
                    return False
                found = _JPEG_MARKER.search(data, position)     # past the data of the scan
                if not found:
                    return True
                position = found.start()
    return True


def _gif_structure_ok(data: bytes) -> bool:
    """GIF extension blocks (and their sub-blocks) before the first image."""
    size = len(data)
    if size < 13:
        return True
    position = 13 + (3 * (2 << (data[10] & 7)) if data[10] & 0x80 else 0)
    structures = junk = 0
    while position < size:
        block = data[position]
        if block in (0x2C, 0x3B):       # first image, or the trailer: no more headers
            return True
        if block == 0x21:
            position += 2
            structures += 1
            while position < size:      # data sub-blocks up to the empty one
                structures += 1
                if structures > IMAGE_MAX_STRUCTURES:
                    return False
                length = data[position]
                position += 1 + length
                if not length:
                    break
            continue
        junk += 1
        position += 1
        if junk > IMAGE_MAX_JUNK:
            return False
    return structures <= IMAGE_MAX_STRUCTURES


_IMAGE_STRUCTURE_CHECKS = {
    'image/png': _png_structure_ok, 'image/jpeg': _jpeg_structure_ok,
    'image/gif': _gif_structure_ok,
}


def _office_zip_mimetype(data: bytes, ext: str) -> str:
    """Inspect bounded ZIP metadata; never extract an untrusted Office member wholesale."""
    if not _bounded_zip_directory(data):
        return ''
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > ZIP_MAX_ENTRIES:
                return ''
            names = {entry.filename for entry in entries}
            ooxml = {'docx': ('word/', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                     'xlsx': ('xl/', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                     'pptx': ('ppt/', 'application/vnd.openxmlformats-officedocument.presentationml.presentation')}
            if ext in ooxml:
                prefix, mimetype = ooxml[ext]
                return mimetype if '[Content_Types].xml' in names and any(
                    name.startswith(prefix) and len(name) > len(prefix) for name in names) else ''
            odf = {'odt': 'text', 'ods': 'spreadsheet', 'odp': 'presentation'}
            if ext in odf:
                info = archive.getinfo('mimetype')
                # The ODF specification keeps this member stored. A bzip2 or LZMA member is not read:
                # CPython unpacks a whole compressed block before it cuts the read, so a few hundred
                # bytes cost hundreds of megabytes, whatever size the header declares.
                if (info.file_size > 255 or info.compress_size > ODF_MIMETYPE_MAX_COMPRESSED
                        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    return ''
                with archive.open(info) as member:
                    declared = member.read(256)
                expected = 'application/vnd.oasis.opendocument.' + odf[ext]
                return expected if declared == expected.encode('ascii') else ''
    except (KeyError, OSError, RuntimeError, ValueError, zipfile.BadZipFile):
        return ''
    return ''


class WoowUploadRejected(UserError):
    """An uploaded file was refused; ``code`` is one of REJECT_CODES."""

    def __init__(self, code, **params):
        if code not in REJECT_CODES:
            raise ValueError('unknown reject code')
        self.code = code
        self.params = params
        if code == 'size':
            message = _('Larger than the %(limit)s MB limit.', limit=params.get('limit', 0))
        elif code == 'extension':
            message = _('.%(ext)s files aren\'t accepted here.', ext=params.get('ext', ''))
        elif code == 'mime':
            message = _("The file's content doesn't match its .%(ext)s extension.",
                        ext=params.get('ext', ''))
        elif code == 'quota' and 'pct' in params:
            message = _('Document storage on this site is %(pct)s%% full, so new files '
                        "can't be added. Ask your administrator to free up space or raise "
                        'the limit.', pct=params['pct'])
        elif code == 'quota':
            message = _("Document storage on this site is full, so new files can't be added. "
                        'Ask your administrator to free up space or raise the limit.')
        elif code == 'no_libmagic':
            message = _("Uploads are temporarily unavailable because file types can't be "
                        'verified on this server. Please contact the office.')
        else:
            message = _('The file was not accepted.')
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class ValidatedFile:
    name: str        # sanitised file name
    mimetype: str    # sniffed type without charset
    size: int        # real byte length
    checksum: str    # env['ir.attachment']._compute_checksum(data) (ER-39)


class WoowDocumentUploadPolicy(models.AbstractModel):
    _name = 'woow.document.upload.policy'
    _description = 'Document upload policy'

    def _validate(self, name, data, *, quota=True) -> ValidatedFile:
        """Every check of arch section 11; the channel comes from env.user. Ends with quota._check.

        A caller that stores several files in one call passes ``quota=False`` and checks the
        quota of all of them together, last (``quota._check_many``).
        """
        user = self.env.user
        if self.env.su or user._is_internal():
            channel = 'internal'
        elif user._is_portal():
            channel = 'portal'
        else:
            raise WoowUploadRejected('channel')
        if not isinstance(data, bytes):
            raise WoowUploadRejected('content')
        settings = self.env['woow.document.settings']._get_settings()
        limit = (settings.internal_max_upload_mb if channel == 'internal'
                 else settings.portal_max_upload_mb)
        if len(data) > limit * MIB:
            raise WoowUploadRejected('size', limit=limit)
        safe_name = self._safe_name(name)
        ext = self._check_extension(safe_name, settings)
        if channel == 'portal' and odoo_mimetypes.magic is None:
            raise WoowUploadRejected('no_libmagic')
        if data.startswith(b'PK\x03\x04'):
            # Odoo's ZIP fallback may extract an untrusted ODF member. Classify
            # bounded ZIPs ourselves, including ordinary .zip uploads.
            if not _bounded_zip_directory(data):
                raise WoowUploadRejected('mime', ext=ext)
            mimetype = ('application/zip' if ext == 'zip'
                        else _office_zip_mimetype(data, ext))
        else:
            mimetype = (odoo_mimetypes.guess_mimetype(data) or '').split(';', 1)[0].lower()
        if mimetype not in acceptable_mimes(ext):
            raise WoowUploadRejected('mime', ext=ext)
        if ext in {'txt', 'csv'}:
            if b'\x00' in data:
                raise WoowUploadRejected('content')
            for codec in ('utf-8', 'big5'):
                try:
                    data.decode(codec)
                    break
                except UnicodeDecodeError:
                    pass
            else:
                raise WoowUploadRejected('content')
        if mimetype.startswith('image/'):
            if not _IMAGE_STRUCTURE_CHECKS.get(mimetype, lambda _data: True)(data):
                raise WoowUploadRejected('content')
            try:
                with Image.open(io.BytesIO(data)) as image:
                    pixels = image.width * image.height
            except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                # Opening an image compares its declared size with Pillow's own limit:
                # past twice that limit it raises (an Exception, not an OSError), past
                # it it only warns, which a warnings-as-errors setting turns into an
                # exception too. Either way the header declares too many pixels.
                raise WoowUploadRejected('pixels') from exc
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise WoowUploadRejected('content') from exc
            if pixels > PIXEL_LIMIT:
                raise WoowUploadRejected('pixels')
        checksum = self.env['ir.attachment']._compute_checksum(data)
        if quota:
            self.env['woow.document.quota']._check(checksum, len(data))
        return ValidatedFile(safe_name, mimetype, len(data), checksum)

    def _validate_rename(self, name, document_type, mimetype) -> str:
        """Name rules shared with uploads, for a rename that brings no new content.

        Links only get ``sanitize_filename``. Files also need an extension that is listed,
        not blocked, and consistent with the stored (sniffed) MIME type; replacing the
        content goes through ``_validate`` instead.
        """
        safe_name = self._safe_name(name)
        if document_type != 'binary':
            return safe_name
        ext = self._check_extension(safe_name, self.env['woow.document.settings']._get_settings())
        stored = str(mimetype or '').split(';', 1)[0].strip().lower()
        if stored not in acceptable_mimes(ext):
            raise WoowUploadRejected('mime', ext=ext)
        return safe_name

    def _safe_name(self, name) -> str:
        """``sanitize_filename`` with its ValueError mapped to the ``filename`` reject code."""
        try:
            return sanitize_filename(name)
        except ValueError as exc:
            raise WoowUploadRejected('filename') from exc

    def _check_extension(self, safe_name, settings) -> str:
        """Return the lower-case extension; blocked or unlisted ones are rejected."""
        ext = safe_name.rpartition('.')[2].lower()
        allowed = DEFAULT_EXTENSIONS | (set(settings.extra_extensions) & OPTIONAL_EXTENSIONS)
        if ext in BLOCKED_EXTENSIONS or ext not in allowed:
            raise WoowUploadRejected('extension', ext=ext)
        return ext

    def _served_mimetype(self, doc) -> str:
        """Stored sniffed type (text/* + '; charset=utf-8'); unknown -> application/octet-stream."""
        mimetype = str(doc.mimetype or '').split(';', 1)[0].strip().lower()
        if not any(mimetype in allowed for allowed in EXT_TO_MIMES.values()):
            return 'application/octet-stream'
        return mimetype + ('; charset=utf-8' if mimetype.startswith('text/') else '')

    def _inline_allowed(self, mimetype) -> bool:
        """True when the base type is in INLINE_WHITELIST."""
        return str(mimetype).split(';', 1)[0].strip().lower() in INLINE_WHITELIST
