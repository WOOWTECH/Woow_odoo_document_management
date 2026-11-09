# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Explicit trash transitions after checking the caller's live access."""
from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError
from ..services.locks import LockBusy, bounded_wait
from ..services.constants import LOCK_TIMEOUT_MS

from .document import WoowDocument
from .document_folder import WoowDocumentFolder


def _audit(record, event, **detail):
    record.env['woow.document.log']._log_event(
        event, channel=record._woow_log_channel(),
        **({'document_id': record.id} if record._name == 'woow.document'
           else {'folder_id': record.id}),
        company_id=record.company_id.id, detail=detail)


class WoowDocumentTrash(models.Model):
    _inherit = 'woow.document'

    def action_trash(self):
        """Move the documents to the trash."""
        self.check_access('write')
        for doc in self:
            if not doc.active:
                continue
            with self.env.cr.savepoint():
                super(WoowDocument, doc).write({
                    'active': False, 'trashed_date': fields.Datetime.now(),
                    'trashed_by_id': self.env.uid})
                _audit(doc, 'trash')
        return True

    def action_restore(self):
        """Restore the documents (wizard when the original folder is not usable)."""
        self.with_context(active_test=False).check_access('write')
        for doc in self.with_context(active_test=False):
            folder = doc.folder_id
            if not folder.exists() or folder.in_trash or not folder._woow_has_rank(2):
                return {'type': 'ir.actions.act_window',
                        'res_model': 'woow.document.restore.wizard',
                        'view_mode': 'form', 'target': 'new',
                        'context': {'default_document_ids': [(6, 0, self.ids)]}}
        for doc in self.with_context(active_test=False):
            if doc.active:
                continue
            with self.env.cr.savepoint():
                super(WoowDocument, doc).write({
                    'active': True, 'restored_date': fields.Datetime.now()})
                _audit(doc, 'restore')
        return True

    def action_purge(self):
        """Open the permanent deletion wizard (document administrators)."""
        self._require_manager()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'res_model': 'woow.document.purge.wizard',
                'view_mode': 'form', 'target': 'new',
                'context': {'default_document_ids': [(6, 0, self.ids)]}}

    def action_set_legal_hold(self, hold=None):
        """Set or release the legal hold (document administrators)."""
        self._require_manager()
        state = bool(self.env.context.get('woow_legal_hold', True)) if hold is None else bool(hold)
        for doc in self:
            with self.env.cr.savepoint():
                super(WoowDocument, doc).write({'legal_hold': state})
                _audit(doc, 'legal_hold', hold=state)
        return True

    def _woow_purge(self):
        """Delete permanently, deepest first, skipping held and locked records -> (done, failed)."""
        self._require_manager()
        done = failed = 0
        for doc in self.with_context(active_test=False):
            if doc.active or doc.legal_hold:
                failed += 1
                continue
            try:
                with self.env.cr.savepoint():
                    with bounded_wait(self.env.cr, LOCK_TIMEOUT_MS):
                        doc.unlink()
                    done += 1
            except (AccessError, UserError, LockBusy):
                failed += 1
        return done, failed


class WoowDocumentFolderTrash(models.Model):
    _inherit = 'woow.document.folder'

    def action_trash(self):
        """Move the folders and their subtree to the trash."""
        self._woow_require_rank(4)
        if any(folder.has_restricted_descendant for folder in self) and not self.env.user.has_group(
                'woow_document_management.group_woow_document_manager'):
            raise AccessError(_('A document administrator must trash this folder.'))
        for folder in self:
            if not folder.active:
                continue
            with self.env.cr.savepoint():
                super(WoowDocumentFolder, folder).write({
                    'active': False, 'trashed_date': fields.Datetime.now(),
                    'trashed_by_id': self.env.uid})
                _audit(folder, 'trash')
        return True

    def action_restore(self):
        """Restore the folders; requires manage (ER-13)."""
        self.with_context(active_test=False)._woow_require_rank(4)
        for folder in self.with_context(active_test=False):
            if folder.parent_id and folder.parent_id.in_trash:
                return {'type': 'ir.actions.act_window',
                        'res_model': 'woow.document.restore.wizard',
                        'view_mode': 'form', 'target': 'new',
                        'context': {'default_folder_ids': [(6, 0, self.ids)]}}
        for folder in self.with_context(active_test=False):
            if folder.active:
                continue
            with self.env.cr.savepoint():
                super(WoowDocumentFolder, folder).write({
                    'active': True, 'restored_date': fields.Datetime.now()})
                _audit(folder, 'restore')
        return True

    def action_purge(self):
        """Open the permanent deletion wizard (document administrators)."""
        self._require_manager()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'res_model': 'woow.document.purge.wizard',
                'view_mode': 'form', 'target': 'new',
                'context': {'default_folder_ids': [(6, 0, self.ids)]}}

    def action_set_legal_hold(self, hold=None):
        """Set or release the legal hold (document administrators)."""
        self._require_manager()
        state = bool(self.env.context.get('woow_legal_hold', True)) if hold is None else bool(hold)
        for folder in self:
            with self.env.cr.savepoint():
                super(WoowDocumentFolder, folder).write({'legal_hold': state})
                _audit(folder, 'legal_hold', hold=state)
        return True

    def _woow_purge(self):
        """Delete permanently, deepest first, skipping held and locked records -> (done, failed)."""
        self._require_manager()
        done = failed = 0
        for root in self.with_context(active_test=False):
            if root.active or root.subtree_legal_hold:
                failed += 1
                continue
            try:
                with self.env.cr.savepoint():
                    with bounded_wait(self.env.cr, LOCK_TIMEOUT_MS):
                        folders = self.with_context(active_test=False).search([
                            ('id', 'child_of', root.id)])
                        if any(folder.subtree_legal_hold for folder in folders):
                            raise UserError(_('This folder cannot be deleted.'))
                        docs = self.env['woow.document'].with_context(active_test=False).search([
                            ('folder_id', 'in', folders.ids)])
                        if any(doc.legal_hold for doc in docs):
                            raise UserError(_('This folder contains a protected document.'))
                        for doc in docs:
                            doc.unlink()
                        for folder in sorted(folders, key=lambda item: len(item.parent_path or ''),
                                             reverse=True):
                            folder.unlink()
                    done += 1
            except (AccessError, UserError, LockBusy):
                failed += 1
        return done, failed
