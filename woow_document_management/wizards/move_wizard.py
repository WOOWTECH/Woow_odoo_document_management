# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Move selected documents or folders after checking current permissions."""
from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError


class WoowDocumentMoveWizard(models.TransientModel):
    _name = 'woow.document.move.wizard'
    _description = 'Move documents and folders'

    document_ids = fields.Many2many('woow.document')
    folder_ids = fields.Many2many('woow.document.folder')
    target_folder_id = fields.Many2one('woow.document.folder', required=True)

    def action_move(self):
        self.check_access('write')
        self.ensure_one()
        if not self.env.user._is_internal() and not self.env.su:
            raise AccessError(_('Only internal users may move documents.'))
        if not self.document_ids and not self.folder_ids:
            raise UserError(_('Select at least one item to move.'))
        target = self.env['woow.document.folder'].search([
            ('id', '=', self.target_folder_id.id), ('in_trash', '=', False)], limit=1)
        if not target:
            raise AccessError(_('The target folder is unavailable.'))
        target.check_access('read')
        target._woow_require_rank(3 if self.folder_ids else 2)
        with self.env.cr.savepoint():
            for model_name, selected in (
                    ('woow.document', self.document_ids),
                    ('woow.document.folder', self.folder_ids)):
                if not selected:
                    continue
                model = self.env[model_name]
                records = model.browse(selected.ids).exists()
                records = model.search([('id', 'in', records.ids)])
                if len(records) != len(selected):
                    raise AccessError(_('One or more selected items are unavailable.'))
                records.check_access('write')
                if any(record.in_trash if model_name == 'woow.document.folder' else
                       (not record.active or record.folder_in_trash) for record in records):
                    raise AccessError(_('One or more selected items are unavailable.'))
                if model_name == 'woow.document.folder':
                    records._woow_require_rank(4)
                for record in records:
                    if record.company_id != target.company_id:
                        raise UserError(_('Cannot move items between companies.'))
                    if (record.folder_id if model_name == 'woow.document' else record.parent_id) == target:
                        continue
                    record.with_context(woow_log_channel='backend').write({
                        'folder_id' if model_name == 'woow.document' else 'parent_id': target.id})
        return {'type': 'ir.actions.act_window_close'}
