# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Earlier versions of a file: the content a new upload or a restore replaced.

A version row keeps the content a document had, its file name and its type at that time. Rows are
made and removed only by the server paths of this file (a new version, a restore, a delete, the
purge of the document). The server marks such a call with an object that exists only in this
process (``_SWAP``, under the context key ``CTX_VERSION_SWAP``): a context sent by a client can
carry text and numbers, never this object. The record rules of the model (``security/versions_rules.xml``)
decide who may read a row, and refuse to remove one while its document is under a legal hold.
"""
import base64
import binascii

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.constants import CTX_VERSION_SWAP, MAX_VERSIONS_PER_DOCUMENT
from .document import WoowDocument
from .ir_attachment import NO_INDEX_CTX_KEY
from .upload_policy import WoowUploadRejected

_MANAGER = 'woow_document_management.group_woow_document_manager'

# The marker of a server call that makes or removes version rows.
_SWAP = object()


def _swap(records):
    """``records`` seen by the server paths that may make or remove version rows."""
    return records.with_context(**{CTX_VERSION_SWAP: _SWAP, NO_INDEX_CTX_KEY: True})


class WoowDocumentVersion(models.Model):
    _name = 'woow.document.version'
    _inherit = ['woow.document.guard.mixin']
    _description = 'Document version'
    _order = 'number desc, id desc'
    # Only the server fills in a version: no column takes a stored default.
    _woow_own_default_fields = frozenset({
        'document_id', 'number', 'name', 'mimetype', 'datas', 'file_size', 'checksum',
        'company_id', 'woow_can_manage'})
    _sql_constraints = [
        ('woow_document_version_number_uniq', 'UNIQUE(document_id, number)',
         'A document cannot have two versions with the same number.'),
        ('woow_document_version_number_positive', 'CHECK(number > 0)',
         'The number of a version starts at 1.'),
    ]

    document_id = fields.Many2one('woow.document', required=True, ondelete='cascade',
                                  index=True, readonly=True)
    number = fields.Integer(readonly=True, help='The first version of a document is number 1.')
    name = fields.Char(string='File name', required=True, readonly=True,
                       help='The name the file had while this content was current.')
    mimetype = fields.Char(readonly=True)
    datas = fields.Binary(string='File', attachment=True, readonly=True)
    file_size = fields.Integer(string='Size', readonly=True)
    checksum = fields.Char(readonly=True, index=True)
    company_id = fields.Many2one(related='document_id.company_id', store=True, index=True)
    woow_can_manage = fields.Boolean(string='Can change the document',
                                     compute='_compute_woow_can_manage')

    @api.depends_context('uid')
    def _compute_woow_can_manage(self):
        """Whether the caller may change the document of the row (shows the action buttons)."""
        writable = self.document_id._filtered_access('write')
        for version in self:
            version.woow_can_manage = version.document_id in writable

    @api.model_create_multi
    def create(self, vals_list):
        """Rows are made by the server paths of this file only."""
        if not self.env.su and self.env.context.get(CTX_VERSION_SWAP) is not _SWAP:
            raise AccessError(_('Versions are kept by the server.'))
        return super().create(vals_list)

    def write(self, vals):
        """A version is never edited: it is made, kept and removed."""
        if not self.env.su:
            raise AccessError(_('Versions cannot be changed.'))
        return super().write(vals)

    def unlink(self):
        """Rows are removed by the server paths of this file only."""
        if not self.env.su and self.env.context.get(CTX_VERSION_SWAP) is not _SWAP:
            raise AccessError(_('Versions are removed by the server.'))
        return super().unlink()

    def _woow_drop(self):
        """Remove the versions of ``self`` (the caller has judged the rights) and log each one."""
        log = self.env['woow.document.log']
        for version in self:
            document = version.document_id
            log._log_event(
                'version_delete', channel=document._woow_log_channel(),
                document_id=document.id, folder_id=document.folder_id.id,
                company_id=document.company_id.id, detail={'number': version.number})
        _swap(self).unlink()

    def action_download(self):
        """Download this version through its own route (the route checks the access again)."""
        self.check_access('read')
        self.ensure_one()
        return {'type': 'ir.actions.act_url',
                'url': '/woow_document/version/%d/download' % self.id,
                'target': 'download'}

    def action_restore(self):
        """Make this version the current content; the current content becomes a new version."""
        self.check_access('read')
        self.ensure_one()
        document = self.document_id
        document._woow_version_ready()
        document._woow_restore_version(self)
        return True

    def action_delete(self):
        """Delete these versions; refused while their document is under a legal hold.

        The document rows are locked before the hold is judged: a hold committed after the
        snapshot of this call changed the row of its document, so the call fails with a
        serialization error (and is run again) instead of removing a version that is held.
        """
        self.check_access('read')
        if len(self) > MAX_VERSIONS_PER_DOCUMENT:
            raise UserError(_('Select fewer versions.'))
        documents = self.document_id
        documents.check_access('write')
        documents._woow_lock_document_rows()
        for document in documents:
            document._woow_history_open()
        if not self.has_access('unlink'):
            # The caller may write the document, so the record rules refuse because of a hold.
            # Only a document administrator may read the hold, so only one is told about it.
            if self.env.su or self.env.user.has_group(_MANAGER):
                raise UserError(_('The versions of a document under legal hold cannot be deleted.'))
            raise UserError(_('These versions cannot be deleted.'))
        self._woow_drop()
        return True


class WoowDocumentVersions(models.Model):
    _inherit = 'woow.document'

    version_ids = fields.One2many('woow.document.version', 'document_id', string='Versions',
                                  readonly=True, groups='base.group_user')
    version_count = fields.Integer(string='Number of versions', compute='_compute_version_count',
                                   groups='base.group_user')
    version_last_number = fields.Integer(
        string='Last version number', readonly=True, copy=False, groups='base.group_user',
        help='The number the newest version took. A number is never given twice, even after '
             'the newest version was deleted.')

    @api.depends('version_ids')
    def _compute_version_count(self):
        ids = [doc.id for doc in self if isinstance(doc.id, int)]
        counts = {}
        if ids:
            counts = {doc.id: count for doc, count in self.env['woow.document.version']._read_group(
                [('document_id', 'in', ids)], ['document_id'], ['__count'])}
        for doc in self:
            doc.version_count = counts.get(doc.id, 0)

    def action_new_version(self):
        """Open the form that uploads a new version of this file."""
        self.check_access('write')
        self.ensure_one()
        self._woow_version_ready()
        return {'type': 'ir.actions.act_window', 'name': _('New version'),
                'res_model': 'woow.document.version.wizard', 'view_mode': 'form',
                'target': 'new', 'context': {'default_document_id': self.id}}

    @api.ondelete(at_uninstall=True)
    def _unlink_versions(self):
        """A permanent deletion takes the versions of the documents, and their files, with it.

        Core runs this before it deletes the rows, and after the guards of ``unlink`` have judged
        the call (administrator, trash, legal hold): a refused purge keeps every version. The
        database would remove the rows alone (``ondelete='cascade'``) and leave their files.
        """
        _swap(self.env['woow.document.version'].search(
            [('document_id', 'in', self.ids)])).unlink()

    def _woow_lock_holder(self):
        """The user who holds the lock of this file, or nobody (the lock is a separate feature)."""
        self.ensure_one()
        return self['lock_uid'] if 'lock_uid' in self._fields else self.env['res.users']

    def _woow_version_ready(self):
        """The conditions of a new version or a restore, the same for both.

        The caller may write the document; it is a file that is not in the trash; no other user
        holds its lock. The superuser and the server are not stopped by a lock.
        """
        self.ensure_one()
        self.check_access('write')
        if self.document_type != 'binary':
            raise UserError(_('Only a file has versions.'))
        self._woow_history_open()

    def _woow_history_open(self):
        """The history of this document may be changed now: it is not in the trash, not locked.

        Shared by the three actions that change a history (a new version, a restore, a delete).
        """
        self.ensure_one()
        if not self.active or self.folder_in_trash:
            raise AccessError(_('This document is unavailable.'))
        holder = self._woow_lock_holder()
        if holder and not self.env.su and holder != self.env.user:
            raise UserError(_('Another user has locked this file.'))

    def _woow_lock_document_rows(self):
        """Row locks on the rows of these documents, writing no row version.

        Taken before anything about the history is read or judged. Under Repeatable Read the
        statement fails with a serialization error when a transaction that committed after the
        snapshot of this call changed one of the rows (a legal hold, a trash, another upload),
        and a transaction that starts later waits for this one. The call is run again on a fresh
        snapshot. The ids are locked in order, so two calls cannot wait for each other.
        """
        ids = sorted({record_id for record_id in self.ids if isinstance(record_id, int)})
        if ids:
            self.env.cr.execute(  # sql-ok: Q31
                'SELECT id FROM woow_document WHERE id = ANY(%s) ORDER BY id FOR NO KEY UPDATE',
                (ids,))

    def _woow_add_version(self, raw, filename):
        """Store ``raw`` as the current content and keep the content it replaces as a version."""
        self.ensure_one()
        self._woow_version_ready()
        self._woow_push_version(raw, filename, quota=True, event='version_add', detail={})

    def _woow_restore_version(self, version):
        """Make ``version`` current; its content passes the upload policy again."""
        self.ensure_one()
        version.ensure_one()
        if version.document_id != self:
            raise AccessError(_('This version belongs to another document.'))
        try:
            raw = base64.b64decode(version.datas or b'', validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise WoowUploadRejected('content') from None
        if not raw:
            raise UserError(_('This version has no file content.'))
        # The bytes of a version are stored already: nothing is added to the quota.
        self._woow_push_version(raw, version.name, quota=False, event='version_restore',
                                detail={'restored': version.number}, keep=version)

    def _woow_push_version(self, raw, filename, *, quota, event, detail, keep=None):
        """Make ``raw`` the current content; the content it replaces becomes the newest version.

        The row of the document is locked first (``_woow_lock_document_rows``): two uploads of
        one file take turns, and the one that comes second is run again on a fresh snapshot, so
        it takes the next number. Everything that can be refused is judged before the first
        change: a document under legal hold keeps every version, so when the limit would remove
        one the call is refused; the file passes the upload policy and, for a new upload, the
        quota. The versions beyond ``MAX_VERSIONS_PER_DOCUMENT`` are removed oldest first, never
        ``keep`` (the version that a restore is putting back). The name of the document keeps its
        stem and takes the extension of the new file, so that a name never disagrees with the
        type of its content. A number is never used twice: it follows the counter of the
        document, not the highest row that is left.
        """
        self.ensure_one()
        self._woow_lock_document_rows()
        versions = self.env['woow.document.version']
        kept = versions.search([('document_id', '=', self.id)], order='number, id')
        droppable = kept - keep if keep else kept
        surplus = droppable[:max(0, len(kept) + 1 - MAX_VERSIONS_PER_DOCUMENT)]
        if surplus and not surplus.has_access('unlink'):
            # Only a document administrator may read the hold, so only one is told about it.
            if self.env.su or self.env.user.has_group(_MANAGER):
                raise UserError(_('This document is under legal hold, so none of its versions '
                                  'can be removed. It has reached the limit of versions.'))
            raise UserError(_('This document cannot take another version.'))
        current = self.datas
        if not current:
            raise UserError(_('This file has no content to keep as a version.'))
        policy = self.env['woow.document.upload.policy']
        checked = policy._validate(filename or self.name, raw, quota=quota)
        name = self._woow_name_for_content(policy, checked.name)
        number = max(self.version_last_number or 0, kept[-1].number if kept else 0) + 1
        earlier = {'document_id': self.id, 'number': number, 'name': self.name,
                   'mimetype': self.mimetype, 'datas': current, 'file_size': self.file_size,
                   'checksum': self.checksum}
        with self.env.cr.savepoint():
            super(WoowDocument, self.with_context(**{NO_INDEX_CTX_KEY: True})).write({
                'datas': base64.b64encode(raw), 'name': name, 'mimetype': checked.mimetype,
                'version_last_number': number})
            _swap(versions).create(earlier)
            if surplus:
                surplus._woow_drop()
            self.env['woow.document.log']._log_event(
                event, channel=self._woow_log_channel(), document_id=self.id,
                folder_id=self.folder_id.id, company_id=self.company_id.id,
                detail=dict(detail, number=number, bytes=len(raw), removed=len(surplus)))
            self.env.flush_all()

    def _woow_name_for_content(self, policy, uploaded_name):
        """The document name for new content: its own stem, with the extension of the new file."""
        stem, dot, ext = self.name.rpartition('.')
        new_ext = uploaded_name.rpartition('.')[2]
        if dot and ext.lower() == new_ext.lower():
            return self.name
        return policy._safe_name('%s.%s' % (stem if dot else self.name, new_ext))
