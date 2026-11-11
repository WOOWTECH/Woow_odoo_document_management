# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Narrow attachment guard for document fields and public-link metadata."""
from odoo import _, api, models
from odoo.exceptions import AccessError

from ..services.constants import WOOW_MODELS

GUARDED_WRITE_KEYS = ('raw', 'datas', 'db_datas', 'mimetype', 'type', 'url', 'res_model', 'res_id',
                      'res_field', 'public', 'access_token', 'store_fname')


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model_create_multi
    def create(self, vals_list):
        # Core checks only explicit res_model/res_id before ORM defaults are applied.
        # Resolve the same defaults now, so contextual values cannot bypass either
        # the document guard or core's write check on the target record.
        keys = ('res_model', 'res_id', 'res_field', 'public', 'access_token')
        defaults = self.default_get(list(keys))
        checked = []
        for incoming in vals_list:
            effective = {key: incoming.get(key, defaults.get(key)) for key in keys}
            if not self.env.su and effective['res_model'] in WOOW_MODELS and (
                    effective['res_field'] or effective['public'] or effective['access_token']):
                raise AccessError(_('Document attachments must be created through the document.'))
            vals = dict(incoming)
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
        return super().write(vals)

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
    def _woow_document_file_is_unique(self, document_id):
        """True when exactly one binary attachment backs the document's file field.

        It must also be the attachment the document records, so a stream never
        serves another attachment that claims the same field.
        """
        self.flush_model(['res_model', 'res_id', 'res_field', 'type'])
        self.env['woow.document'].flush_model(['attachment_id'])
        self.env.cr.execute(  # sql-ok: Q17
            """SELECT count(*), COALESCE(bool_and(a.type = 'binary' AND a.id = d.attachment_id), FALSE)
               FROM ir_attachment a JOIN woow_document d ON d.id = a.res_id
               WHERE a.res_model = 'woow.document' AND a.res_field = 'datas' AND a.res_id = %s""",
            (document_id,))
        count, consistent = self.env.cr.fetchone()
        return count == 1 and bool(consistent)
