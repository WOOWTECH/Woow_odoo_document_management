# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Create share links (owned by share). Day-0 stub."""
from odoo import fields, models


class WoowDocumentShareWizard(models.TransientModel):
    _name = 'woow.document.share.wizard'
    _description = 'Create share links'

    share_type = fields.Selection([('folder', 'Folder'), ('documents', 'Documents')], required=True,
                                  default='documents')
    folder_id = fields.Many2one('woow.document.folder')
    document_ids = fields.Many2many('woow.document')
    include_subfolders = fields.Boolean(default=True)
    recipient_emails = fields.Text()
    expire_at = fields.Datetime()
    max_downloads = fields.Integer(default=0)
    password = fields.Char()   # transient; cleared after use, never logged
    note = fields.Text()

    def action_create_links(self):
        """Create the share links and return them once to the creator."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB
