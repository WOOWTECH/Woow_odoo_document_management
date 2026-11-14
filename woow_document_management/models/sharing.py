# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Sharing for people: expiry dates, roles on one document and the sharing dialogs (release 18.0.2).

Extension classes only. ``woow.document.share`` (``models/share.py``) holds one row per person a
document is shared with; this file adds what the other models need for it: the lists that the record
rules read (``shared_editor_user_ids``), the mirror of a direct change of the old lists into rows, the
expiry date of folder members, the cron that ends expired access and the two "Share" dialogs.
"""
import logging
import time

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.constants import ACCESS_EXPIRY_BATCH
from .document import WoowDocument
from .document_folder import _stored_flag

_logger = logging.getLogger(__name__)

_USER = 'woow_document_management.group_woow_document_user'
# Time one run of the expiry cron may spend on removing rows (the cron comes back every 30 minutes).
ACCESS_EXPIRY_SECONDS = 120
_SERVER_DOCUMENT_FIELDS = frozenset({'shared_editor_user_ids', 'woow_user_can_share'})
_LEVEL_RANK = {'read': 1, 'upload': 2, 'edit': 3, 'manage': 4}
_SUBJECT_FIELD = {'user': 'user_id', 'group': 'group_id', 'partner': 'partner_id'}
_ALL_USERS_LEVELS = [('none', 'No access'), ('read', 'View'), ('upload', 'Upload'),
                     ('edit', 'Edit'), ('manage', 'Manage')]


def _merge_end(ends, key, rank, end):
    """Add a grant of ``rank`` to ``key`` in ``ends``: it ends when the last grant of that rank does,
    and never (``False``) when one of them has no end."""
    known = ends.setdefault(key, {})
    if rank not in known:
        known[rank] = end
    else:
        known[rank] = False if known[rank] is False or end is False else max(known[rank], end)


class WoowDocumentFolderAccessExpiry(models.Model):
    _inherit = 'woow.document.folder.access'

    expiry_date = fields.Date(
        string='Access ends', index='btree_not_null',
        help='The last day the member has access. Empty means no end.')

    @api.constrains('expiry_date')
    def _check_expiry_date(self):
        """A date that has passed is not a way to give access: the cron would remove the row at once."""
        today = fields.Date.today()
        if any(line.expiry_date and line.expiry_date < today for line in self):
            raise ValidationError(_('The end date cannot be in the past.'))

    def unlink(self):
        folders = self.folder_id
        result = super().unlink()
        # a member who lost the access to a folder does not keep a lock in it or below it
        self.env['woow.document']._woow_release_lost_locks(folder_ids=folders.ids)
        return result


class WoowDocumentSharing(models.Model):
    _inherit = 'woow.document'

    share_ids = fields.One2many('woow.document.share', 'document_id', groups='base.group_user')
    shared_editor_user_ids = fields.Many2many(
        'res.users', 'woow_document_shared_editor_rel', 'document_id', 'user_id', readonly=True,
        groups='base.group_user')
    woow_user_can_share = fields.Boolean(compute='_compute_woow_user_can_share')

    @api.depends_context('uid')
    def _compute_woow_user_can_share(self):
        """Whether the caller may change who the document is shared with (the rule of the lists)."""
        for doc in self:
            doc.woow_user_can_share = bool(
                isinstance(doc.id, int) and doc.folder_id and doc._filtered_access('write')
                and doc.folder_id._woow_has_rank(4))

    # -- the stored lists and the rows ----------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(_SERVER_DOCUMENT_FIELDS.intersection(vals) for vals in vals_list):
            raise AccessError(_('This document field is set by the server.'))
        listed = [bool({'shared_user_ids', 'shared_partner_ids'}.intersection(vals))
                  for vals in vals_list]
        documents = super().create(vals_list)
        if any(listed) and len(documents) == len(vals_list):
            wanted = {doc.id for doc, flag in zip(documents, listed) if flag}
            documents.filtered(lambda doc: doc.id in wanted)._woow_mirror_lists()
        return documents

    def write(self, vals):
        if vals and not self.env.su:
            if _SERVER_DOCUMENT_FIELDS.intersection(vals):
                raise AccessError(_('This document field is set by the server.'))
            if {'owner_id', 'folder_id'}.intersection(vals) and self._woow_share_only_editors():
                raise AccessError(_('A document that is only shared with you can be edited, '
                                    'not moved or given to someone else.'))
        mirrored = bool({'shared_user_ids', 'shared_partner_ids'}.intersection(vals))
        result = super().write(vals)
        if mirrored:
            self._woow_mirror_lists()
        return result

    def _woow_share_only_editors(self):
        """The documents of ``self`` that the caller can write only because they are shared as editor.

        An editor by share changes the content of the document; the owner and the folder stay as
        they are, unless the caller also holds the right to write the document through its folder
        (edit level, or owner with upload level), which is what the rules gave before 18.0.2.
        """
        uid = self.env.uid
        only = self.browse()
        for doc in self:
            if uid not in doc.shared_editor_user_ids.ids:
                continue
            folder = doc.folder_id
            if folder._woow_has_rank(3) or (doc.owner_id.id == uid and folder._woow_has_rank(2)):
                continue
            only |= doc
        return only

    def _woow_mirror_lists(self):
        """Turn the lists that a caller wrote into rows, then make the lists the rows' own.

        The new people of ``shared_user_ids`` and ``shared_partner_ids`` get a viewer row without an
        end; a person that left a list loses the row. The caller has already been judged by the
        write that changed the lists (it asks for the manage level of the folder), so the rows are
        made as server work. The lists are then rebuilt from the rows, which also takes an editor
        out of ``shared_editor_user_ids`` when the row is removed.
        """
        shares = self.env['woow.document.share']
        synced = shares._woow_synced()
        documents = self.exists().with_context(active_test=False)
        if not documents:
            return
        shares._woow_lock_documents(documents.ids)
        # the lists as they are stored: every entry counts, an archived person or a contact that the
        # caller's rules hide included (the field would leave them out, and they would lose the row)
        listed_users, listed_partners = shares._woow_listed(documents.ids)
        rows = {}
        for row in shares._woow_rows(documents.ids):
            rows.setdefault(row[1], []).append(row)
        drop, add = [], []
        for doc in documents:
            users, partners = listed_users.get(doc.id, set()), listed_partners.get(doc.id, set())
            have_users, have_partners = set(), set()
            for row_id, _document_id, user_id, partner_id, _role in rows.get(doc.id, ()):
                if user_id:
                    have_users.add(user_id)
                    if user_id not in users:
                        drop.append(row_id)
                else:
                    have_partners.add(partner_id)
                    if partner_id not in partners:
                        drop.append(row_id)
            for user_id in sorted(users - have_users):
                add.append({'document_id': doc.id, 'subject_type': 'user', 'user_id': user_id,
                            'role': 'viewer', 'expiry_date': False})
            for partner_id in sorted(partners - have_partners):
                add.append({'document_id': doc.id, 'subject_type': 'partner',
                            'partner_id': partner_id, 'role': 'viewer', 'expiry_date': False})
        changed = {}
        if drop:
            by_row = {row[0]: row[1] for rows_of in rows.values() for row in rows_of}
            for row_id in drop:
                changed.setdefault(by_row[row_id], []).append(row_id)
            synced.browse(drop).unlink()
        if add:
            for row in synced.create(add):
                changed.setdefault(row.document_id.id, []).append(row.id)
        shares._woow_sync_lists(documents)
        if changed:
            # the rows are made as server work, which writes no audit row of its own: the change of
            # who may read the document is recorded here, as a change of the rows themselves is
            shares._woow_audit(changed)

    # -- locks of people who lost their access -------------------------------------------------
    @api.model
    def _woow_release_lost_locks(self, document_ids=(), folder_ids=()):
        """Release the locks of the given documents, and of the documents in the given folders and
        below them, whose holder can no longer write the document.

        Server work after access was taken away (a share row or a member row was removed, which
        includes the expiry cron): the holder's own right is judged in the holder's environment, and
        the lock is cleared without the holder's rights, with an ``unlock`` row marked as forced.
        """
        document_ids, folder_ids = sorted(set(document_ids)), sorted(set(folder_ids))
        if not (document_ids or folder_ids):
            return
        self.env.flush_all()
        scope = []
        if document_ids:
            scope.append(('id', 'in', document_ids))
        if folder_ids:
            scope.append(('folder_id', 'child_of', folder_ids))
        domain = ['|'] * (len(scope) - 1) + scope + [('lock_uid', '!=', False)]
        everything = self.sudo().with_context(active_test=False)  # sudo-ok: S5
        locked = everything.search(domain)
        for doc in locked:
            if doc.with_user(doc.lock_uid).has_access('write'):  # sudo-ok: S5
                continue
            with self.env.cr.savepoint():
                super(WoowDocument, doc).write({'lock_uid': False, 'lock_date': False})
                self.env['woow.document.log']._log_event(
                    'unlock', channel=self._woow_log_channel(), document_id=doc.id,
                    folder_id=doc.folder_id.id, company_id=doc.company_id.id,
                    detail={'forced': True})

    # -- the dialog -----------------------------------------------------------------------------
    def action_open_share_dialog(self):
        """Open the compact sharing dialog: this document with its list of people."""
        self.check_access('write')
        self.ensure_one()
        self.folder_id._woow_require_rank(4)
        view = self.env.ref('woow_document_management.view_woow_document_share_dialog')
        return {'type': 'ir.actions.act_window', 'name': _('Share'),
                'res_model': 'woow.document', 'res_id': self.id, 'view_mode': 'form',
                'views': [(view.id, 'form')], 'target': 'new'}

    # -- expiry ---------------------------------------------------------------------------------
    @api.model
    def _cron_expire_access(self):
        """Every 30 minutes: remove folder members and shares whose last day has passed.

        Rows go through the normal ``unlink`` so that the stored lists that the record rules read
        are rebuilt, and so that the locks of people who lost their access are released. Rows are
        taken ``ACCESS_EXPIRY_BATCH`` at a time, batch after batch, until none is left or
        ``ACCESS_EXPIRY_SECONDS`` are used up (a batch is always finished, and each kind gets at
        least one); the cron is then run again at once. A row that cannot be removed is logged by its
        id once, left out of the rest of the run, and does not stop the rows behind it.
        """
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        today = fields.Date.today()
        log = self.env['woow.document.log']
        deadline = time.monotonic() + ACCESS_EXPIRY_SECONDS
        done, more = 0, False
        for model, kind in ((self.env['woow.document.folder.access'], 'member'),
                            (self.env['woow.document.share']._woow_synced(), 'share')):
            skipped = set()
            while True:
                domain = [('expiry_date', '<', today), ('id', 'not in', sorted(skipped))]
                rows = model.search(domain, order='expiry_date, id', limit=ACCESS_EXPIRY_BATCH)
                if not rows:
                    break
                for row in rows:
                    try:
                        with self.env.cr.savepoint():
                            if kind == 'member':
                                values = {'folder_id': row.folder_id.id,
                                          'company_id': row.company_id.id,
                                          'detail': {'member_id': row.id}}
                            else:
                                values = {'document_id': row.document_id.id,
                                          'folder_id': row.folder_id.id,
                                          'company_id': row.company_id.id,
                                          'detail': {'share_id': row.id}}
                            row.unlink()
                            log._log_event('access_expire', channel='cron', **values)
                            done += 1
                    except Exception as error:  # noqa: BLE001 - one row never stops the run
                        skipped.add(row.id)
                        _logger.warning('Expired %s %s was not removed (%s)', kind, row.id,
                                        type(error).__name__)
                if time.monotonic() >= deadline:
                    more = more or bool(model.search_count(
                        [('expiry_date', '<', today), ('id', 'not in', sorted(skipped))], limit=1))
                    break
        self.env['ir.cron']._notify_progress(done=done, remaining=1 if more else 0)


class WoowDocumentFolderSharing(models.Model):
    _inherit = 'woow.document.folder'

    woow_all_users_level = fields.Selection(
        _ALL_USERS_LEVELS, string='All document users', compute='_compute_woow_all_users_level',
        inverse='_inverse_woow_all_users_level', groups='base.group_user',
        help='What every document user may do in this folder, without being named one by one.')

    @api.depends('access_ids.level', 'access_ids.subject_type', 'access_ids.group_id')
    def _compute_woow_all_users_level(self):
        group = self.env.ref(_USER)
        for folder in self:
            line = folder.access_ids.filtered(
                lambda item: item.subject_type == 'group' and item.group_id == group)[:1]
            folder.woow_all_users_level = line.level if line else 'none'

    def _inverse_woow_all_users_level(self):
        """Create, change or remove the member row of the document user group, as a normal write.

        The rows are made through the model of folder members, so its guards apply as for any
        member: the manage level of the folder, the ceiling of the parent, the limit of members.
        """
        group = self.env.ref(_USER)
        access = self.env['woow.document.folder.access']
        for folder in self:
            level = folder.woow_all_users_level
            if level == 'manage':
                raise UserError(_('Choose no access, view, upload or edit.'))
            lines = access.search([('folder_id', '=', folder.id), ('subject_type', '=', 'group'),
                                   ('group_id', '=', group.id)])
            if not level or level == 'none':
                lines.unlink()
            elif lines:
                if lines.level != level:
                    lines.write({'level': level})
            else:
                access.create({'folder_id': folder.id, 'subject_type': 'group',
                               'group_id': group.id, 'level': level})

    def write(self, vals):
        if 'woow_all_users_level' in vals and not self.env.su:
            self._woow_require_rank(4)
        plan = None
        if 'inherit_access' in vals and not _stored_flag(vals, 'inherit_access', True):
            plan = self._woow_inherited_ends()
        result = super().write(vals)
        if plan:
            self._woow_carry_inherited_ends(plan)
        return result

    def _woow_inherited_ends(self):
        """Before inheritance is turned off: what ends, and when, of the access that the folders inherit.

        Returns {folder id: ({member id: level before}, {(subject, principal): {rank: last day}})}
        for the folders that inherit today. ``last day`` is ``False`` for a grant that never ends.
        The grants come from the member rows (and the owners) of the parent and of every folder
        above it as long as the folder in between inherits. The rows are read as stored, whatever
        the record rules of the caller: the caller may manage a folder below a parent that he does
        not read, and the copy of the access that turning inheritance off makes must not outlive it.
        """
        folders = self.filtered(lambda folder: folder.inherit_access and folder.parent_id)
        if not folders:
            return {}
        folders.flush_recordset(['inherit_access', 'parent_id', 'parent_path'])
        self.env['woow.document.folder.access'].flush_model()
        cr = self.env.cr
        cr.execute(  # sql-ok: Q65
            'SELECT id, parent_path FROM woow_document_folder WHERE id = ANY(%s)',
            (sorted(folders.ids),))
        paths = {row[0]: [int(part) for part in (row[1] or '').split('/') if part]
                 for row in cr.fetchall()}
        every = sorted({node for path in paths.values() for node in path})
        cr.execute(  # sql-ok: Q65
            'SELECT id, inherit_access, owner_id FROM woow_document_folder WHERE id = ANY(%s)',
            (every,))
        nodes = {row[0]: (bool(row[1]), row[2]) for row in cr.fetchall()}
        cr.execute(  # sql-ok: Q65
            'SELECT folder_id, subject_type, user_id, group_id, partner_id, level, expiry_date '
            'FROM woow_document_folder_access WHERE folder_id = ANY(%s)', (every,))
        lines = {}
        for row in cr.fetchall():
            lines.setdefault(row[0], []).append(row[1:])
        plan = {}
        for folder in folders:
            chain, path = [], paths.get(folder.id, [])[:-1]
            for node in reversed(path):
                chain.append(node)
                if not nodes.get(node, (False, None))[0]:
                    break
            ends = {}
            for node in chain:
                owner = nodes.get(node, (False, None))[1]
                if owner:
                    _merge_end(ends, ('user', owner), 4, False)
                for subject, user_id, group_id, partner_id, level, end in lines.get(node, ()):
                    principal = {'user': user_id, 'group': group_id, 'partner': partner_id}.get(subject)
                    if not principal or level not in _LEVEL_RANK:
                        continue
                    rank = min(_LEVEL_RANK[level], 2) if subject == 'partner' else _LEVEL_RANK[level]
                    _merge_end(ends, (subject, principal), rank, end or False)
            plan[folder.id] = ({line.id: line.level for line in folder.access_ids}, ends)
        return plan

    def _woow_carry_inherited_ends(self, plan):
        """After inheritance is turned off: the member rows that the copy made or raised get the end
        of the access they stand for, so that nobody keeps for good what was given until a day.

        A person may hold the same level from several rows above: the row ends when the last of
        them does, and never when one of them has no end. When the person also held a lower level
        without an end, that lower level is not kept past the end of the higher one: the copy is one
        row, and ending it is the safe way to be wrong.
        """
        today = fields.Date.today()
        for folder in self.exists():
            known = plan.get(folder.id)
            if not known:
                continue
            before, ends = known
            for line in folder.access_ids:
                subject = line.subject_type
                if subject not in _SUBJECT_FIELD:
                    continue
                if line.id in before and before[line.id] == line.level:
                    continue
                sources = ends.get((subject, line[_SUBJECT_FIELD[subject]].id))
                rank = _LEVEL_RANK.get(line.level, 0)
                if not sources or rank not in sources:
                    continue
                end = sources[rank]
                if end is False:
                    if line.id in before and line.expiry_date:
                        line.write({'expiry_date': False})
                elif end < today:
                    # that grant ended already and only waits for the cron: do not copy it
                    if line.id in before:
                        line.write({'level': before[line.id]})
                    else:
                        line.unlink()
                elif line.expiry_date != end:
                    line.write({'expiry_date': end})

    def action_open_share_dialog(self):
        """Open the compact sharing dialog of this folder: its members and the all-users choice."""
        self._woow_require_rank(4)
        self.ensure_one()
        view = self.env.ref('woow_document_management.view_woow_folder_share_dialog')
        return {'type': 'ir.actions.act_window', 'name': _('Share'),
                'res_model': 'woow.document.folder', 'res_id': self.id, 'view_mode': 'form',
                'views': [(view.id, 'form')], 'target': 'new'}
