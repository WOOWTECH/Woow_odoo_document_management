# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Upload validation (IF-UPL, owned by upload-quota).

Day-0 stub, fail closed: every upload is rejected, nothing is served inline.
"""
from __future__ import annotations

import dataclasses
import io
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


def _bounded_zip_directory(data: bytes) -> bool:
    """Reject oversized ZIP directories before ZipFile allocates entry objects."""
    if not data.startswith(b'PK\x03\x04'):
        return False
    # EOCD has a 22-byte fixed header and an optional 65535-byte comment.
    end = data.rfind(b'PK\x05\x06', max(0, len(data) - 65557))
    if end < 0 or end + 22 > len(data):
        return False
    disk, disk_cd, on_disk, total, directory_size, directory_offset, comment_size = \
        struct.unpack_from('<HHHHIIH', data, end + 4)
    return (disk == disk_cd == 0 and on_disk == total and total <= 4096
            and directory_size <= 1024 * 1024
            and directory_offset + directory_size <= end
            and end + 22 + comment_size == len(data)
            and total != 0xffff and directory_size != 0xffffffff
            and directory_offset != 0xffffffff)


def _office_zip_mimetype(data: bytes, ext: str) -> str:
    """Inspect bounded ZIP metadata; never extract an untrusted Office member wholesale."""
    if not _bounded_zip_directory(data):
        return ''
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 4096:
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
                if info.file_size > 255:
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
        elif code == 'quota':
            message = _('Document storage on this site is %(pct)s%% full, so new files '
                        "can't be added. Ask your administrator to free up space or raise "
                        'the limit.', pct=params.get('pct', 100))
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

    def _validate(self, name, data) -> ValidatedFile:
        """Every check of arch section 11; the channel comes from env.user. Ends with quota._check."""
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
        try:
            safe_name = sanitize_filename(name)
        except ValueError as exc:
            raise WoowUploadRejected('filename') from exc
        ext = safe_name.rpartition('.')[2].lower()
        allowed = DEFAULT_EXTENSIONS | (set(settings.extra_extensions) & OPTIONAL_EXTENSIONS)
        if ext in BLOCKED_EXTENSIONS or ext not in allowed:
            raise WoowUploadRejected('extension', ext=ext)
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
            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > PIXEL_LIMIT:
                        raise WoowUploadRejected('pixels')
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise WoowUploadRejected('content') from exc
        checksum = self.env['ir.attachment']._compute_checksum(data)
        self.env['woow.document.quota']._check(checksum, len(data))
        return ValidatedFile(safe_name, mimetype, len(data), checksum)

    def _served_mimetype(self, doc) -> str:
        """Stored sniffed type (text/* + '; charset=utf-8'); unknown -> application/octet-stream."""
        mimetype = str(doc.mimetype or '').split(';', 1)[0].strip().lower()
        if not any(mimetype in allowed for allowed in EXT_TO_MIMES.values()):
            return 'application/octet-stream'
        return mimetype + ('; charset=utf-8' if mimetype.startswith('text/') else '')

    def _inline_allowed(self, mimetype) -> bool:
        """True when the base type is in INLINE_WHITELIST."""
        return str(mimetype).split(';', 1)[0].strip().lower() in INLINE_WHITELIST
