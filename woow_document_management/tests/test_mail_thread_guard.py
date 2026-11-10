# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Portal users never read document or folder chatter through mail.message (DEC-11, ER-20)."""
import base64
import json
import secrets

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged

_MODELS = ('woow.document', 'woow.document.folder')


def _chatter_fixture(env, suffix, password=None):
    """A portal reader and an internal member who both may read one folder and its document.

    Returns (portal, member, folder, document, document_message, folder_message); the two
    messages are ordinary "Send message" comments (mt_comment) posted by the administrator.
    """
    admin = env.ref('base.user_admin')
    partner = env['res.partner'].sudo().create({
        'name': 'Chatter portal contact %s' % suffix,
        'email': 'chatter-portal-%s@example.test' % suffix})
    portal_vals = {
        'name': 'Chatter portal reader %s' % suffix, 'login': 'wdoc-chatter-portal-%s' % suffix,
        'partner_id': partner.id,
        'groups_id': [Command.set([env.ref('base.group_portal').id])]}
    if password:
        portal_vals['password'] = password
    portal = env['res.users'].sudo().create(portal_vals)
    member = env['res.users'].sudo().create({
        'name': 'Chatter member %s' % suffix, 'login': 'wdoc-chatter-member-%s' % suffix,
        'groups_id': [Command.set([
            env.ref('woow_document_management.group_woow_document_user').id])]})
    folder = env['woow.document.folder'].sudo().create({
        'name': 'Chatter folder %s' % suffix, 'owner_id': admin.id})
    env['woow.document.folder.access'].sudo().create({
        'folder_id': folder.id, 'subject_type': 'partner',
        'partner_id': partner.id, 'level': 'read'})
    env['woow.document.folder.access'].sudo().create({
        'folder_id': folder.id, 'subject_type': 'user',
        'user_id': member.id, 'level': 'read'})
    document = env['woow.document'].sudo().create({
        'name': 'chatter-%s.txt' % suffix, 'folder_id': folder.id,
        'datas': base64.b64encode(b'chatter fixture')})
    env.flush_all()
    document_message = document.with_user(admin).message_post(
        body='Internal note about the document', message_type='comment',
        subtype_xmlid='mail.mt_comment')
    folder_message = folder.with_user(admin).message_post(
        body='Internal note about the folder', message_type='comment',
        subtype_xmlid='mail.mt_comment')
    return portal, member, folder, document, document_message, folder_message


