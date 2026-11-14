# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Upload a new version of a file."""
import base64
import binascii

from odoo import _, fields, models
from odoo.exceptions import AccessError

from ..models.upload_policy import WoowUploadRejected


class WoowDocumentVersionWizard(models.TransientModel):
    _name = 'woow.document.version.wizard'
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'Upload a new version of a file'
    _woow_own_default_fields = frozenset()      # every field is filled in by the user in the form

    document_id = fields.Many2one('woow.document', required=True, ondelete='cascade')
    # Kept in the table of the wizard, not as an attachment: the wizard row is short lived.
    datas = fields.Binary(string='New file', attachment=False)
    filename = fields.Char()

    def action_confirm(self):
        """Keep the current content as a version and make the uploaded file the current one."""
        self.check_access('write')
        self.ensure_one()
        document = self.env['woow.document'].search([('id', '=', self.document_id.id)], limit=1)
        if not document:
            raise AccessError(_('This document is unavailable.'))
        try:
            raw = base64.b64decode(self.datas or b'', validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise WoowUploadRejected('content') from None
        if not raw:
            raise WoowUploadRejected('content')
        document._woow_add_version(raw, self.filename)
        self.datas = False
        return {'type': 'ir.actions.act_window_close'}
