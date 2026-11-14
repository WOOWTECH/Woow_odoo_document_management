# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""People a document is shared with directly (release 18.0.2, lane ``sharing``).

One row is one person (an internal document user, or a portal contact) with a role and an optional
last day. The rows are the source of three stored lists of the document that the record rules read:
``shared_user_ids``, ``shared_partner_ids`` and ``shared_editor_user_ids``. Server code aligns the
lists whenever a row changes (``_woow_sync_lists``), and a direct change of the first two lists is
mirrored into rows (``woow.document._woow_mirror_lists``), so that both ways of sharing stay one.

Who may create, change or delete a row is who may change the lists of that document in release
18.0.1: the caller must be able to write the document and must hold the manage level on its folder.
That check (``_woow_require_manage``) runs before anything is read or locked.
"""
from odoo import Command, _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.constants import CTX_SHARE_SYNC, LOCK_TIMEOUT_MS, SHARE_ROWS_PER_DOCUMENT_MAX
from ..services.locks import LockBusy, bounded_wait
from .document import WoowDocument
from .document_folder import _m2o_value

_USER = 'woow_document_management.group_woow_document_user'

# The in-process marker of server code that aligns rows and lists. A context key of this name that
# a client sends over RPC is a plain value and is never this object, so it is not honoured.
SHARE_SYNC = object()

_M2O_FIELDS = (('document_id', 'woow.document'), ('user_id', 'res.users'),
               ('partner_id', 'res.partner'))
_SERVER_FIELDS = frozenset({'company_id', 'folder_id'})
_LIST_FIELDS = ['shared_user_ids', 'shared_partner_ids', 'shared_editor_user_ids']


class WoowDocumentShare(models.Model):
    _name = 'woow.document.share'
    _inherit = ['woow.document.guard.mixin']
    _description = 'Document share'
    _order = 'document_id, id'
    _woow_allowed_defaults = frozenset({'default_document_id'})
    # the folder and the company come from the document; a stored default must not choose a role,
    # a kind of person or a last day for a row that the caller did not describe
    _woow_own_default_fields = frozenset({
        'company_id', 'folder_id', 'subject_type', 'role', 'expiry_date'})
    _sql_constraints = [
        ('subject_exactly_one',
         "CHECK ((subject_type = 'user' AND user_id IS NOT NULL AND partner_id IS NULL) OR "
         "(subject_type = 'partner' AND user_id IS NULL AND partner_id IS NOT NULL))",
         'Select exactly one person of the chosen type.'),
        ('portal_viewer_only', "CHECK (subject_type <> 'partner' OR role = 'viewer')",
         'Portal contacts can only view.'),
    ]

    document_id = fields.Many2one('woow.document', required=True, index=True, ondelete='cascade')
    folder_id = fields.Many2one(related='document_id.folder_id', store=True, index=True)
    company_id = fields.Many2one(related='document_id.company_id', store=True, index=True)
    subject_type = fields.Selection(
        [('user', 'Document user'), ('partner', 'Portal contact')], required=True, default='user')
    user_id = fields.Many2one('res.users', ondelete='cascade')
    partner_id = fields.Many2one('res.partner', ondelete='cascade')
    role = fields.Selection([('viewer', 'Viewer'), ('editor', 'Editor')], required=True,
                            default='viewer')
    expiry_date = fields.Date(string='Access ends', index='btree_not_null',
                              help='The last day the person has access. Empty means no end.')

    def init(self):
        super().init()
        tools.create_unique_index(
            self.env.cr, 'woow_document_share_person_uniq', self._table,
            ['document_id', 'COALESCE(user_id,0)', 'COALESCE(partner_id,0)'])

    # -- helpers --------------------------------------------------------------------------------
    def _woow_syncing(self):
        """True for the server code that aligns rows and lists (its caller was already judged)."""
        return self.env.context.get(CTX_SHARE_SYNC) is SHARE_SYNC

    def _woow_synced(self):
        """``self`` marked as server work that was judged by its caller."""
        return self.with_context(**{CTX_SHARE_SYNC: SHARE_SYNC})

    @api.model
    def _woow_rows(self, document_ids):
        """(id, document, user, partner, role) of every row of the documents, whatever the caller's rules.

        The rows are the source of the lists that the record rules read, so the lists must be
        built from all of them, not from the ones that the caller happens to read.
        """
        ids = sorted({int(value) for value in document_ids})
        if not ids:
            return []
        self.flush_model(['document_id', 'user_id', 'partner_id', 'role'])
        self.env.cr.execute(  # sql-ok: Q61
            'SELECT id, document_id, user_id, partner_id, role FROM woow_document_share '
            'WHERE document_id = ANY(%s)', (ids,))
        return self.env.cr.fetchall()

    @api.model
    def _woow_listed(self, document_ids):
        """The people that the two old lists name, whatever the caller's rules: ({doc: users}, {doc: partners}).

        The lists are stored in two relation tables and an archived person is in them too. Reading
        them through the field would drop the people that the caller's record rules hide (a contact
        of another company, say), and the mirror would take their access away.
        """
        ids = sorted({int(value) for value in document_ids})
        users, partners = {}, {}
        if not ids:
            return users, partners
        self.env['woow.document'].flush_model(['shared_user_ids', 'shared_partner_ids'])
        self.env.cr.execute(  # sql-ok: Q64
            'SELECT document_id, user_id FROM woow_document_shared_user_rel '
            'WHERE document_id = ANY(%s)', (ids,))
        for document_id, user_id in self.env.cr.fetchall():
            users.setdefault(document_id, set()).add(user_id)
        self.env.cr.execute(  # sql-ok: Q64
            'SELECT document_id, partner_id FROM woow_document_shared_partner_rel '
            'WHERE document_id = ANY(%s)', (ids,))
        for document_id, partner_id in self.env.cr.fetchall():
            partners.setdefault(document_id, set()).add(partner_id)
        return users, partners

    @api.model
    def _woow_require_manage(self, document_ids):
        """AccessError unless the caller may change who these documents are shared with.

        The same test as the change of ``shared_user_ids`` and ``shared_partner_ids`` has in release
        18.0.1: the caller can write the document, and holds the manage level on its folder. A
        document that is missing and one that the caller may not use answer the same.
        """
        if self.env.su:
            return
        ids = sorted(set(document_ids))
        message = _('You cannot change who this document is shared with.')
        documents = self.env['woow.document'].with_context(active_test=False).browse(ids).exists()
        if len(documents) != len(ids):
            raise AccessError(message)
        try:
            documents.check_access('write')
            documents.folder_id._woow_require_rank(4)
        except AccessError:
            raise AccessError(message) from None

    @api.model
    def _woow_lock_documents(self, document_ids):
        """Serialize the changes of the shares of the same documents (rows are then read in a stable state).

        A row lock on each document, taken in id order. Under the repeatable read of a request, a
        document that another transaction changed since the snapshot fails with a serialization error
        and the request is retried, so two changes of one document never build the lists from stale rows.
        """
        ids = sorted(set(document_ids))
        if not ids:
            return
        try:
            with bounded_wait(self.env.cr, LOCK_TIMEOUT_MS):
                self.env.cr.execute(  # sql-ok: Q62
                    'UPDATE woow_document SET id = id WHERE id = ANY(%s)', (ids,))
        except LockBusy:
            raise UserError(_('Another change to this document is in progress. Try again.')) from None

    @api.model
    def _woow_sync_lists(self, documents):
        """Make the stored lists of ``documents`` the ones the rows say.

        ``shared_user_ids`` is every user with a row, ``shared_partner_ids`` every contact with a row and
        ``shared_editor_user_ids`` the users whose row says editor. The lists are written below the
        document's own ``write`` (as the trash actions do): the caller was judged already, and that
        write would only judge the same fields again. Only a list that differs is written.
        """
        # Every stored entry counts, an archived user or contact included: the lists are read and
        # written with ``active_test`` off, after the values that a write cached (core caches only the
        # active ones) are dropped.
        documents = documents.exists().with_context(active_test=False)
        if not documents:
            return
        documents.invalidate_recordset(_LIST_FIELDS)
        users, partners, editors = {}, {}, {}
        for _row_id, document_id, user_id, partner_id, role in self._woow_rows(documents.ids):
            if user_id:
                users.setdefault(document_id, set()).add(user_id)
                if role == 'editor':
                    editors.setdefault(document_id, set()).add(user_id)
            elif partner_id:
                partners.setdefault(document_id, set()).add(partner_id)
        for document in documents:
            values = {}
            for name, wanted in (('shared_user_ids', users), ('shared_partner_ids', partners),
                                 ('shared_editor_user_ids', editors)):
                target = sorted(wanted.get(document.id, ()))
                if sorted(document[name].ids) != target:
                    values[name] = [Command.set(target)]
            if values:
                super(WoowDocument, document).write(values)

    def _woow_audit(self, changed):
        """One ``access_change`` row per document, with the ids of the rows that changed (ids only).

        ``changed`` is {document id: [row ids]}.
        """
        documents = self.env['woow.document'].with_context(active_test=False).browse(
            sorted(changed)).exists()
        for document in documents:
            self.env['woow.document.log']._log_event(
                'access_change', channel=self._woow_log_channel(), document_id=document.id,
                folder_id=document.folder_id.id, company_id=document.company_id.id,
                detail={'share_ids': sorted(changed[document.id])[:500]})

    # -- checks of the values -------------------------------------------------------------------
    @api.model
    def _woow_typed_values(self, vals):
        """``vals`` with each many2one read as the ORM keeps it (an id or False), or a refusal.

        The same reading as every other guard of the module (``_m2o_value``): what has no id, and a
        number below 1, is refused, so the value that is judged is the one that is stored.
        """
        vals = dict(vals)
        for name, comodel in _M2O_FIELDS:
            if name in vals:
                value = _m2o_value(vals[name], comodel)
                if value is None or (value is not False and value < 1):
                    raise ValidationError(_('Choose an available person.')
                                          if name != 'document_id'
                                          else _('Choose an available document.'))
                vals[name] = value
        return vals

    @api.model
    def _woow_check_shape(self, subject_type, user_id, partner_id, role):
        """The person and the role that a row will hold: one person of the kind, a portal contact views."""
        if subject_type == 'user':
            if not user_id or partner_id:
                raise ValidationError(_('Select exactly one person of the chosen type.'))
        elif subject_type == 'partner':
            if not partner_id or user_id:
                raise ValidationError(_('Select exactly one person of the chosen type.'))
            if role != 'viewer':
                raise ValidationError(_('Portal contacts can only view.'))
        else:
            raise ValidationError(_('Select exactly one person of the chosen type.'))

    def _woow_check_people_free(self, resulting, exclude=()):
        """No document is given a row twice for the same person (``resulting``: [(document, user, partner)])."""
        seen = set()
        for key in resulting:
            if key in seen:
                raise ValidationError(_('This person already has a row for this document.'))
            seen.add(key)
        taken = {(row[1], row[2] or 0, row[3] or 0)
                 for row in self._woow_rows({key[0] for key in resulting}) if row[0] not in exclude}
        if any((doc, user or 0, partner or 0) in taken for doc, user, partner in resulting):
            raise ValidationError(_('This person already has a row for this document.'))

    # -- create, write, unlink ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        sync = self._woow_syncing()
        vals_list = [dict(vals) for vals in vals_list]
        for vals in vals_list:
            if 'subject_type' not in vals:
                vals['subject_type'] = 'partner' if vals.get('partner_id') \
                    and not vals.get('user_id') else 'user'
        if not (self.env.su or sync):
            self.check_access('create')
            if len(vals_list) > SHARE_ROWS_PER_DOCUMENT_MAX:
                raise UserError(_('Too many people at once.'))
            default_document = self.default_get(['document_id']).get('document_id') \
                if any('document_id' not in vals for vals in vals_list) else None
            resulting = []
            for index, vals in enumerate(vals_list):
                if _SERVER_FIELDS.intersection(vals):
                    raise AccessError(_('This field is set by the server.'))
                vals = vals_list[index] = self._woow_typed_values(vals)
                if not vals.get('document_id'):
                    vals['document_id'] = default_document or False
                if not vals['document_id']:
                    raise ValidationError(_('Choose an available document.'))
                vals.setdefault('role', 'viewer')
                self._woow_check_shape(vals['subject_type'], vals.get('user_id'),
                                       vals.get('partner_id'), vals['role'])
                resulting.append((vals['document_id'], vals.get('user_id') or False,
                                  vals.get('partner_id') or False))
            document_ids = {key[0] for key in resulting}
            self._woow_require_manage(document_ids)
            self._woow_lock_documents(document_ids)
            self._woow_check_people_free(resulting)
        records = super().create(vals_list)
        records._woow_check_rows()
        self._woow_sync_lists(records.document_id)
        if not sync:
            changed = {}
            for row in records:
                changed.setdefault(row.document_id.id, []).append(row.id)
            records._woow_audit(changed)
        return records

    def write(self, vals):
        sync = self._woow_syncing()
        if vals and not (self.env.su or sync):
            if _SERVER_FIELDS.intersection(vals):
                raise AccessError(_('This field is set by the server.'))
            self.check_access('write')
            vals = self._woow_typed_values(vals)
            if 'document_id' in vals and any(row.document_id.id != vals['document_id']
                                             for row in self):
                raise UserError(_('The document of a share cannot be changed.'))
            documents = self.document_id
            self._woow_require_manage(documents.ids)
            self._woow_lock_documents(documents.ids)
            resulting = []
            for row in self:
                subject_type = vals.get('subject_type', row.subject_type)
                user_id = vals['user_id'] if 'user_id' in vals else row.user_id.id
                partner_id = vals['partner_id'] if 'partner_id' in vals else row.partner_id.id
                self._woow_check_shape(subject_type, user_id or False, partner_id or False,
                                       vals.get('role', row.role))
                resulting.append((row.document_id.id, user_id or False, partner_id or False))
            self._woow_check_people_free(resulting, exclude=set(self.ids))
        documents = self.document_id
        result = super().write(vals)
        if vals:
            if 'document_id' in vals:
                self._woow_check_rows()     # a change of the person or the role adds no row
            self._woow_sync_lists(documents)
            if not sync:
                changed = {}
                for row in self:
                    changed.setdefault(row.document_id.id, []).append(row.id)
                self._woow_audit(changed)
        return result

    def unlink(self):
        sync = self._woow_syncing()
        if not (self.env.su or sync):
            self.check_access('unlink')
        documents = self.document_id
        if not (self.env.su or sync):
            self._woow_require_manage(documents.ids)
            self._woow_lock_documents(documents.ids)
        changed = {}
        for row in self:
            changed.setdefault(row.document_id.id, []).append(row.id)
        result = super().unlink()
        self._woow_sync_lists(documents)
        if not sync:
            self._woow_audit(changed)
        return result

    # -- constraints ----------------------------------------------------------------------------
    @api.constrains('subject_type', 'user_id', 'partner_id', 'role')
    def _check_subject(self):
        """A document user, or a contact with an active portal account who can only view.

        A row that the server makes from a direct change of the lists is not judged here: release
        18.0.1 took any user or contact in those lists, and the rows only mirror them.
        """
        if self._woow_syncing():
            return
        for row in self:
            if row.subject_type == 'user':
                if not row.user_id._is_internal() or not row.user_id.has_group(_USER):
                    raise ValidationError(_('Choose a document user.'))
            elif row.subject_type == 'partner':
                if row.role != 'viewer' or not any(
                        user.active and user._is_portal() for user in row.partner_id.user_ids):
                    raise ValidationError(_('Choose a contact with an active portal account.'))

    @api.constrains('expiry_date')
    def _check_expiry_date(self):
        today = fields.Date.today()
        if any(row.expiry_date and row.expiry_date < today for row in self):
            raise ValidationError(_('The end date cannot be in the past.'))

    def _woow_check_rows(self):
        """A document is shared with at most ``SHARE_ROWS_PER_DOCUMENT_MAX`` people."""
        counts = {}
        for _row_id, document_id, _user, _partner, _role in self._woow_rows(self.document_id.ids):
            counts[document_id] = counts.get(document_id, 0) + 1
        if any(count > SHARE_ROWS_PER_DOCUMENT_MAX for count in counts.values()):
            raise ValidationError(_('A document cannot be shared with more than %(count)s people.',
                                    count=SHARE_ROWS_PER_DOCUMENT_MAX))

    @api.onchange('subject_type')
    def _onchange_subject_type(self):
        for row in self:
            if row.subject_type == 'partner':
                row.user_id = False
                row.role = 'viewer'
            else:
                row.partner_id = False
