# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Add attachments to a folder (owned by backend-ui). Day-0 stub."""
from odoo import fields, models


class WoowDocumentAddAttachmentWizard(models.TransientModel):
    _name = 'woow.document.add.attachment.wizard'
    _description = 'Add attachments to a folder'

    attachment_ids = fields.Many2many('ir.attachment')
    folder_id = fields.Many2one('woow.document.folder')

    def action_add(self):
        """Copy the selected attachments into the folder as documents."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB
