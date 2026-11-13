# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Explicit trash transitions after checking the caller's live access."""
import logging

from psycopg2 import errors

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError
from ..services.locks import LockBusy, bounded_wait
from ..services.constants import LOCK_TIMEOUT_MS

from .document import WoowDocument
from .document_folder import WoowDocumentFolder

_logger = logging.getLogger(__name__)

# A record that cannot be purged now is skipped, never the whole run: a refused
# check, a lock wait that timed out, a row that another transaction changed
# after this snapshot (a legal hold set meanwhile) or that deadlocked with it, or a
# delete the database refuses because a row that goes with it violates a constraint.
_PURGE_SKIPPED = (AccessError, UserError, LockBusy, errors.SerializationFailure,
                  errors.DeadlockDetected, errors.IntegrityError)


def _skipped(kind, record, error, failures=None):
    """Note a skipped record: a refused delete or an unexpected error is logged (ids only).

    The class of an unexpected error is logged, never its text, which may name data.
    ``failures`` collects (model, id) of the skipped records for the caller.
    """
    if isinstance(error, errors.IntegrityError):
        _logger.warning('Purge skipped a %s the database refused to delete: id %s',
                        kind, record.id)
    elif not isinstance(error, _PURGE_SKIPPED):
        _logger.warning('Purge skipped a %s after an unexpected error: id %s (%s)',
                        kind, record.id, type(error).__name__)
    if failures is not None:
        failures.append((record._name, record.id))


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

    def _woow_purge(self, failures=None):
        """Delete permanently, skipping held and locked records -> (done, failed).

        A document already removed with a folder earlier in the same run is skipped
        and counts as neither. The hold is read from this transaction's snapshot and
        the delete writes the same row, so a hold committed after the snapshot makes
        the delete fail with serialization_failure: that document counts as failed.
        Any other error of one record skips only that record as well (R2b-06); with
        ``failures`` (a list) the skipped records are appended to it as (model, id).
        """
        self._require_manager()
        done = failed = 0
        for doc in self.with_context(active_test=False):
            if not doc.exists():
                continue
            try:
                with self.env.cr.savepoint():
                    if doc.active or doc.legal_hold:
                        raise UserError(_('This item cannot be deleted permanently.'))
                    with bounded_wait(self.env.cr, LOCK_TIMEOUT_MS):
                        doc.unlink()
                    done += 1
            except Exception as error:  # noqa: BLE001 - one record never stops the run
                failed += 1
                _skipped('document', doc, error, failures)
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

    def _woow_purge(self, budget=None):
        """Delete permanently, deepest first, skipping held and locked records -> (done, failed).

        ``done`` counts the folders removed with their whole subtree. With ``budget``
        at most that many records (documents and folders) are deleted; a subtree
        that does not fit is left partly deleted and is continued by the next call.
        A folder already removed with an ancestor in the same run is skipped and
        counts as neither. A root whose subtree (any folder in it, trashed on its
        own or not) a legal hold, restore or move committed after this
        transaction's snapshot has changed counts as failed and nothing of it is
        deleted.
        """
        done, failed, _deleted = self._woow_purge_batch(budget)
        return done, failed

    def _woow_purge_batch(self, budget=None, failures=None):
        """``_woow_purge`` that also returns the number of records deleted.

        The rows of each root and of every folder below it are locked before any
        hold is read (``_woow_lock_purge_root``), so a batch that deletes only
        documents or only the deepest folders cannot miss a folder hold committed
        meanwhile, wherever in the subtree it was set.
        """
        self._require_manager()
        done = failed = deleted = 0
        for root in self.with_context(active_test=False):
            if budget is not None and deleted >= budget:
                break
            if not root.exists():
                continue
            try:
                with self.env.cr.savepoint(), bounded_wait(self.env.cr, LOCK_TIMEOUT_MS):
                    root._woow_lock_purge_root()
                    if root.active or root.subtree_legal_hold:
                        raise UserError(_('This item cannot be deleted permanently.'))
                    count, finished = root._woow_purge_tree(
                        None if budget is None else budget - deleted)
                deleted += count
                done += finished
            except Exception as error:  # noqa: BLE001 - one record never stops the run
                failed += 1
                _skipped('folder', root, error, failures)
        return done, failed, deleted

    def _woow_lock_purge_root(self):
        """Row locks on a purge root and every folder below it, writing no row version.

        This is the barrier for stale snapshots. A legal hold, a restore or a move
        of any folder of the subtree, trashed on its own or not, updates that
        folder's row. Under Repeatable Read, SELECT ... FOR NO KEY UPDATE fails
        with serialization_failure when a transaction that committed after this
        snapshot changed one of the rows, and such a change that starts later
        waits until this transaction ends. The subtree is searched with trashed
        folders included and locked in id order, so the barrier does not depend
        on how the stored ``subtree_legal_hold`` reaches the root. Holds on
        documents need no barrier: deleting a document writes the row that its
        hold changed.
        """
        self.ensure_one()
        subtree = self.with_context(active_test=False).search([('id', 'child_of', self.id)])
        self.env.cr.execute(  # sql-ok: Q21
            'SELECT id FROM woow_document_folder WHERE id = ANY(%s) '
            'ORDER BY id FOR NO KEY UPDATE', (subtree.ids or [self.id],))

    def _woow_purge_tree(self, limit=None):
        """Delete this trashed folder's documents, then its folders deepest first.

        Returns (records deleted, 1 when the folder itself is gone else 0). With
        ``limit`` no more than that many records are deleted in this call. The
        holds are read from the snapshot, so the caller first locks the rows of
        this folder and of every folder below it (``_woow_lock_purge_root``).
        """
        self.ensure_one()
        folders = self.with_context(active_test=False).search([('id', 'child_of', self.id)])
        if any(folder.subtree_legal_hold for folder in folders):
            raise UserError(_('This folder cannot be deleted.'))
        docs = self.env['woow.document'].with_context(active_test=False).search(
            [('folder_id', 'in', folders.ids)], order='id')
        if any(doc.legal_hold for doc in docs):
            raise UserError(_('This folder contains a protected document.'))
        deleted = 0
        if docs:
            chunk = docs if limit is None else docs[:limit]
            chunk.unlink()
            deleted = len(chunk)
            if len(chunk) < len(docs):
                return deleted, 0
        for folder in sorted(folders, key=lambda item: (len(item.parent_path or ''), item.id),
                             reverse=True):
            if limit is not None and deleted >= limit:
                return deleted, 0
            folder.unlink()
            deleted += 1
        return deleted, 1
