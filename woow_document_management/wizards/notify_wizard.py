# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Notify folder members (owned by backend-ui). Day-0 stub."""
from odoo import fields, models


class WoowDocumentNotifyWizard(models.TransientModel):
    _name = 'woow.document.notify.wizard'
    _description = 'Notify folder members'

    folder_id = fields.Many2one('woow.document.folder')
    note = fields.Text()

    def action_send(self):
        """Queue the member notice mails (no share token inside)."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB
