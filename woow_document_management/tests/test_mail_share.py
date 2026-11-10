# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Share wizard and queued mail behavior."""
import secrets
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools.sql import column_exists

from ..services import tokens


@tagged('woow_docmgmt_share_mail')
class TestShareWizardPassword(TransactionCase):
    """df38931ba9cd: the share password is hashed when the wizard row is saved."""

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.share_link_base_url', 'https://docs.example.test')
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Wizard password folder', 'owner_id': self.admin.id})
        self.wizards = self.env['woow.document.share.wizard'].with_user(self.admin)

    def _row_values(self, wizard):
        """Every column of the wizard row as stored in the database."""
        self.env.flush_all()
        self.env.cr.execute('SELECT * FROM woow_document_share_wizard WHERE id = %s',
                            (wizard.id,))
        row = self.env.cr.fetchone()
        return {column.name: row[index] for index, column in enumerate(self.env.cr.description)}

    def test_failed_action_leaves_no_plaintext_and_resave_keeps_the_password(self):
        password = secrets.token_urlsafe(16)
        wizard = self.wizards.create({
            'share_type': 'folder', 'folder_id': self.folder.id,
            'password': password,
            'expire_at': fields.Datetime.now() + timedelta(days=400)})
        self.env.invalidate_all()
        with self.assertRaises(UserError):
            wizard.action_create_links()
        stored = self._row_values(wizard)
        self.assertNotIn('password', stored)
        self.assertFalse(column_exists(self.env.cr, 'woow_document_share_wizard', 'password'))
        self.assertFalse(any(password in str(value) for value in stored.values()))
        self.assertTrue(tokens.check_password(password, stored['password_hash']))
        self.assertFalse(wizard.password)
        # The client saves again with only the corrected field: the password survives.
        wizard.write({'expire_at': fields.Datetime.now() + timedelta(days=1)})
        self.env.invalidate_all()
        result = wizard.action_create_links()
        self.assertEqual(result['tag'], 'woow_document_share_result')
        share = self.env['woow.document.share'].sudo().search([
            ('folder_id', '=', self.folder.id)])
        self.assertEqual(len(share), 1)
        self.assertTrue(share.has_password)
        self.assertTrue(tokens.check_password(password, share.password_hash))
        self.assertFalse(wizard.password)
        self.assertFalse(wizard.password_hash)
        self.assertFalse(self._row_values(wizard)['password_hash'])

    def test_password_length_is_checked_when_the_row_is_saved(self):
        short = secrets.token_urlsafe(2)[:3]
        with self.assertRaises(UserError):
            self.wizards.create({
                'share_type': 'folder', 'folder_id': self.folder.id, 'password': short,
                'expire_at': fields.Datetime.now() + timedelta(days=1)})
        wizard = self.wizards.create({
            'share_type': 'folder', 'folder_id': self.folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        with self.assertRaises(UserError):
            wizard.write({'password': secrets.token_urlsafe(100)[:129]})
        wizard.write({'password': secrets.token_urlsafe(16)})
        self.assertTrue(wizard.password_hash)
        wizard.write({'password': False})
        self.assertFalse(wizard.password_hash)

    def test_hash_cannot_be_injected_and_empty_password_follows_the_policy(self):
        forged = tokens.hash_password(secrets.token_urlsafe(16))
        with self.assertRaises(AccessError):
            self.wizards.create({
                'share_type': 'folder', 'folder_id': self.folder.id,
                'password_hash': forged,
                'expire_at': fields.Datetime.now() + timedelta(days=1)})
        wizard = self.wizards.create({
            'share_type': 'folder', 'folder_id': self.folder.id, 'password': '',
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        with self.assertRaises(AccessError):
            wizard.write({'password_hash': forged})
        self.assertFalse(wizard.password_hash)
        self.assertEqual(wizard.action_create_links()['tag'], 'woow_document_share_result')
        share = self.env['woow.document.share'].sudo().search([
            ('folder_id', '=', self.folder.id)])
        self.assertFalse(share.has_password)
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.share_require_password', 'True')
        required = self.wizards.create({
            'share_type': 'folder', 'folder_id': self.folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        with self.assertRaises(UserError):
            required.action_create_links()
        self.assertEqual(self.env['woow.document.share'].sudo().search_count([
            ('folder_id', '=', self.folder.id)]), 1)

    def test_init_drops_the_legacy_plaintext_column(self):
        self.env.cr.execute(
            'ALTER TABLE woow_document_share_wizard ADD COLUMN IF NOT EXISTS password varchar')
        self.assertTrue(column_exists(self.env.cr, 'woow_document_share_wizard', 'password'))
        self.env['woow.document.share.wizard'].init()
        self.assertFalse(column_exists(self.env.cr, 'woow_document_share_wizard', 'password'))


@tagged('woow_docmgmt_share_mail')
class TestShareMail(TransactionCase):

    def test_member_can_send_and_hourly_limit_leaves_no_mail(self):
        group = self.env.ref('woow_document_management.group_woow_document_user')
        member = self.env['res.users'].sudo().create({
            'name': 'Mail quota member', 'login': 'wdoc-mail-quota-member',
            'email': 'sender@example.test',
            'groups_id': [Command.set([group.id])],
        })
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Mail quota folder', 'owner_id': member.id,
        })
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user',
            'user_id': member.id, 'level': 'manage',
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.share_link_base_url', 'https://docs.example.test')

        def send_one(recipient):
            wizard = self.env['woow.document.share.wizard'].with_user(member).create({
                'share_type': 'folder', 'folder_id': folder.id,
                'recipient_emails': recipient,
                'expire_at': fields.Datetime.now() + timedelta(days=1),
            })
            return wizard.action_create_links()

        self.assertEqual(send_one('first@example.test')['tag'],
                         'woow_document_share_result')
        self.assertEqual(self.env['woow.document.log'].sudo()._sum_count([
            ('event', '=', 'share_send'), ('user_id', '=', member.id)]), 1)

        # Existing successful sends are counted for the same user. A rejected
        # request must roll back its share and queued mail in the wizard savepoint.
        self.env['woow.document.log'].sudo().create({
            'event': 'share_send', 'result': 'ok', 'channel': 'backend',
            'user_id': member.id, 'count': 99,
        })
        before_shares = self.env['woow.document.share'].sudo().search_count([
            ('folder_id', '=', folder.id)])
        before_mails = self.env['mail.mail'].sudo().search_count([
            ('model', '=', 'woow.document.share')])
        with self.assertRaises(UserError):
            send_one('blocked@example.test')
        self.assertEqual(self.env['woow.document.share'].sudo().search_count([
            ('folder_id', '=', folder.id)]), before_shares)
        self.assertEqual(self.env['mail.mail'].sudo().search_count([
            ('model', '=', 'woow.document.share')]), before_mails)

        outsider = self.env['res.users'].sudo().create({
            'name': 'Mail quota outsider', 'login': 'wdoc-mail-quota-outsider',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            self.env['woow.document.share'].with_user(outsider)._issue({
                'share_type': 'folder', 'folder_id': folder.id,
                'expire_at': fields.Datetime.now() + timedelta(days=1),
            })

    def test_failed_mail_and_parent_message_are_removed_after_24h(self):
        admin = self.env.ref('base.user_admin')
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.share_link_base_url', 'https://docs.example.test')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Failed mail fixture', 'owner_id': admin.id})
        wizard = self.env['woow.document.share.wizard'].with_user(admin).create({
            'share_type': 'folder', 'folder_id': folder.id,
            'recipient_emails': 'reader@example.test',
            'expire_at': fields.Datetime.now() + timedelta(days=3),
        })
        wizard.action_create_links()
        share = self.env['woow.document.share'].sudo().search([
            ('folder_id', '=', folder.id)], limit=1)
        mail = self.env['mail.mail'].sudo().search([
            ('model', '=', 'woow.document.share'), ('res_id', '=', share.id)], limit=1)
        message_id = mail.mail_message_id.id
        self.assertTrue(mail.auto_delete)
        self.assertFalse(mail.is_notification)
        mail.write({'state': 'exception'})
        self.env.cr.execute('UPDATE mail_mail SET create_date = %s WHERE id = %s',
                            (fields.Datetime.now() - timedelta(hours=25), mail.id))
        self.env.invalidate_all()
        self.env['woow.document.share'].sudo()._maintenance_run()
        self.assertFalse(mail.exists())
        self.assertFalse(self.env['mail.message'].sudo().browse(message_id).exists())
        notices = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'res.partner'), ('res_id', '=', admin.partner_id.id),
            ('user_id', '=', admin.id), ('summary', '=',
             'A document share email could not be delivered')])
        self.assertEqual(len(notices), 1)
        self.assertNotIn('docs.example.test', notices.note)


    def test_wizard_queues_individual_links_and_clears_password(self):
        admin = self.env.ref('base.user_admin')
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.share_link_base_url', 'https://docs.example.test')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Test folder', 'owner_id': admin.id})
        wizard = self.env['woow.document.share.wizard'].with_user(admin).create({
            'share_type': 'folder', 'folder_id': folder.id,
            'recipient_emails': 'reader1@example.test,reader2@example.test',
            'password': secrets.token_urlsafe(16),
            'expire_at': fields.Datetime.now() + timedelta(days=1),
        })
        result = wizard.action_create_links()
        self.assertEqual(result['tag'], 'woow_document_share_result')
        self.assertEqual(len(result['params']['links']), 2)
        self.assertFalse(wizard.password)
        shares = self.env['woow.document.share'].sudo().search([
            ('folder_id', '=', folder.id)])
        self.assertEqual(len(shares), 2)
        self.assertTrue(all(shares.mapped('has_password')))
        mails = self.env['mail.mail'].sudo().search([
            ('model', '=', 'woow.document.share'), ('res_id', 'in', shares.ids)])
        self.assertEqual(len(mails), 2)
        self.assertEqual(set(mails.mapped('email_to')),
                         {'reader1@example.test', 'reader2@example.test'})
        self.assertEqual(set(mails.mapped('state')), {'outgoing'})

    def test_reissue_never_removes_password(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Protected folder', 'owner_id': admin.id})
        share, _token = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
            'password': secrets.token_urlsafe(16)})
        with self.assertRaises(UserError):
            share.action_reissue()
        self.assertFalse(share.revoked_at)

    def test_maintenance_marks_expired_and_records_event(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Expiring folder', 'owner_id': admin.id})
        share, _token = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        share.sudo().write({'expire_at': fields.Datetime.now() - timedelta(minutes=1)})
        self.env['woow.document.share'].sudo()._maintenance_run()
        self.assertEqual(share.state, 'expired')
        event = self.env['woow.document.log'].sudo().search([
            ('share_id', '=', share.id), ('event', '=', 'share_expire')])
        self.assertEqual(len(event), 1)
