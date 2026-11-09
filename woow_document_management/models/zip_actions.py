# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""ZIP download buttons with access checks in the caller's environment."""
from urllib.parse import urlencode

from odoo import models
from odoo.exceptions import AccessError


def _action(name, ids):
    return {'type': 'ir.actions.act_url',
            'url': '/woow_document/zip?' + urlencode({name: ','.join(map(str, ids))}),
            'target': 'download'}


class WoowDocumentZip(models.Model):
    _inherit = 'woow.document'

    def action_download_zip(self):
        """Download the selected documents as one ZIP file."""
        self.check_access('read')
        if not self.env.user._is_internal() or not self or len(self.search([('id', 'in', self.ids)])) != len(self):
            raise AccessError('Documents unavailable.')
        return _action('document_ids', self.ids)


class WoowDocumentFolderZip(models.Model):
    _inherit = 'woow.document.folder'

    def action_download_zip(self):
        """Download the selected folders as one ZIP file."""
        self.check_access('read')
        if not self.env.user._is_internal() or not self or len(self.search([('id', 'in', self.ids)])) != len(self):
            raise AccessError('Folders unavailable.')
        return _action('folder_ids', self.ids)
