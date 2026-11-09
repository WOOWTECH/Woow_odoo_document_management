# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Delete permanently (owned by trash-audit-cron). Day-0 stub."""
from odoo import _, fields, models
from odoo.exceptions import UserError


class WoowDocumentPurgeWizard(models.TransientModel):
    _name = 'woow.document.purge.wizard'
    _description = 'Delete permanently'

    document_ids = fields.Many2many('woow.document')
    folder_ids = fields.Many2many('woow.document.folder')
    confirm = fields.Boolean()

    def action_confirm_purge(self):
        """Delete the selected items permanently after confirmation."""
        self.check_access('write')
        self.ensure_one()
        self.env['woow.document']._require_manager()
        docs = self.with_context(active_test=False).document_ids
        folders = self.with_context(active_test=False).folder_ids
        if not self.confirm or not (docs or folders):
            raise UserError(_('Confirm permanent deletion of the selected items.'))
        folder_done, folder_failed = folders._woow_purge()
        doc_done, doc_failed = docs.exists()._woow_purge()
        if folder_failed or doc_failed:
            raise UserError(_('Some selected items could not be deleted.'))
        return {'type': 'ir.actions.act_window_close'}
