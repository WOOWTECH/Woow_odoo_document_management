# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Lock, duplicate and rename actions of documents.

A lock is a courtesy of one editor to the others: while it is held, nobody but its holder changes
the document. It is a plain pair of fields, written only by ``action_lock`` and ``action_unlock``
(the guards of ``document.py`` refuse them to every caller that is not the superuser), and it is
judged in ``write`` and ``unlink`` here, and in the trash actions of ``docactions_trash.py``.
This file is imported right after ``document.py`` and before ``trash_actions.py``, so that the
overrides of ``write`` and ``unlink`` sit directly on the document class. It never widens access: a user
who may not write a document cannot lock it, and a lock never lets anyone see or do more.
"""
from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.copy_names import copy_name
from .document import WoowDocument
from .quota import LEDGER_KEY, QuotaLedger

_MANAGER = 'woow_document_management.group_woow_document_manager'

# What a lock holds back from every other user (the ORM names of the fields).
LOCK_BLOCKED_FIELDS = frozenset({
    'name', 'datas', 'url', 'folder_id', 'tag_ids', 'description', 'document_type',
    'owner_id', 'partner_id', 'properties', 'active'})
LOCK_MAX_PER_CALL = 200                    # documents one lock or unlock call takes
DUPLICATE_MAX_PER_CALL = 20                # documents one duplicate call takes

# While a whole folder is deleted the documents in it are removed with it, locked or not. Only a
# value that is this very object (it cannot be sent over RPC) switches the lock check off.
_PURGE_KEY = 'woow_folder_purge'
_PURGE_MARK = object()


class WoowDocumentLock(models.Model):
    _inherit = 'woow.document'

    lock_uid = fields.Many2one('res.users', string='Locked by', readonly=True, copy=False,
                               ondelete='set null', index='btree_not_null',
                               groups='base.group_user')
    lock_date = fields.Datetime(string='Locked on', readonly=True, copy=False,
                                groups='base.group_user')
    is_locked = fields.Boolean(string='Locked', compute='_compute_is_locked',
                               search='_search_is_locked', groups='base.group_user')
    woow_locked_by_other = fields.Boolean(compute='_compute_locked_by_other',
                                          groups='base.group_user')

    @api.depends('lock_uid')
    def _compute_is_locked(self):
        for doc in self:
            doc.is_locked = bool(doc.lock_uid)

    @api.depends('lock_uid')
    @api.depends_context('uid')
    def _compute_locked_by_other(self):
        for doc in self:
            doc.woow_locked_by_other = bool(doc.lock_uid) and doc.lock_uid.id != self.env.uid

    def _search_is_locked(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(_('This filter is not supported.'))
        return [('lock_uid', '!=' if (operator == '=') == value else '=', False)]

    # -- the lock check ------------------------------------------------------------------------
    def _woow_refuse_foreign_lock(self, operation='write'):
        """UserError when another user holds the lock of one of the documents.

        The caller's own access is checked first, so that a document the caller cannot reach
        gets the answer it gets everywhere else. The superuser, the in-process folder purge and
        the lock holder are not held back.
        """
        if self.env.su or self.env.context.get(_PURGE_KEY) is _PURGE_MARK or not self:
            return
        self.check_access(operation)
        uid = self.env.uid
        if any(doc.lock_uid and doc.lock_uid.id != uid for doc in self):
            raise UserError(_('This document is locked by another user.'))

    def write(self, vals):
        """Refuse the changes that a lock holds back, unless the caller holds the lock."""
        if vals and self.env.user._is_internal() and LOCK_BLOCKED_FIELDS.intersection(vals):
            self._woow_refuse_foreign_lock('write')
        return super().write(vals)

    def unlink(self):
        """Refuse to delete a document that another user has locked."""
        self._require_manager()
        self._woow_refuse_foreign_lock('unlink')
        return super().unlink()

    # -- lock and unlock -----------------------------------------------------------------------
    def action_lock(self):
        """Lock the documents for the caller (needs write access to each)."""
        self.check_access('write')
        if len(self) > LOCK_MAX_PER_CALL:
            raise UserError(_('Select fewer documents.'))
        uid = self.env.uid
        for doc in self:
            if not doc.active or doc.folder_in_trash:
                raise AccessError(_('This document is unavailable.'))
            if doc.lock_uid and doc.lock_uid.id != uid:
                raise UserError(_('This document is locked by another user.'))
        now = fields.Datetime.now()
        for doc in self:
            if doc.lock_uid:
                continue
            with self.env.cr.savepoint():
                super(WoowDocument, doc).write({'lock_uid': uid, 'lock_date': now})
                self.env['woow.document.log']._log_event(
                    'lock', channel=self._woow_log_channel(), document_id=doc.id,
                    folder_id=doc.folder_id.id, company_id=doc.company_id.id)
        return True

    def action_unlock(self):
        """Release the lock: its holder, a manager of the folder or a document administrator."""
        self.with_context(active_test=False).check_access('read')
        if len(self) > LOCK_MAX_PER_CALL:
            raise UserError(_('Select fewer documents.'))
        uid = self.env.uid
        administrator = self.env.su or self.env.user.has_group(_MANAGER)
        for doc in self.with_context(active_test=False):
            if not doc.lock_uid:
                continue
            holder = doc.lock_uid.id == uid
            if not (holder or administrator or doc.folder_id._woow_has_rank(4)):
                raise AccessError(_('You cannot unlock this document.'))
            if not doc.has_access('write'):
                # The release is an ordinary write of the two lock columns (no elevation), so a
                # holder who lost write access gets a clear answer here instead of the generic
                # one from the ORM.
                raise UserError(_('This lock cannot be released from your account now. '
                                  'Ask a document administrator.'))
        for doc in self.with_context(active_test=False):
            if not doc.lock_uid:
                continue
            with self.env.cr.savepoint():
                holder = doc.lock_uid.id == uid
                super(WoowDocument, doc).write({'lock_uid': False, 'lock_date': False})
                self.env['woow.document.log']._log_event(
                    'unlock', channel=self._woow_log_channel(), document_id=doc.id,
                    folder_id=doc.folder_id.id, company_id=doc.company_id.id,
                    detail={'forced': not holder})
        return True

    # -- duplicate -----------------------------------------------------------------------------
    def action_duplicate(self):
        """Copy files and links into their own folders (read access, and upload rights there)."""
        self.check_access('read')
        if not self.env.su and not self.env.user._is_internal():
            raise AccessError(_('You cannot duplicate documents.'))
        if not self:
            raise UserError(_('Select at least one document.'))
        # The administrator's "files per upload" setting bounds a duplicate call as well: a copy is
        # one `create` per document, so the cap of `create` would never see more than one file.
        limit = DUPLICATE_MAX_PER_CALL
        if not self.env.su:
            limit = min(limit, self.env['woow.document.settings']._get_settings()
                        .max_files_per_upload)
        if len(self) > limit:
            raise UserError(_('Select at most %(count)s documents to duplicate at once.',
                              count=limit))
        for doc in self:
            if not doc.active or doc.folder_in_trash:
                raise AccessError(_('This document is unavailable.'))
            doc.folder_id._woow_require_rank(2)
            if doc.document_type not in ('binary', 'url'):
                raise UserError(_('This kind of document cannot be duplicated.'))
        suffix = _('(copy)')
        # One quota measurement for the whole call, and the bytes each copy adds as it goes.
        documents = self.env['woow.document'].with_context(**{LEDGER_KEY: QuotaLedger()})
        copies = self.browse()
        with self.env.cr.savepoint():
            for doc in self:
                is_file = doc.document_type == 'binary'
                vals = {
                    'name': copy_name(doc.name, suffix, keep_extension=is_file),
                    'folder_id': doc.folder_id.id, 'document_type': doc.document_type,
                    'owner_id': self.env.uid, 'tag_ids': [Command.set(doc.tag_ids.ids)],
                    'description': doc.description or False}
                if is_file:
                    data = doc.datas
                    if not data:
                        raise UserError(_('This document has no file to duplicate.'))
                    vals['datas'] = data
                else:
                    vals['url'] = doc.url
                copy = documents.create(vals)
                copies |= copy
                self.env['woow.document.log']._log_event(
                    'duplicate', channel=self._woow_log_channel(), document_id=copy.id,
                    folder_id=copy.folder_id.id, company_id=copy.company_id.id,
                    detail={'source_id': doc.id})
        action = {'type': 'ir.actions.act_window', 'res_model': 'woow.document',
                  'target': 'current'}
        if len(copies) == 1:
            return dict(action, view_mode='form', views=[(False, 'form')], res_id=copies.id)
        return dict(action, name=_('Copies'), view_mode='list,form',
                    views=[(False, 'list'), (False, 'form')],
                    domain=[('id', 'in', copies.ids)])

    # -- rename --------------------------------------------------------------------------------
    def action_open_rename_wizard(self):
        """Open the rename wizard for one document."""
        self.check_access('write')
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Rename'),
                'res_model': 'woow.document.rename.wizard', 'view_mode': 'form',
                'target': 'new', 'context': {'default_document_id': self.id}}


class WoowDocumentFolderRename(models.Model):
    _inherit = 'woow.document.folder'

    def action_open_rename_wizard(self):
        """Open the rename wizard for one folder."""
        self._woow_require_rank(3)
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Rename'),
                'res_model': 'woow.document.rename.wizard', 'view_mode': 'form',
                'target': 'new', 'context': {'default_folder_id': self.id}}
