# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Tags shared by folders and documents."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class WoowDocumentTag(models.Model):
    _name = 'woow.document.tag'
    _inherit = ['woow.document.guard.mixin']
    _description = 'Document tag'
    _order = 'name, id'

    name = fields.Char(required=True, translate=True)
    color = fields.Integer()
    company_id = fields.Many2one('res.company', index=True)
    active = fields.Boolean(default=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not self.env.user.has_group(
                'woow_document_management.group_woow_document_manager'):
            settings = self.env['woow.document.settings']._get_settings()
            if settings.tag_create_policy != 'users' or not self.env.user.has_group(
                    'woow_document_management.group_woow_document_user'):
                raise AccessError(_('Only document administrators may create tags.'))
        return super().create(vals_list)

    def init(self):
        super().init()
        self.env.cr.execute(  # sql-ok: Q3
            """CREATE UNIQUE INDEX IF NOT EXISTS woow_document_tag_name_company_uniq
               ON woow_document_tag (lower(name->>'en_US'), (COALESCE(company_id, 0)))
               WHERE active""")
