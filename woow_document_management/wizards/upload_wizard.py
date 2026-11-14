# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Upload several files to one folder in one step, through the normal document create."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.quota import LEDGER_KEY, QuotaLedger
from ..models.upload_policy import WoowUploadRejected
from ..services.constants import MIB

# Most refused files that the result message names; the count of the others is still given.
_NAMED_REFUSALS = 10


class WoowDocumentUploadWizard(models.TransientModel):
    _name = 'woow.document.upload.wizard'
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'Upload documents'
    _woow_own_default_fields = frozenset()      # every field is filled in by the user in the form

    folder_id = fields.Many2one('woow.document.folder', required=True, ondelete='cascade')
    tag_ids = fields.Many2many('woow.document.tag', 'woow_document_upload_wizard_tag_rel',
                               'wizard_id', 'tag_id')
    line_ids = fields.One2many('woow.document.upload.wizard.line', 'wizard_id', string='Files')

    def action_upload(self):
        """Create one document per file in the chosen folder; a refused file does not stop the others."""
        self.check_access('write')
        self.ensure_one()
        user = self.env.user
        if not user.has_group('woow_document_management.group_woow_document_user'):
            raise AccessError(_('You cannot upload documents.'))
        lines = self.line_ids
        if not lines:
            raise UserError(_('Add at least one file.'))
        if len(lines) > self.env['woow.document.settings']._get_settings().max_files_per_upload:
            raise UserError(_('Invalid number of files.'))
        folder = self.env['woow.document.folder'].search([
            ('id', '=', self.folder_id.id), ('in_trash', '=', False)], limit=1)
        if not folder:
            raise AccessError(_('The target folder is unavailable.'))
        folder.check_access('read')
        folder._woow_require_rank(2)
        tags = self.env['woow.document.tag'].search([('id', 'in', self.tag_ids.ids)])
        if len(tags) != len(self.tag_ids):
            raise AccessError(_('One or more selected tags are unavailable.'))

        created = self.env['woow.document']
        refused = []
        # One quota measurement for the whole upload, and the bytes it adds as it goes.
        documents = self.env['woow.document'].with_context(
            woow_log_channel='backend', **{LEDGER_KEY: QuotaLedger()})
        for line in lines:
            label = line.name or _('(unnamed file)')
            if not line.name or not line.datas:
                refused.append((label, _('The file has no name or no content.')))
                continue
            try:
                with self.env.cr.savepoint():
                    created |= documents.create({
                        'name': line.name, 'folder_id': folder.id,
                        'datas': line.datas, 'tag_ids': [(6, 0, tags.ids)]})
            except (WoowUploadRejected, UserError, AccessError) as error:
                refused.append((label, str(error)))
        # The temporary rows are not needed any more, whatever the result.
        lines.unlink()
        if refused and not created:
            raise UserError(self._refusal_message(refused, 0))
        message = _('%(added)s added.', added=len(created))
        if refused:
            message = _('%(added)s added; %(refused)s refused. %(names)s',
                        added=len(created), refused=len(refused),
                        names=self._refusal_message(refused, len(created), names_only=True))
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Upload finished'), 'message': message,
                           'type': 'warning' if refused else 'success', 'sticky': bool(refused),
                           'next': {'type': 'ir.actions.act_window_close'}}}

    @api.model
    def _refusal_message(self, refused, added, names_only=False):
        """The refused files by name (the first few), each with its reason."""
        shown = '; '.join('%s: %s' % (name, reason)
                          for name, reason in refused[:_NAMED_REFUSALS])
        if len(refused) > _NAMED_REFUSALS:
            shown += _('; and %(count)s more', count=len(refused) - _NAMED_REFUSALS)
        if names_only:
            return shown
        return _('No files were uploaded: %(reasons)s', reasons=shown)


class WoowDocumentUploadWizardLine(models.TransientModel):
    _name = 'woow.document.upload.wizard.line'
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'File of an upload'
    _woow_own_default_fields = frozenset()      # every field is filled in by the user in the form

    wizard_id = fields.Many2one('woow.document.upload.wizard', required=True, ondelete='cascade',
                                index=True)
    name = fields.Char()
    # The bytes stay in the row of this temporary table, not in an attachment of their own.
    datas = fields.Binary(attachment=False)

    @api.model_create_multi
    def create(self, vals_list):
        """Bound the number and the size of the files that wait in one upload."""
        self._woow_check_lines(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        if 'datas' in vals:
            self._woow_check_size(vals['datas'])
        return super().write(vals)

    def _woow_check_size(self, encoded):
        """Refuse a file above the internal upload limit before it is kept (a length check only).

        The content itself is validated, with the upload policy, when the upload is confirmed.
        """
        if not encoded or self.env.su:
            return
        if not isinstance(encoded, (str, bytes)):
            raise WoowUploadRejected('content')
        limit = self.env['woow.document.settings']._get_settings().internal_max_upload_mb
        if len(encoded) * 3 // 4 > limit * MIB + 3:
            raise WoowUploadRejected('size', limit=limit)

    def _woow_check_lines(self, vals_list):
        for vals in vals_list:
            if 'datas' in vals:
                self._woow_check_size(vals['datas'])
        if self.env.su:
            return
        limit = self.env['woow.document.settings']._get_settings().max_files_per_upload
        added = {}
        for vals in vals_list:
            wizard_id = vals.get('wizard_id')
            if isinstance(wizard_id, int):
                added[wizard_id] = added.get(wizard_id, 0) + 1
        for wizard_id, count in added.items():
            if count + self.search_count([('wizard_id', '=', wizard_id)]) > limit:
                raise UserError(_('Invalid number of files.'))
