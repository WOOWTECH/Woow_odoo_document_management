# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Tags shared by folders and documents (IF-MODEL, owned by core-models-acl). Day-0 stub."""
from odoo import fields, models


class WoowDocumentTag(models.Model):
    _name = 'woow.document.tag'
    _inherit = ['woow.document.guard.mixin']
    _description = 'Document tag'
    _order = 'name, id'

    name = fields.Char(required=True, translate=True)
    color = fields.Integer()
    company_id = fields.Many2one('res.company', index=True)
    active = fields.Boolean(default=True)
