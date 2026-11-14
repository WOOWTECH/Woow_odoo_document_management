# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Locks and the trash actions.

These overrides sit above the trash actions (this file is imported after ``trash_actions``), so
that they are asked first. The lock itself lives in ``docactions.py``.
"""
from odoo import models

from .docactions import _PURGE_KEY, _PURGE_MARK


class WoowDocumentTrashLock(models.Model):
    _inherit = 'woow.document'

    def action_trash(self):
        """Move the documents to the trash, unless another user has locked one of them."""
        self.check_access('write')
        self._woow_refuse_foreign_lock('write')
        return super().action_trash()

    def action_restore(self):
        """Restore the documents, unless another user has locked one of them."""
        self.with_context(active_test=False).check_access('write')
        self.with_context(active_test=False)._woow_refuse_foreign_lock('write')
        return super().action_restore()


class WoowDocumentFolderTrashLock(models.Model):
    _inherit = 'woow.document.folder'

    def _woow_purge_tree(self, limit=None):
        """Delete a trashed folder with its documents; a lock of a document does not hold it back."""
        return super(WoowDocumentFolderTrashLock, self.with_context(
            **{_PURGE_KEY: _PURGE_MARK}))._woow_purge_tree(limit)
