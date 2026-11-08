# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Move documents and folders (owned by backend-ui). Day-0 stub."""
from odoo import fields, models


class WoowDocumentMoveWizard(models.TransientModel):
    _name = 'woow.document.move.wizard'
    _description = 'Move documents and folders'

    document_ids = fields.Many2many('woow.document')
    folder_ids = fields.Many2many('woow.document.folder')
    target_folder_id = fields.Many2one('woow.document.folder')

    def action_move(self):
        """Move the selected items to the target folder."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB
