# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Delete permanently (owned by trash-audit-cron). Day-0 stub."""
from odoo import fields, models


class WoowDocumentPurgeWizard(models.TransientModel):
    _name = 'woow.document.purge.wizard'
    _description = 'Delete permanently'

    document_ids = fields.Many2many('woow.document')
    folder_ids = fields.Many2many('woow.document.folder')
    confirm = fields.Boolean()

    def action_confirm_purge(self):
        """Delete the selected items permanently after confirmation."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB
