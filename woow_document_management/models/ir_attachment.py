# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""ir.attachment create/write guard (IF-GUARD, owned by core-models-acl). Q6 lives here.

Only create and write are overridden (never unlink, check or _search; I-03).
Day-0 stub, fail closed for non-superusers: creating attachments on module models, and any
write touching a guarded key, are refused.
"""
from odoo import _, api, models
from odoo.exceptions import AccessError

from ..services.constants import WOOW_MODELS

GUARDED_WRITE_KEYS = ('raw', 'datas', 'db_datas', 'mimetype', 'type', 'url', 'res_model', 'res_id',
                      'res_field', 'public', 'access_token', 'store_fname')


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(vals.get('res_model') in WOOW_MODELS for vals in vals_list):
            raise AccessError(_('This action is not available yet.'))  # WOOW-STUB
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and any(key in vals for key in GUARDED_WRITE_KEYS):
            raise AccessError(_('This action is not available yet.'))  # WOOW-STUB
        return super().write(vals)
