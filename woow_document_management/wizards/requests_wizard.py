# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Wizards of requested files: ask someone for a file, and send the file asked for."""
import base64
import binascii

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.upload_policy import WoowUploadRejected
from ..services.constants import MIB
from .upload_wizard import WIZARDS_PER_USER


class WoowDocumentRequestWizard(models.TransientModel):
    _name = 'woow.document.request.wizard'
    _inherit = ['woow.document.wizard.refs.mixin', 'woow.document.server.fields.mixin']
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
    # no size: a longer note is refused with a message by the request itself, never cut
    note = fields.Char(string='Note')

    def _woow_visible_refs(self):
        return {
            'folder_id': ('woow.document.folder', _('The target folder is unavailable.')),
            'tag_ids': ('woow.document.tag', _('One or more selected tags are unavailable.')),
        }

    def action_create(self):
        """Create the placeholder and schedule the activity of the person asked."""
        self.check_access('write')
        self.ensure_one()
        self.env['woow.document'].with_context(woow_log_channel='backend')._woow_open_request(
            self.folder_id, self.name, self.request_user_id, self.days, self.tag_ids, self.note)
        return {'type': 'ir.actions.act_window_close'}


class WoowDocumentRequestFulfilWizard(models.TransientModel):
    _name = 'woow.document.request.fulfil.wizard'
    _inherit = ['woow.document.wizard.refs.mixin', 'woow.document.server.fields.mixin']
    _description = 'Send a requested file'
    _woow_own_default_fields = frozenset({'file_size'})

    document_id = fields.Many2one('woow.document', string='Requested file', required=True,
                                  ondelete='cascade')
    file = fields.Binary(string='File', attachment=False)
    filename = fields.Char(string='File name')
    # Set by the server from ``file``: the sum of it is what a user keeps waiting.
    file_size = fields.Integer(readonly=True)

    def _woow_visible_refs(self):
        return {
            'document_id': ('woow.document', _('You cannot upload to this request.')),
        }

    @api.model_create_multi
    def create(self, vals_list):
        """Bound the size and the number of the files that wait in the database, as the upload does.

        Only the newest ``WIZARDS_PER_USER`` dialogs of a user keep their file (the older ones were
        abandoned, and their bytes would stay until the transient vacuum); one file is at most the internal
        upload limit, and what a user keeps waiting is at most that limit for each dialog.
        """
        if self.env.su:
            return super().create(vals_list)
        if len(vals_list) > WIZARDS_PER_USER:
            raise UserError(_('Too many dialogs at once.'))
        vals_list = [self._woow_checked_values(vals) for vals in vals_list]
        wizards = super().create(vals_list)
        mine = self.search([('create_uid', '=', self.env.uid)], order='id desc')
        # (a user may not delete a row of this wizard, so the bytes of an abandoned one are dropped)
        mine[WIZARDS_PER_USER:].filtered('file_size').write({'file': False})
        wizards._woow_check_totals()
        return wizards

    def write(self, vals):
        if self.env.su:
            return super().write(vals)
        vals = self._woow_checked_values(vals)
        result = super().write(vals)
        if 'file' in vals:
            self._woow_check_totals()
        return result

    @api.model
    def _woow_checked_values(self, vals):
        """The values of one row, with the size that the server computes from the file.

        Only a length check of the content: the file is validated, with the upload policy and the
        quota, when it is sent (``action_fulfil``).
        """
        if 'file_size' in vals:
            raise AccessError(_('This field is set by the server.'))
        vals = dict(vals)
        if 'file' in vals:
            encoded = vals['file']
            if encoded and not isinstance(encoded, (str, bytes)):
                raise WoowUploadRejected('content')
            limit = self.env['woow.document.settings']._get_settings().internal_max_upload_mb
            # the bytes that the text stands for (the padding is not counted)
            size = len(encoded.rstrip(b'=' if isinstance(encoded, bytes) else '=')) * 3 // 4 \
                if encoded else 0
            if size > limit * MIB:
                raise WoowUploadRejected('size', limit=limit)
            vals['file_size'] = size
        return vals

    def _woow_check_totals(self):
        """What waits in the database for one user stays bounded (read from the stored rows)."""
        limit = self.env['woow.document.settings']._get_settings().internal_max_upload_mb
        mine = self._read_group([('create_uid', '=', self.env.uid)], [], ['file_size:sum'])
        if ((mine[0][0] if mine else 0) or 0) > limit * WIZARDS_PER_USER * MIB:
            raise WoowUploadRejected('size', limit=limit * WIZARDS_PER_USER)

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
