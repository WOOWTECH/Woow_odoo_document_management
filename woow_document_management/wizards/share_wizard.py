# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Issue individual, expiring links from an explicit selection."""
import re
from datetime import timedelta
from email.utils import parseaddr

from odoo import _, api, Command, fields, models
from odoo.exceptions import AccessError, UserError

from ..services import tokens
from ..services.constants import (MAX_RECIPIENTS, MAX_SHARE_DOCUMENTS, PASSWORD_MAX,
                                 PASSWORD_MIN)


class WoowDocumentShareWizard(models.TransientModel):
    _name = 'woow.document.share.wizard'
    _description = 'Create share links'

    share_type = fields.Selection([('folder', 'Folder'), ('documents', 'Documents')], required=True,
                                  default='documents')
    folder_id = fields.Many2one('woow.document.folder')
    document_ids = fields.Many2many('woow.document')
    include_subfolders = fields.Boolean(default=True)
    recipient_emails = fields.Text()
    expire_at = fields.Datetime()
    max_downloads = fields.Integer(default=0)
    # The form saves the row before the button runs (two requests), so a stored field would
    # commit the plaintext. create / write hash it into password_hash at once; password is
    # never stored and always reads back empty. Neither value is logged.
    password = fields.Char(compute='_compute_password', readonly=False)
    password_hash = fields.Char(readonly=True)   # written only through ``password``
    note = fields.Text()

    def _compute_password(self):
        for wizard in self:
            wizard.password = False

    def init(self):
        """Drop the plaintext column of the previous schema; wizard rows are transient."""
        super().init()
        self.env.cr.execute(  # sql-ok: Q3
            'ALTER TABLE woow_document_share_wizard DROP COLUMN IF EXISTS password')

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._woow_hash_password(vals) for vals in vals_list])

    def write(self, vals):
        return super().write(self._woow_hash_password(vals))

    def _woow_hash_password(self, vals):
        """Replace ``password`` by its hash; a ready-made hash is never accepted from callers."""
        if 'password_hash' in vals and not self.env.su:
            raise AccessError(_('The share password is set through the password field.'))
        if 'password' not in vals:
            return vals
        vals = dict(vals)
        password = vals.pop('password')
        if not password:
            vals['password_hash'] = False
            return vals
        try:
            vals['password_hash'] = tokens.hash_password(password)
        except ValueError:
            raise UserError(_('The share password must be between %(low)d and %(high)d characters.',
                              low=PASSWORD_MIN, high=PASSWORD_MAX)) from None
        return vals

    def action_create_links(self):
        """Create the share links and return them once to the creator."""
        self.check_access('write')
        self.ensure_one()
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        settings = self.env['woow.document.settings']._get_settings()
        expiry = fields.Datetime.to_datetime(self.expire_at) or now + timedelta(days=settings.share_default_days)
        if not now < expiry <= now + timedelta(days=settings.share_max_days):
            raise UserError(_('The share expiry is outside the allowed period.'))
        if self.max_downloads < 0:
            raise UserError(_('The download limit is invalid.'))
        if self.share_type == 'folder':
            if not self.folder_id or self.document_ids:
                raise UserError(_('Select one folder.'))
            scope = {'share_type': 'folder', 'folder_id': self.folder_id.id,
                     'include_subfolders': self.include_subfolders}
        else:
            ids = self.document_ids.ids
            if self.folder_id or not ids or len(ids) > MAX_SHARE_DOCUMENTS:
                raise UserError(_('Select between 1 and 500 documents.'))
            scope = {'share_type': 'documents', 'document_ids': [Command.set(ids)]}
        raw = re.split(r'[,;\n]', self.recipient_emails or '')
        recipients = [item.strip() for item in raw if item.strip()]
        if len(recipients) > MAX_RECIPIENTS or len(set(recipients)) != len(recipients):
            raise UserError(_('Select at most 20 distinct recipients.'))
        for email in recipients:
            name, parsed = parseaddr(email)
            if name or parsed != email or len(email) > 254 or not re.fullmatch(
                    r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email, flags=re.ASCII):
                raise UserError(_('Enter valid email addresses.'))
        links = []
        try:
            with self.env.cr.savepoint():
                for recipient in recipients or [None]:
                    values = dict(scope, expire_at=expiry, max_downloads=self.max_downloads,
                                  recipient_email=recipient or False)
                    share, token = self.env['woow.document.share']._issue({
                        **values, 'password_hash': self.password_hash or None})
                    url = share._woow_link_url(token)
                    if recipient:
                        share._woow_send_link(token, self.note)
                    links.append(url)
            return {'type': 'ir.actions.client', 'tag': 'woow_document_share_result',
                    'params': {'links': links}}
        finally:
            # Success clears the hash; a failed request rolls this back together with
            # the rest, so the form can be corrected and saved again.
            self.write({'password': False})
