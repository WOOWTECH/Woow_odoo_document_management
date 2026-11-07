# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Storage quota (IF-QUOTA, owned by upload-quota). Q2 and Q5 live here.

Day-0 stub, fail closed: every quota check rejects.
"""
from odoo import _, models
from odoo.exceptions import AccessError

from .upload_policy import WoowUploadRejected


class WoowDocumentQuota(models.AbstractModel):
    _name = 'woow.document.quota'
    _description = 'Document storage quota'

    def _usage_bytes(self) -> int:
        """Q2: deduplicated bytes of all stored attachments."""
        raise NotImplementedError  # WOOW-STUB

    def _bytes_if_new(self, checksum, size) -> int:
        """Q5: 0 when a stored attachment with this checksum exists, else ``size``."""
        raise NotImplementedError  # WOOW-STUB

    def _check(self, checksum, size) -> None:
        """Refuse uploads above quota_block_pct under lock('quota'); LockBusy on timeout."""
        raise WoowUploadRejected('quota')  # WOOW-STUB

    def _cron_check_warn(self) -> None:
        """Daily: activity for document administrators when usage reaches quota_warn_pct."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        raise NotImplementedError  # WOOW-STUB
