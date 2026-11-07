# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Share link schema (IF-SHARE-SCHEMA, owned by core-models-acl).

Day-0 stub, fail closed: non-superuser create/write are refused.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError

_SECRET_SENTINEL = object()
SECRET_CTX_KEY = 'woow_doc_share_secret'


class WoowDocumentShare(models.Model):
    _name = 'woow.document.share'
    _inherit = ['woow.document.guard.mixin']      # no mail.thread
    _description = 'Document share link'
    _order = 'id desc'

    name = fields.Char()
    company_id = fields.Many2one('res.company', required=True, index=True, ondelete='restrict')
    share_type = fields.Selection([('folder', 'Folder'), ('documents', 'Documents')],
                                  required=True)
    folder_id = fields.Many2one('woow.document.folder', index=True, ondelete='cascade')
    include_subfolders = fields.Boolean(default=True)
    document_ids = fields.Many2many('woow.document', 'woow_document_share_document_rel',
                                    'share_id', 'document_id')
    token_hash = fields.Char(size=64, required=True, readonly=True, copy=False,
                             groups='base.group_system')
    token_hint = fields.Char(size=6)
    password_hash = fields.Char(copy=False, groups='base.group_system')
    has_password = fields.Boolean(compute='_compute_has_password', store=True)
    expire_at = fields.Datetime(required=True, index=True)
    revoked_at = fields.Datetime()
    revoked_by_id = fields.Many2one('res.users', ondelete='set null')
    state = fields.Selection([('active', 'Active'), ('expired', 'Expired'), ('revoked', 'Revoked')],
                             default='active', index=True)
    recipient_email = fields.Char()
    max_downloads = fields.Integer(default=0)
    sent_at = fields.Datetime()
    replaced_share_id = fields.Many2one('woow.document.share', ondelete='set null')

    @api.depends('password_hash')
    def _compute_has_password(self):
        """Day-0 stub: True, so links always ask for a password (fail closed)."""
        for share in self:  # WOOW-STUB
            share.has_password = True

    @api.model_create_multi
    def create(self, vals_list):
        """Secret fields only with the sentinel context; _check_create_rights per record."""
        if not self.env.su:
            raise AccessError(_('This action is not available yet.'))  # WOOW-STUB
        return super().create(vals_list)

    def write(self, vals):
        """Non-superusers may only rename or shorten expire_at."""
        if not self.env.su:
            raise AccessError(_('This action is not available yet.'))  # WOOW-STUB
        return super().write(vals)

    def _check_create_rights(self, vals):
        """Schema-level default: always refuse (the share lane overrides it)."""
        raise AccessError(_('You cannot create share links.'))

    def _woow_mark_revoked(self):
        """Mark the link revoked (caller already ran check_access('write'))."""
        raise NotImplementedError  # WOOW-STUB
