# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Share link schema and immutable scope/secret guards."""
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

    def init(self):
        super().init()
        self.env.cr.execute(  # sql-ok: Q3
            'CREATE UNIQUE INDEX IF NOT EXISTS woow_document_share_token_hash_uniq '
            'ON woow_document_share (token_hash)')

    @api.depends('password_hash')
    def _compute_has_password(self):
        for share in self:
            share.has_password = bool(share.password_hash)

    @api.model_create_multi
    def create(self, vals_list):
        """Secret fields only with the sentinel context; _check_create_rights per record."""
        trusted = self.env.context.get(SECRET_CTX_KEY) is _SECRET_SENTINEL
        for vals in vals_list:
            if not self.env.su and not trusted and {'token_hash', 'password_hash'} & vals.keys():
                raise AccessError(_('Share secrets can only be set by the share wizard.'))
            self._check_create_rights(vals)
        return super().create(vals_list)

    def write(self, vals):
        """Non-superusers may only rename or shorten expire_at."""
        if not self.env.su:
            if set(vals) - {'name', 'expire_at'}:
                raise AccessError(_('This share field cannot be changed.'))
            if 'expire_at' in vals:
                new_expiry = fields.Datetime.to_datetime(vals['expire_at'])
                if not new_expiry or any(
                        new_expiry > share.expire_at for share in self):
                    raise AccessError(_('A share expiry can only be shortened.'))
        return super().write(vals)

    def _check_create_rights(self, vals):
        """Schema-level default: always refuse (the share lane overrides it)."""
        raise AccessError(_('You cannot create share links.'))

    def _woow_mark_revoked(self):
        """Mark the link revoked (caller already ran check_access('write'))."""
        for share in self:
            if not share.revoked_at:
                super(WoowDocumentShare, share).write({
                    'revoked_at': fields.Datetime.now(),
                    'revoked_by_id': self.env.uid, 'state': 'revoked',
                })
