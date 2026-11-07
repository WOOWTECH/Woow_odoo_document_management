# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Back-office buttons (IF-UI, owned by backend-ui). Day-0 stub."""
from odoo import models


class WoowDocumentBackend(models.Model):
    _inherit = 'woow.document'

    def action_open_move_wizard(self):
        """Open the move wizard."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB


class WoowDocumentFolderBackend(models.Model):
    _inherit = 'woow.document.folder'

    def action_open_move_wizard(self):
        """Open the move wizard."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB

    def action_notify_members(self):
        """Open the member notice wizard."""
        self._woow_require_rank(4)
        raise NotImplementedError  # WOOW-STUB
