# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Sharing for people: expiry dates, roles on one document and the sharing dialogs (release 18.0.2).

Extension classes only. ``woow.document.share`` (``models/share.py``) holds one row per person a
document is shared with; this file adds what the other models need for it: the lists that the record
rules read (``shared_editor_user_ids``), the mirror of a direct change of the old lists into rows, the
expiry date of folder members, the cron that ends expired access and the two "Share" dialogs.
"""
import logging

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.constants import ACCESS_EXPIRY_BATCH

_logger = logging.getLogger(__name__)

_USER = 'woow_document_management.group_woow_document_user'
_SERVER_DOCUMENT_FIELDS = frozenset({'shared_editor_user_ids', 'woow_user_can_share'})
_ALL_USERS_LEVELS = [('none', 'No access'), ('read', 'View'), ('upload', 'Upload'),
                     ('edit', 'Edit'), ('manage', 'Manage')]


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
        # core caches only the active entries of a list that a write has just changed
        documents.invalidate_recordset(['shared_user_ids', 'shared_partner_ids'])
        shares._woow_lock_documents(documents.ids)
        rows = {}
        for row in shares._woow_rows(documents.ids):
            rows.setdefault(row[1], []).append(row)
        drop, add = [], []
        for doc in documents:
            # every stored entry counts, an archived user or contact included
            users, partners = set(doc.shared_user_ids.ids), set(doc.shared_partner_ids.ids)
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
        if drop:
            synced.browse(drop).unlink()
        if add:
            synced.create(add)
        shares._woow_sync_lists(documents)

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
        are rebuilt. At most ``ACCESS_EXPIRY_BATCH`` rows of each kind per run; the cron is run again
        at once while a kind is not finished. A row that cannot be removed is skipped and logged by
        its id, so that it does not stop the others.
        """
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        today = fields.Date.today()
        log = self.env['woow.document.log']
        members_model = self.env['woow.document.folder.access']
        shares_model = self.env['woow.document.share']._woow_synced()
        members = members_model.search([('expiry_date', '<', today)],
                                       order='expiry_date, id', limit=ACCESS_EXPIRY_BATCH)
        shares = shares_model.search([('expiry_date', '<', today)],
                                     order='expiry_date, id', limit=ACCESS_EXPIRY_BATCH)
        done = 0
        for row in members:
            try:
                with self.env.cr.savepoint():
                    values = {'folder_id': row.folder_id.id, 'company_id': row.company_id.id,
                              'detail': {'member_id': row.id}}
                    row.unlink()
                    log._log_event('access_expire', channel='cron', **values)
                    done += 1
            except Exception as error:  # noqa: BLE001 - one row never stops the run
                _logger.warning('Expired member %s was not removed (%s)', row.id,
                                type(error).__name__)
        for row in shares:
            try:
                with self.env.cr.savepoint():
                    values = {'document_id': row.document_id.id, 'folder_id': row.folder_id.id,
                              'company_id': row.company_id.id, 'detail': {'share_id': row.id}}
                    row.unlink()
                    log._log_event('access_expire', channel='cron', **values)
                    done += 1
            except Exception as error:  # noqa: BLE001 - one row never stops the run
                _logger.warning('Expired share %s was not removed (%s)', row.id,
                                type(error).__name__)
        more = len(members) == ACCESS_EXPIRY_BATCH or len(shares) == ACCESS_EXPIRY_BATCH
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
        return super().write(vals)

    def action_open_share_dialog(self):
        """Open the compact sharing dialog of this folder: its members and the all-users choice."""
        self._woow_require_rank(4)
        self.ensure_one()
        view = self.env.ref('woow_document_management.view_woow_folder_share_dialog')
        return {'type': 'ir.actions.act_window', 'name': _('Share'),
                'res_model': 'woow.document.folder', 'res_id': self.id, 'view_mode': 'form',
                'views': [(view.id, 'form')], 'target': 'new'}
