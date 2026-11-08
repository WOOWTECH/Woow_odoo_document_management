# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Folder members (IF-MODEL, owned by core-models-acl). Day-0 stub: fields only."""
from odoo import fields, models


class WoowDocumentFolderAccess(models.Model):
    _name = 'woow.document.folder.access'
    _inherit = ['woow.document.guard.mixin']
    _description = 'Document folder member'
    _woow_allowed_defaults = frozenset({'default_folder_id'})

    folder_id = fields.Many2one('woow.document.folder', required=True, index=True,
                                ondelete='cascade')
    company_id = fields.Many2one(related='folder_id.company_id', store=True, index=True)
    subject_type = fields.Selection(
        [('user', 'User'), ('group', 'Group'), ('partner', 'Portal contact')], required=True)
    user_id = fields.Many2one('res.users', ondelete='cascade')
    group_id = fields.Many2one('res.groups', ondelete='cascade')
    partner_id = fields.Many2one('res.partner', ondelete='cascade')
    level = fields.Selection(
        [('read', 'View'), ('upload', 'Upload'), ('edit', 'Edit'), ('manage', 'Manage')],
        required=True)
