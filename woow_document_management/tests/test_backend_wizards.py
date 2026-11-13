# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Move wizard exercises current permissions and model guards."""
import base64

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('woow_docmgmt_backend')
class TestBackendWizards(TransactionCase):

    def test_member_notice_only_targets_current_members_without_token(self):
        admin = self.env.ref('base.user_admin')
        reader = new_test_user(self.env, login='notice_reader',
                               groups='woow_document_management.group_woow_document_user')
        stranger = self.env['res.partner'].create({
            'name': 'Outside contact', 'email': 'outside@example.test'})
        reader.partner_id.email = 'reader@example.test'
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.notice_base_url', 'https://docs.example.test')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Member notice', 'owner_id': admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user',
            'user_id': reader.id, 'level': 'read'})
        action = folder.with_user(admin).action_notify_members()
        self.assertEqual(action['res_model'], 'woow.document.notify.wizard')
        wizard_model = self.env['woow.document.notify.wizard'].with_user(admin)
        rejected = wizard_model.create({
            'folder_id': folder.id, 'all_members': False,
            'recipient_ids': [(6, 0, [stranger.id])]})
        with self.assertRaises(AccessError):
            rejected.action_send()
        self.assertFalse(self.env['mail.mail'].search([
            ('model', '=', 'woow.document.folder'), ('res_id', '=', folder.id)]))
        notice = wizard_model.create({
            'folder_id': folder.id, 'all_members': False,
            'recipient_ids': [(6, 0, [reader.partner_id.id])], 'note': 'Please review'})
        self.assertEqual(notice.action_send()['type'], 'ir.actions.act_window_close')
        mails = self.env['mail.mail'].search([
            ('model', '=', 'woow.document.folder'), ('res_id', '=', folder.id)])
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails.recipient_ids, reader.partner_id)
        self.assertIn('https://docs.example.test/web#id=', mails.body_html)
        self.assertNotIn('access_token', mails.body_html)
        with self.assertRaises(AccessError):
            folder.with_user(reader).action_notify_members()

        portal_user = new_test_user(self.env, login='notice_portal',
                                    groups='base.group_portal')
        portal_user.partner_id.email = 'portal@example.test'
        grant = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'partner',
            'partner_id': portal_user.partner_id.id, 'level': 'read'})
        portal_notice = wizard_model.create({
            'folder_id': folder.id, 'all_members': False,
            'recipient_ids': [(6, 0, [portal_user.partner_id.id])]})
        portal_notice.action_send()
        portal_mail = self.env['mail.mail'].search([
            ('model', '=', 'woow.document.folder'), ('res_id', '=', folder.id),
            ('recipient_ids', 'in', [portal_user.partner_id.id])], limit=1)
        self.assertIn('https://docs.example.test/my/docs/folder/', portal_mail.body_html)
        grant.sudo().unlink()
        revoked_notice = wizard_model.create({
            'folder_id': folder.id, 'all_members': False,
            'recipient_ids': [(6, 0, [portal_user.partner_id.id])]})
        with self.assertRaises(AccessError):
            revoked_notice.action_send()

    def test_a_notice_needs_a_public_https_address(self):
        admin = self.env.ref('base.user_admin')
        reader = new_test_user(self.env, login='notice_address_reader',
                               groups='woow_document_management.group_woow_document_user')
        reader.partner_id.email = 'address-reader@example.test'
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Notice address', 'owner_id': admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user',
            'user_id': reader.id, 'level': 'read'})
        wizard = self.env['woow.document.notify.wizard'].with_user(admin).create({
            'folder_id': folder.id, 'all_members': False,
            'recipient_ids': [(6, 0, [reader.partner_id.id])]})
        params = self.env['ir.config_parameter'].sudo()
        mails = self.env['mail.mail'].sudo()
        sent = [('model', '=', 'woow.document.folder'), ('res_id', '=', folder.id)]
        # no address of its own, and the address of the site is not https
        params.set_param('web.base.url', 'http://docs.example.test')
        with self.assertRaises(UserError):
            wizard.action_send()
        # an address of its own that is not https is not used either
        params.set_param('woow_document_management.notice_base_url', 'http://docs.example.test')
        with self.assertRaises(UserError):
            wizard.action_send()
        self.assertFalse(mails.search(sent))
        # the address that the setting names is the one every link starts with
        params.set_param('woow_document_management.notice_base_url', 'https://notice.example.test/')
        wizard.action_send()
        self.assertIn('https://notice.example.test/web#id=', mails.search(sent).body_html)

    def test_move_document_and_reject_trash_target(self):
        admin = self.env.ref('base.user_admin')
        folders = self.env['woow.document.folder'].sudo()
        source = folders.create({'name': 'Source', 'owner_id': admin.id})
        target = folders.create({'name': 'Target', 'owner_id': admin.id})
        document = self.env['woow.document'].sudo().create({
            'name': 'note.txt', 'folder_id': source.id,
            'datas': base64.b64encode(b'data')})
        wizard_model = self.env['woow.document.move.wizard'].with_user(admin)
        wizard = wizard_model.create({
            'document_ids': [(6, 0, [document.id])], 'target_folder_id': target.id})
        self.assertEqual(wizard.action_move()['type'], 'ir.actions.act_window_close')
        self.assertEqual(document.folder_id, target)
        source.with_user(admin).action_trash()
        rejected = wizard_model.create({
            'document_ids': [(6, 0, [document.id])], 'target_folder_id': source.id})
        with self.assertRaises(AccessError):
            rejected.action_move()
        self.assertEqual(document.folder_id, target)

    def test_folder_cannot_move_below_itself(self):
        admin = self.env.ref('base.user_admin')
        root = self.env['woow.document.folder'].sudo().create({
            'name': 'Root', 'owner_id': admin.id})
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Child', 'owner_id': admin.id, 'parent_id': root.id})
        wizard = self.env['woow.document.move.wizard'].with_user(admin).create({
            'folder_ids': [(6, 0, [root.id])], 'target_folder_id': child.id})
        with self.assertRaises(UserError):
            wizard.action_move()
        self.assertFalse(root.parent_id)

    def test_move_requires_target_grant(self):
        admin = self.env.ref('base.user_admin')
        user = new_test_user(self.env, login='move_restricted',
                             groups='woow_document_management.group_woow_document_user')
        folders = self.env['woow.document.folder'].sudo()
        source = folders.create({'name': 'Shared source', 'owner_id': admin.id})
        target = folders.create({'name': 'Private target', 'owner_id': admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': source.id, 'subject_type': 'user',
            'user_id': user.id, 'level': 'edit'})
        document = self.env['woow.document'].sudo().create({
            'name': 'move.txt', 'folder_id': source.id,
            'datas': base64.b64encode(b'data')})
        wizard = self.env['woow.document.move.wizard'].with_user(user).create({
            'document_ids': [(6, 0, [document.id])], 'target_folder_id': target.id})
        with self.assertRaises(AccessError):
            wizard.action_move()
        self.assertEqual(document.folder_id, source)
