# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Restore to another folder (owned by trash-audit-cron). Day-0 stub."""
from odoo import fields, models


class WoowDocumentRestoreWizard(models.TransientModel):
    _name = 'woow.document.restore.wizard'
    _description = 'Restore to another folder'

    document_ids = fields.Many2many('woow.document')
    folder_ids = fields.Many2many('woow.document.folder')
    target_folder_id = fields.Many2one('woow.document.folder')

    def action_restore_to(self):
        """Restore the selected items into the chosen folder."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB
