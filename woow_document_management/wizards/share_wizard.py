# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Issue individual, expiring links from an explicit selection."""
import re
from datetime import timedelta
from email.utils import parseaddr

from odoo import _, Command, fields, models
from odoo.exceptions import UserError

from ..services.constants import MAX_RECIPIENTS, MAX_SHARE_DOCUMENTS


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
    password = fields.Char()   # transient; cleared after use, never logged
    note = fields.Text()

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
                        **values, 'password': self.password or None})
                    url = share._woow_link_url(token)
                    if recipient:
                        share._woow_send_link(token, self.note)
                    links.append(url)
            return {'type': 'ir.actions.client', 'tag': 'woow_document_share_result',
                    'params': {'links': links}}
        finally:
            self.write({'password': False})
