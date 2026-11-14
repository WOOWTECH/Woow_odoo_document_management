# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Wizards of requested files: ask someone for a file, and send the file asked for."""
import base64
import binascii

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.requests import REQUEST_NOTE_MAX
from ..models.upload_policy import WoowUploadRejected


class WoowDocumentRequestWizard(models.TransientModel):
    _name = 'woow.document.request.wizard'
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'Ask someone for a file'
    _woow_own_default_fields = frozenset()

    name = fields.Char(string='What is needed', required=True)
    folder_id = fields.Many2one('woow.document.folder', string='Folder', required=True)
    request_user_id = fields.Many2one(
        'res.users', string='Ask', required=True,
        domain="[('share', '=', False), ('active', '=', True)]")
    days = fields.Integer(string='Days until due', default=7, required=True)
    tag_ids = fields.Many2many('woow.document.tag', 'woow_document_request_wizard_tag_rel',
                               'wizard_id', 'tag_id', string='Tags')
    note = fields.Char(string='Note', size=REQUEST_NOTE_MAX)

    def action_create(self):
        """Create the placeholder and schedule the activity of the person asked."""
        self.check_access('write')
        self.ensure_one()
        self.env['woow.document'].with_context(woow_log_channel='backend')._woow_open_request(
            self.folder_id, self.name, self.request_user_id, self.days, self.tag_ids, self.note)
        return {'type': 'ir.actions.act_window_close'}


class WoowDocumentRequestFulfilWizard(models.TransientModel):
    _name = 'woow.document.request.fulfil.wizard'
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'Send a requested file'
    _woow_own_default_fields = frozenset()

    document_id = fields.Many2one('woow.document', string='Requested file', required=True,
                                  ondelete='cascade')
    file = fields.Binary(string='File', attachment=False)
    filename = fields.Char(string='File name')

    def action_fulfil(self):
        """Store the file on the placeholder under the upload policy and the quota."""
        self.check_access('write')
        self.ensure_one()
        if not self.file:
            raise UserError(_('Choose a file.'))
        raw = self.file.encode('ascii', 'ignore') if isinstance(self.file, str) else self.file
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise WoowUploadRejected('content') from None
        document = self.document_id.with_context(woow_log_channel='backend')
        if not document.exists():
            raise AccessError(_('You cannot upload to this request.'))
        document._woow_fulfil_request(self.filename, data)
        return {'type': 'ir.actions.act_window_close'}
