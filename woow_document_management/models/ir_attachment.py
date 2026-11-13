# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Narrow attachment guard for document fields and for what makes a file reachable without a session.

The attachments of this module's records (``res_model`` in ``WOOW_MODELS``) are never public and
never hold an access token: after any ``create`` or ``write``, in every environment, the superuser's
included, both columns of such a row are empty. A caller who is not the superuser is refused with
``AccessError``. The superuser is not: core makes and moves the files of a post as superuser and
those calls must not fail, so what would open the row is taken out of the values instead.
"""
from odoo import _, api, models
from odoo.exceptions import AccessError

from ..services.constants import WOOW_MODELS
from .guard_mixin import own_default_values

GUARDED_WRITE_KEYS = ('raw', 'datas', 'db_datas', 'mimetype', 'type', 'url', 'res_model', 'res_id',
                      'res_field', 'public', 'access_token', 'store_fname')
# What makes a file reachable without a session: the public flag of core and an access token.
OPEN_KEYS = ('public', 'access_token')
# A context with this key set has core store no indexed content for the file it saves.
NO_INDEX_CTX_KEY = 'woow_no_index'
# The file of a binary field of a document or folder is made as superuser, under the uid of the
# caller; only the server sets what makes it reachable (it is never public or tokenised).
FIELD_FILE_OWN_DEFAULTS = frozenset({
    'public', 'access_token', 'url', 'description', 'original_id', 'company_id'})


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model
    def _index(self, bin_data, file_type, checksum=None):
        """The files of documents are not indexed by core: their ``index_content`` stays empty.

        The indexers of ``attachment_indexation`` unpack and parse the members of Office and
        OpenDocument archives (of any file type, at any declared size) while the file is saved,
        and a few kilobytes of upload can take the memory and time of a worker. What is read from
        a document file is for the upload policy, or a search bridge, to decide.
        """
        if self.env.context.get(NO_INDEX_CTX_KEY):
            return False
        return super()._index(bin_data, file_type, checksum=checksum)

    @api.model
    def _add_missing_default_values(self, values):
        """The file of a document or folder field takes the own default of the fields it sets."""
        completed = super()._add_missing_default_values(values)
        if values.get('res_model') in WOOW_MODELS and values.get('res_field'):
            completed = own_default_values(self, values, completed, FIELD_FILE_OWN_DEFAULTS)
        return completed

    @api.model_create_multi
    def create(self, vals_list):
        """A row of a record of this module is created without the public flag and without a token.

        Core checks only explicit res_model/res_id before ORM defaults are applied. Resolve the
        same defaults now, so contextual values cannot bypass either the document guard or core's
        write check on the target record. A caller who is not the superuser is refused when the
        row will belong to a module model and be public, tokenised or back a field. The superuser
        is not refused, core makes the files of a post as superuser (those that the body of a post
        refers to are created with a token that core has just made): the row is created with the
        flag and the token set to ``False``, whether the values or a default would set them.
        """
        keys = ('res_model', 'res_id', 'res_field', 'public', 'access_token')
        defaults = self.default_get(list(keys))
        checked = []
        for incoming in vals_list:
            effective = {key: incoming.get(key, defaults.get(key)) for key in keys}
            vals = dict(incoming)
            if effective['res_model'] in WOOW_MODELS:
                opens = effective['public'] or effective['access_token']
                if not self.env.su and (effective['res_field'] or opens):
                    raise AccessError(_('Document attachments must be created through the document.'))
                if opens:
                    vals.update(dict.fromkeys(OPEN_KEYS, False))
            for key in ('res_model', 'res_id'):
                if key not in vals and key in defaults:
                    vals[key] = defaults[key]
            checked.append(vals)
        return super().create(checked)

    def write(self, vals):
        if not self.env.su and any(key in vals for key in GUARDED_WRITE_KEYS):
            if vals.get('res_model') in WOOW_MODELS and vals.get('res_field'):
                raise AccessError(_('Document field attachments cannot be reassigned.'))
            self.env.cr.execute(  # sql-ok: Q6
                'SELECT id FROM ir_attachment WHERE id = ANY(%s) '
                'AND res_model = ANY(%s) AND res_field IS NOT NULL',
                (self.ids, list(WOOW_MODELS)))
            if self.env.cr.fetchone() and any(key in vals for key in GUARDED_WRITE_KEYS):
                raise AccessError(_('Document file attachments cannot be changed directly.'))
            metadata = {'public', 'access_token', 'res_model', 'res_id', 'res_field'}
            if metadata.intersection(vals) and any(
                    attachment.res_model in WOOW_MODELS for attachment in self):
                raise AccessError(_('Document attachment access cannot be changed directly.'))
            if vals.get('res_model') in WOOW_MODELS:
                # Decide on the values the rows will have after this write: an
                # attachment that already backs a binary field, or is public or
                # tokenised, must not be moved onto a document or folder in a
                # second step either.
                self.flush_recordset(['res_field', 'public', 'access_token'])
                self.env.cr.execute(  # sql-ok: Q6
                    'SELECT res_field IS NOT NULL, COALESCE(public, FALSE), '
                    'access_token IS NOT NULL FROM ir_attachment WHERE id = ANY(%s)',
                    (self.ids,))
                for has_field, is_public, has_token in self.env.cr.fetchall():
                    if (vals['res_field'] if 'res_field' in vals else has_field) or (
                            vals['public'] if 'public' in vals else is_public) or (
                            vals['access_token'] if 'access_token' in vals else has_token):
                        raise AccessError(_('Document field attachments cannot be reassigned.'))
        if self and {'res_model', 'res_id', 'res_field'}.intersection(vals) and (
                'res_model' not in vals or vals['res_model'] in WOOW_MODELS):
            self._woow_check_field_target(vals)
        if self.env.su and self and (vals.get('res_model') in WOOW_MODELS
                                     or vals.get('public') or vals.get('access_token')):
            return self._woow_write_closed(vals)
        return super().write(vals)

    def _woow_write_closed(self, vals):
        """A superuser's write that could open a row, with the rows of module records kept closed.

        ``write`` calls this only when the values name a module model for ``res_model`` or set the
        public flag or a token; any other write is core's and no row is read for it. The rows that
        are, or become, rows of a module model get the flag and the token set to ``False`` in the
        same write, so what a poster put on a pending file does not survive core's move of the
        file onto the record; the other rows of a mixed set are written as they were asked.
        Nothing is raised. A write that names the model reads no row; one that only sets the flag
        or a token reads ``res_model`` of the rows (one query for the set, none when they are in
        the cache already), and a write that sets neither does not come here.
        """
        if vals.get('res_model') in WOOW_MODELS:
            return super(IrAttachment, self).write(dict(vals, **dict.fromkeys(OPEN_KEYS, False)))
        owned = self.browse() if 'res_model' in vals else self.filtered(
            lambda attachment: attachment.res_model in WOOW_MODELS)
        if not owned:
            return super(IrAttachment, self).write(vals)
        done = super(IrAttachment, owned).write(
            dict(vals, **{key: False for key in OPEN_KEYS if vals.get(key)}))
        if owned != self:
            done = super(IrAttachment, self - owned).write(vals) and done
        return done

    def generate_access_token(self):
        """The attachments of this module's records never get an access token.

        Core stores a token on each attachment it is asked about, as superuser, and returns the
        tokens in the order of ``self``. Its callers are the portal chatter (the files of the
        messages it returns), the mail queue (the files of a message that are too large to send:
        the mail then carries a link), the html editor and ``mail.message.create`` (the picture in
        the body of a message). This is the method itself, so none of them is left out, whoever
        calls it and in whatever environment. A row of a document or folder is given none, and its
        place in the list is ``False``, what a token field holds when it is empty; the other rows
        are core's. ``create`` and ``write`` keep the same rule for a token that anything else sets.
        """
        owned = self.filtered(lambda attachment: attachment.res_model in WOOW_MODELS)
        if not owned:
            return super().generate_access_token()
        others = self - owned
        tokens = dict(zip(others.ids, super(IrAttachment, others).generate_access_token()))
        return [tokens.get(attachment.id, False) for attachment in self]

    def validate_access(self, access_token):
        """A token never opens an attachment of this module's records, whoever stored it.

        Core lets a token alone through: it compares it with the stored one and answers with the
        attachment as superuser, with no look at the record rules, the membership, the trash or a
        legal hold. Every caller of this check is covered, ``/web/content`` and ``/web/image``
        through ``ir.binary`` among them. Core returns the record as superuser once a token has
        matched, and that record is the one read here, so no escalation of the module's own is
        needed. The refusal is core's own for a token that does not match, so the answer does not
        tell the two apart; a request without a token is not changed (``create`` and ``write`` keep
        the public flag, which that request would follow, off the rows of this module's records).
        """
        attachment = super().validate_access(access_token)
        if access_token and attachment.res_model in WOOW_MODELS:
            raise AccessError('Invalid access token')
        return attachment

    def _woow_check_field_target(self, vals):
        """No write, the superuser's included, makes an attachment back a field of a module record.

        Core moves a poster's pending attachments onto the record as superuser
        (``mail.thread._process_attachments_for_post``, reached from ``message_post``
        and from ``message_notify`` with any ``model`` and ``res_id``), so a pending
        attachment that names a field would otherwise become a second file of a
        document or folder. Binary fields create their attachments and never move
        them: after this write every row that backs a field of a module record must
        back the same field of the same record as before.
        """
        ids = [value for value in self.ids if type(value) is int]
        if not ids:
            return
        self.flush_recordset(['res_model', 'res_id', 'res_field'])
        self.env.cr.execute(  # sql-ok: Q6
            'SELECT res_model, res_id, res_field FROM ir_attachment WHERE id = ANY(%s)',
            (ids,))
        for res_model, res_id, res_field in self.env.cr.fetchall():
            before = (res_model or False, res_id or 0, res_field or False)
            after = (vals.get('res_model', res_model) or False, vals.get('res_id', res_id) or 0,
                     vals.get('res_field', res_field) or False)
            if after[0] in WOOW_MODELS and after[2] and after != before:
                raise AccessError(_('Document field attachments cannot be reassigned.'))

    @api.model
    def _woow_field_attachment_ids(self, attachment_ids):
        """Ids among ``attachment_ids`` that back a binary field of some record."""
        ids = [value for value in attachment_ids or () if type(value) is int]
        if not ids:
            return []
        self.flush_model(['res_field'])
        self.env.cr.execute(  # sql-ok: Q17
            'SELECT id FROM ir_attachment WHERE id = ANY(%s) AND res_field IS NOT NULL', (ids,))
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _woow_document_file_row(self, document_id):
        """The stored file a document records, read without record rules, or None.

        Only the document's own ``attachment_id`` qualifies, and only while it is a
        binary attachment without a URL that backs that document's ``datas``. Any
        other attachment that claims the same field is never returned. Returns
        (name, mimetype, checksum, store_fname, db_datas, write_date).
        """
        self.flush_model(['res_model', 'res_id', 'res_field', 'type', 'url', 'store_fname',
                          'db_datas', 'mimetype', 'checksum', 'name'])
        self.env['woow.document'].flush_model(['attachment_id'])
        self.env.cr.execute(  # sql-ok: Q17
            """SELECT a.name, a.mimetype, a.checksum, a.store_fname, a.db_datas, a.write_date
               FROM woow_document d JOIN ir_attachment a ON a.id = d.attachment_id
               WHERE d.id = %s AND a.res_model = 'woow.document' AND a.res_id = d.id
                 AND a.res_field = 'datas' AND a.type = 'binary'
                 AND COALESCE(a.url, '') = ''""",
            (document_id,))
        return self.env.cr.fetchone()
