# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Upload a new version of a file."""
import base64
import binascii

from odoo import _, api, fields, models
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

    @api.model_create_multi
    def create(self, vals_list):
        """A row is made only for a document the caller may change, and only with a file that the
        upload policy accepts: nothing else is parked in the table of the wizard."""
        for vals in vals_list:
            completed = self._add_missing_default_values(dict(vals))
            self._woow_judge_input(completed.get('document_id'), completed.get('datas'),
                                   completed.get('filename'))
        return super().create(vals_list)

    def write(self, vals):
        if 'document_id' in vals or vals.get('datas'):
            for wizard in self:
                self._woow_judge_input(
                    vals.get('document_id', wizard.document_id.id), vals.get('datas'),
                    vals.get('filename', wizard.filename))
        return super().write(vals)

    @api.model
    def _woow_judge_input(self, document_id, datas, filename):
        """The same answer for a missing, a hidden and a read-only document (nothing to learn
        from it); a file that the policy refuses is refused when it arrives, not when it is used.
        The quota is judged by the confirm, which is the call that stores the file."""
        if self.env.su:
            return
        document = self.env['woow.document']
        if isinstance(document_id, int):
            document = document.search([('id', '=', document_id)], limit=1)
            if not document or not document.has_access('write'):
                raise AccessError(_('This document is unavailable.'))
        if datas:
            try:
                raw = base64.b64decode(datas, validate=True)
            except (binascii.Error, TypeError, ValueError):
                raise WoowUploadRejected('content') from None
            self.env['woow.document.upload.policy']._validate(
                filename or document.name or 'file', raw, quota=False)

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
