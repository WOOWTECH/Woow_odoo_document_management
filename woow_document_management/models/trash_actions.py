# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Trash, restore, purge and legal hold (IF-TRASH, owned by trash-audit-cron). Day-0 stub."""
from odoo import models


class WoowDocumentTrash(models.Model):
    _inherit = 'woow.document'

    def action_trash(self):
        """Move the documents to the trash."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB

    def action_restore(self):
        """Restore the documents (wizard when the original folder is not usable)."""
        self.with_context(active_test=False).check_access('write')
        raise NotImplementedError  # WOOW-STUB

    def action_purge(self):
        """Open the permanent deletion wizard (document administrators)."""
        self._require_manager()
        raise NotImplementedError  # WOOW-STUB

    def action_set_legal_hold(self, hold=None):
        """Set or release the legal hold (document administrators)."""
        self._require_manager()
        raise NotImplementedError  # WOOW-STUB

    def _woow_purge(self):
        """Delete permanently, deepest first, skipping held and locked records -> (done, failed)."""
        raise NotImplementedError  # WOOW-STUB


class WoowDocumentFolderTrash(models.Model):
    _inherit = 'woow.document.folder'

    def action_trash(self):
        """Move the folders and their subtree to the trash."""
        self._woow_require_rank(4)
        raise NotImplementedError  # WOOW-STUB

    def action_restore(self):
        """Restore the folders; requires manage (ER-13)."""
        self.with_context(active_test=False)._woow_require_rank(4)
        raise NotImplementedError  # WOOW-STUB

    def action_purge(self):
        """Open the permanent deletion wizard (document administrators)."""
        self._require_manager()
        raise NotImplementedError  # WOOW-STUB

    def action_set_legal_hold(self, hold=None):
        """Set or release the legal hold (document administrators)."""
        self._require_manager()
        raise NotImplementedError  # WOOW-STUB

    def _woow_purge(self):
        """Delete permanently, deepest first, skipping held and locked records -> (done, failed)."""
        raise NotImplementedError  # WOOW-STUB
