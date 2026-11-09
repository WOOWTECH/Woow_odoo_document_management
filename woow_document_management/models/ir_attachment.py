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
        return super().write(vals)
