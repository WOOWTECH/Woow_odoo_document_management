# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Rename a document or a folder; keep the restore wizard from moving a locked document."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class WoowDocumentRenameWizard(models.TransientModel):
    _name = 'woow.document.rename.wizard'
    _inherit = ['woow.document.wizard.refs.mixin', 'woow.document.server.fields.mixin']
    _description = 'Rename a document or folder'
    _woow_own_default_fields = frozenset()

    document_id = fields.Many2one('woow.document', ondelete='cascade')
    folder_id = fields.Many2one('woow.document.folder', ondelete='cascade')
    name = fields.Char(string='New name', required=True)

    def _woow_visible_refs(self):
        return {
            'document_id': ('woow.document', _('This item is unavailable.')),
            'folder_id': ('woow.document.folder', _('This item is unavailable.')),
        }

    @api.model
    def default_get(self, fields_list):
        """Prefill the name with the current one of the item (found under the caller's rules)."""
        values = super().default_get(fields_list)
        if 'name' in fields_list and not values.get('name'):
            context = self.env.context
            for model_name, key in (('woow.document', 'default_document_id'),
                                    ('woow.document.folder', 'default_folder_id')):
                record_id = context.get(key)
                if type(record_id) is int:
                    record = self.env[model_name].search([('id', '=', record_id)], limit=1)
                    if record:
                        values['name'] = record.name
        return values

    def action_rename(self):
        """Rename the item with a normal write: every guard of the document or folder applies."""
        self.check_access('write')
        self.ensure_one()
        if bool(self.document_id) == bool(self.folder_id):
            raise UserError(_('Choose one document or one folder to rename.'))
        name = (self.name or '').strip()
        if not name:
            raise UserError(_('A name is required.'))
        model = self.env['woow.document' if self.document_id else 'woow.document.folder']
        record = model.search([('id', '=', (self.document_id or self.folder_id).id)], limit=1)
        if not record:
            raise AccessError(_('This item is unavailable.'))
        record.write({'name': name})
        return {'type': 'ir.actions.act_window_close'}


class WoowDocumentRestoreWizardLock(models.TransientModel):
    _inherit = 'woow.document.restore.wizard'

    def action_restore_to(self):
        """Restore into another folder, unless another user has locked one of the documents."""
        self.check_access('write')
        self.ensure_one()
        self.with_context(active_test=False).document_ids._woow_refuse_foreign_lock('write')
        return super().action_restore_to()
