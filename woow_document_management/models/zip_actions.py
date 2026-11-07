# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""ZIP download buttons (owned by internal-routes-zip). Day-0 stub."""
from odoo import models


class WoowDocumentZip(models.Model):
    _inherit = 'woow.document'

    def action_download_zip(self):
        """Download the selected documents as one ZIP file."""
        self.check_access('read')
        raise NotImplementedError  # WOOW-STUB


class WoowDocumentFolderZip(models.Model):
    _inherit = 'woow.document.folder'

    def action_download_zip(self):
        """Download the selected folders as one ZIP file."""
        self.check_access('read')
        raise NotImplementedError  # WOOW-STUB