@tagged('woow_docmgmt_core')
class TestMailThreadGuardOrm(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.portal, cls.member, cls.folder, cls.document,
         cls.document_message, cls.folder_message) = _chatter_fixture(cls.env, 'orm')
        cls.message_ids = [cls.document_message.id, cls.folder_message.id]

    def _assert_hidden(self, user, message_ids):
        messages = self.env['mail.message'].with_user(user)
        domain = [('id', 'in', message_ids)]
        self.assertFalse(messages.search(domain))
        self.assertEqual(messages.search_count(domain), 0)
        self.assertEqual(messages.search_read(domain, ['body']), [])
        self.assertEqual(messages.search_read(
            [('model', 'in', _MODELS), ('id', 'in', message_ids)], ['body']), [])
        with self.assertRaises(AccessError):
            messages.browse(message_ids).read(['body'])
        with self.assertRaises(AccessError):
            messages.browse(message_ids).check_access('read')
        self.assertFalse(messages.browse(message_ids).has_access('read'))

    def test_portal_reader_cannot_read_document_or_folder_chatter(self):
        """A portal partner who may read the record cannot read its comments by any ORM path."""
        documents = self.env['woow.document'].with_user(self.portal)
        folders = self.env['woow.document.folder'].with_user(self.portal)
        self.assertEqual(documents.search_count([('id', '=', self.document.id)]), 1)
        self.assertEqual(folders.search_count([('id', '=', self.folder.id)]), 1)
        self.assertEqual(self.document_message.sudo().subtype_id,
                         self.env.ref('mail.mt_comment'))
        self.assertFalse(self.document_message.sudo().is_internal)
        self._assert_hidden(self.portal, self.message_ids)

    def test_portal_follower_is_still_mailed_but_cannot_read_the_message(self):
        """Being a notified follower (HD-10) does not open the ORM path either."""
        admin = self.env.ref('base.user_admin')
        partner = self.portal.partner_id
        self.document.with_user(admin).message_subscribe(partner_ids=[partner.id])
        self.assertIn(partner, self.document.sudo().message_partner_ids)
        message = self.document.with_user(admin).message_post(
            body='Follower comment', message_type='comment', subtype_xmlid='mail.mt_comment')
        notifications = self.env['mail.notification'].sudo().search([
            ('mail_message_id', '=', message.id), ('res_partner_id', '=', partner.id)])
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications.notification_type, 'email')
        self.assertIn(notifications.notification_status, ('ready', 'sent'))
        self._assert_hidden(self.portal, [message.id])
        self.assertEqual(self.env['mail.message'].with_user(self.portal).search_count(
            [('model', 'in', _MODELS)]), 0)

    def test_internal_member_still_reads_the_chatter(self):
        """Internal users keep the core behaviour: readable record means readable comments."""
        messages = self.env['mail.message'].with_user(self.member)
        domain = [('id', 'in', self.message_ids)]
        self.assertEqual(messages.search(domain).ids, sorted(self.message_ids, reverse=True))
        self.assertEqual(messages.search_count(domain), 2)
        bodies = {row['id']: row['body'] for row in messages.search_read(domain, ['body'])}
        self.assertIn('Internal note about the document', bodies[self.document_message.id])
        self.assertIn('Internal note about the folder', bodies[self.folder_message.id])
        messages.browse(self.message_ids).check_access('read')
        self.assertTrue(messages.browse(self.message_ids).has_access('read'))

    def test_portal_keeps_reading_messages_outside_module_models(self):
        """The rule only hides this module's models; the portal user's own thread stays readable."""
        partner = self.portal.partner_id
        own = partner.with_user(self.env.ref('base.user_admin')).message_post(
            body='Message on the partner record', message_type='comment',
            subtype_xmlid='mail.mt_comment', partner_ids=[partner.id])
        messages = self.env['mail.message'].with_user(self.portal)
        self.assertEqual(messages.search([('id', '=', own.id)]).id, own.id)
        self.assertEqual(messages.browse(own.id).read(['body'])[0]['id'], own.id)


@tagged('woow_docmgmt_core')
class TestMailThreadGuardRpc(HttpCase):

    def _call_kw(self, model, method, args, kwargs=None):
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 'guard',
                              'params': {'model': model, 'method': method,
                                         'args': args, 'kwargs': kwargs or {}}})
        response = self.url_open('/web/dataset/call_kw/%s/%s' % (model, method),
                                 data=payload, headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_portal_rpc_cannot_read_module_chatter_but_internal_can(self):
        password = secrets.token_urlsafe(16)
        portal, _member, folder, document, document_message, folder_message = _chatter_fixture(
            self.env, 'rpc', password=password)
        ids = [document_message.id, folder_message.id]
        self.authenticate(portal.login, password)
        found = self._call_kw('mail.message', 'search_read',
                              [[['id', 'in', ids]]], {'fields': ['body']})
        self.assertNotIn('error', found)
        self.assertEqual(found['result'], [])
        count = self._call_kw('mail.message', 'search_count',
                              [[['model', 'in', list(_MODELS)]]])
        self.assertEqual(count['result'], 0)
        read = self._call_kw('mail.message', 'read', [ids, ['body']])
        self.assertIn('error', read)
        self.assertEqual(read['error']['data']['name'], 'odoo.exceptions.AccessError')
        self.assertNotIn('Internal note', json.dumps(read))

        self.authenticate('admin', 'admin')
        found = self._call_kw('mail.message', 'search_read',
                              [[['id', 'in', ids]]], {'fields': ['body']})
        self.assertEqual(sorted(row['id'] for row in found['result']), sorted(ids))
