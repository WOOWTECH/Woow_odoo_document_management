# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Mail current folder members a login-only link."""
from ipaddress import ip_address
from urllib.parse import urlsplit

from odoo import _, Command, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.constants import MAIL_PER_USER_PER_HOUR


class WoowDocumentNotifyWizard(models.TransientModel):
    _name = 'woow.document.notify.wizard'
    _description = 'Notify folder members'

    folder_id = fields.Many2one('woow.document.folder', required=True, readonly=True)
    all_members = fields.Boolean(default=True)
    recipient_ids = fields.Many2many('res.partner', string='Selected members')
    note = fields.Text()

    def _member_recipients(self):
        folder = self.folder_id
        internal_users = (folder.eff_read_user_ids | folder.eff_read_group_ids.mapped('users') |
                          folder.owner_id).filtered(
            lambda user: user.active and user._is_internal() and
            user.has_group('woow_document_management.group_woow_document_user'))
        internal = self.env['res.partner'].browse()
        for user in internal_users:
            if folder.company_id in user.company_ids:
                internal |= user.partner_id
        portal = self.env['res.partner'].browse()
        for partner in folder.eff_read_partner_ids.filtered('active'):
            portal_users = partner.user_ids.filtered(
                lambda user: user.active and user.has_group('base.group_portal') and
                folder.company_id in user.company_ids)
            if portal_users:
                portal |= partner
        return internal, portal - internal

    def _base_url(self):
        settings = self.env['woow.document.settings']._get_settings()
        base = (settings.notice_base_url or self.folder_id.get_base_url()).rstrip('/')
        parsed = urlsplit(base)
        try:
            literal_ip = ip_address(parsed.hostname or '')
        except ValueError:
            literal_ip = None
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or literal_ip \
                or parsed.username or parsed.password or parsed.path not in ('', '/') \
                or parsed.query or parsed.fragment or (parsed.scheme == 'http' and
                                                      not settings.allow_http_links):
            raise UserError(_('Configure a public HTTPS address for document notices.'))
        return base

    def action_send(self):
        """Queue one mail per currently authorized member, with an authenticated URL."""
        self.check_access('write')
        self.ensure_one()
        self.folder_id._woow_require_rank(4)
        if self.folder_id.in_trash:
            raise AccessError(_('Folder unavailable.'))
        internal, portal = self._member_recipients()
        allowed = internal | portal
        if self.all_members:
            recipients = allowed
        else:
            ids = self.recipient_ids.ids
            visible = self.env['res.partner'].search([('id', 'in', ids)])
            if not ids or len(visible) != len(ids) or any(
                    partner not in allowed for partner in visible):
                raise AccessError(_('A selected member is no longer authorized.'))
            recipients = visible
        if not recipients or len(recipients) > MAIL_PER_USER_PER_HOUR:
            raise UserError(_('Select between 1 and 100 current members.'))
        if any(not partner.email for partner in recipients):
            raise UserError(_('Every selected member needs an email address.'))
        sender = self.env.user.email_formatted or self.folder_id.company_id.email
        if not sender:
            raise UserError(_('Set a sender email address before notifying members.'))
        base = self._base_url()
        template = self.env.ref('woow_document_management.mail_template_woow_document_member_notice')
        for partner in recipients:
            path = ('/my/docs/folder/%d' if partner in portal else
                    '/web#id=%d&model=woow.document.folder&view_type=form') % self.folder_id.id
            template.with_context(woow_notice_url=base + path,
                                  woow_notice_note=self.note or '').send_mail(
                self.folder_id.id, force_send=False,
                email_values={'recipient_ids': [Command.set([partner.id])],
                              'email_to': False, 'email_from': sender,
                              'auto_delete': True})
        return {'type': 'ir.actions.act_window_close'}
