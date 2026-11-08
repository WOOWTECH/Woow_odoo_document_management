# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Upload validation (IF-UPL, owned by upload-quota).

Day-0 stub, fail closed: every upload is rejected, nothing is served inline.
"""
from __future__ import annotations

import dataclasses

from odoo import _, models
from odoo.exceptions import UserError

INLINE_WHITELIST: frozenset[str] = frozenset({
    'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf', 'text/plain'})
# When CA-23 does not hold, upload-quota removes 'application/pdf'; the change log goes to
# ci-sweep for docs/CONTRACTS.md (ER-47.14).

REJECT_CODES = ('size', 'filename', 'extension', 'mime', 'content', 'pixels', 'quota',
                'no_libmagic', 'channel', 'stub')   # 'stub' is day-0 only


class WoowUploadRejected(UserError):
    """An uploaded file was refused; ``code`` is one of REJECT_CODES."""

    def __init__(self, code, **params):
        if code not in REJECT_CODES:
            raise ValueError('unknown reject code')
        self.code = code
        self.params = params
        super().__init__(_('The file was not accepted.'))


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
        raise WoowUploadRejected('stub')  # WOOW-STUB

    def _served_mimetype(self, doc) -> str:
        """Stored sniffed type (text/* + '; charset=utf-8'); unknown -> application/octet-stream."""
        return 'application/octet-stream'  # WOOW-STUB

    def _inline_allowed(self, mimetype) -> bool:
        """True when the base type is in INLINE_WHITELIST."""
        return False  # WOOW-STUB
