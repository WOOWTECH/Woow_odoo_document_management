# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Portal users never read document or folder chatter through mail.message (DEC-11, ER-20)."""
import base64
import json
import secrets

from odoo import Command
from odoo.addons.mail.tests.common import MockEmail
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


def _raw_email(sender, subject):
    """A plain RFC 2822 message as the mail gateway receives it."""
    return ('From: %s\r\nTo: docs@example.test\r\nSubject: %s\r\n'
            'Message-Id: <%s@example.test>\r\nContent-Type: text/plain; charset=utf-8\r\n'
            '\r\nInbound body\r\n' % (sender, subject, secrets.token_hex(12)))


def _document_user(env, login, **extra):
    return env['res.users'].sudo().create(dict({
        'name': login, 'login': login, 'email': '%s@example.test' % login,
        'groups_id': [Command.set([
            env.ref('woow_document_management.group_woow_document_user').id])]}, **extra))


@tagged('woow_docmgmt_core')
class TestMailGatewayGuard(TransactionCase):
    """Incoming email never creates or posts on documents and folders for normal users."""

    def setUp(self):
        super().setUp()
        self.plain = self.env['res.users'].sudo().create({
            'name': 'Gateway plain user', 'login': 'wdoc-gateway-plain',
            'email': 'gateway-plain@example.test',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])]})
        self.reader = _document_user(self.env, 'wdoc-gateway-reader')
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Gateway folder', 'owner_id': self.env.ref('base.user_admin').id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user',
            'user_id': self.reader.id, 'level': 'read'})
        self.document = self.env['woow.document'].sudo().create({
            'name': 'gateway.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'gateway fixture')})
        self.env.flush_all()

    def _counts(self):
        return (self.env['woow.document'].sudo().with_context(active_test=False).search_count([]),
                self.env['woow.document.folder'].sudo().with_context(
                    active_test=False).search_count([]),
                self.env['mail.message'].sudo().search_count([('model', 'in', list(_MODELS))]))

    def test_normal_users_cannot_create_records_through_the_gateway(self):
        before = self._counts()
        routes = (('woow.document', {'folder_id': self.folder.id, 'document_type': 'url',
                                     'url': 'https://example.invalid/landing'}),
                  ('woow.document.folder', {'parent_id': self.folder.id}),
                  ('woow.document.folder', {}))
        for entry in ('woow.document', 'woow.document.folder', 'res.partner', 'mail.thread'):
            for model, values in routes:
                with self.subTest(entry=entry, model=model, values=values), \
                        self.assertRaises(AccessError):
                    self.env[entry].with_user(self.plain).message_process(
                        model, _raw_email('gateway-plain@example.test', 'Inbound'),
                        custom_values=dict(values))
        self.assertEqual(self._counts(), before)
        for model in _MODELS:
            with self.assertRaises(AccessError):
                self.env[model].with_user(self.plain).message_new(
                    {'subject': 'Direct'}, {'parent_id': self.folder.id})

    def test_reader_cannot_post_on_a_record_through_the_gateway(self):
        before = self._counts()
        for thread in (self.document, self.folder):
            with self.subTest(model=thread._name), self.assertRaises(AccessError):
                self.env[thread._name].with_user(self.reader).message_process(
                    thread._name, _raw_email('admin@example.test', 'Spoofed'),
                    thread_id=thread.id)
        self.assertEqual(self._counts(), before)

    def test_superuser_gateway_delivers_replies_but_never_creates_records(self):
        gateway = self.env['mail.thread'].sudo()
        subject = 'Reply %s' % secrets.token_hex(4)
        self.assertEqual(gateway.message_process(
            'woow.document', _raw_email('outside@example.test', subject),
            thread_id=self.document.id), self.document.id)
        self.assertEqual(self.env['mail.message'].sudo().search_count([
            ('model', '=', 'woow.document'), ('res_id', '=', self.document.id),
            ('subject', '=', subject)]), 1)
        before = self._counts()
        for model in _MODELS:
            with self.subTest(model=model), self.assertRaises(AccessError):
                gateway.message_process(model, _raw_email('outside@example.test', 'New'))
            with self.assertRaises(AccessError):
                self.env[model].sudo().message_new({'subject': 'Direct'})
        self.assertEqual(self._counts(), before)

    def test_system_administrator_gateway_delivers_replies(self):
        """The mail gateway script logs in as a system administrator, not as superuser."""
        admin = self.env.ref('base.user_admin')
        self.assertTrue(admin._is_system())
        gateway = self.env['mail.thread'].with_user(admin)
        self.assertFalse(gateway.env.su)
        subject = 'Gateway reply %s' % secrets.token_hex(4)
        self.assertEqual(gateway.message_process(
            'woow.document.folder',
            _raw_email('outside@example.test', subject), thread_id=self.folder.id), self.folder.id)
        self.assertEqual(self.env['mail.message'].sudo().search_count([
            ('model', '=', 'woow.document.folder'), ('res_id', '=', self.folder.id),
            ('subject', '=', subject)]), 1)
        with self.assertRaises(AccessError):
            gateway.message_process('woow.document.folder',
                                    _raw_email('outside@example.test', 'New folder'))


@tagged('woow_docmgmt_core')
class TestFollowerRemovalGuard(TransactionCase):
    """Only internal users may remove followers other than themselves."""

    def setUp(self):
        super().setUp()
        admin = self.env.ref('base.user_admin')
        self.partner = self.env['res.partner'].sudo().create({
            'name': 'Uploader contact', 'email': 'uploader@example.test'})
        self.portal = self.env['res.users'].sudo().create({
            'name': 'Uploader', 'login': 'wdoc-unfollow-portal', 'partner_id': self.partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        self.member = _document_user(self.env, 'wdoc-unfollow-member')
        self.editor = _document_user(self.env, 'wdoc-unfollow-editor')
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Unfollow folder', 'owner_id': admin.id})
        access = self.env['woow.document.folder.access'].sudo()
        access.create({'folder_id': self.folder.id, 'subject_type': 'partner',
                       'partner_id': self.partner.id, 'level': 'upload'})
        access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                       'user_id': self.member.id, 'level': 'read'})
        access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                       'user_id': self.editor.id, 'level': 'edit'})
        self.document = self.env['woow.document'].sudo().create({
            'name': 'unfollow.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'unfollow fixture')})
        self.followers = (self.member.partner_id | admin.partner_id | self.portal.partner_id)
        for record in (self.folder, self.document):
            record.sudo().message_subscribe(partner_ids=self.followers.ids)
        self.env.flush_all()

    def _followers(self, record):
        return record.sudo().message_partner_ids

    def test_portal_uploader_cannot_remove_other_followers(self):
        self.assertTrue(self.folder.with_user(self.portal)._woow_portal_can_upload())
        for record in (self.folder, self.document):
            for partner_ids in ([self.member.partner_id.id],
                                list(range(1, max(self.followers.ids) + 5))):
                with self.subTest(model=record._name, ids=len(partner_ids)), \
                        self.assertRaises(AccessError):
                    record.with_user(self.portal).message_unsubscribe(partner_ids)
            self.assertEqual(self._followers(record), self.followers)

    def test_self_removal_internal_editors_and_superuser_keep_working(self):
        self.folder.with_user(self.portal).message_unsubscribe([self.portal.partner_id.id])
        self.assertNotIn(self.portal.partner_id, self._followers(self.folder))
        self.folder.with_user(self.editor).message_unsubscribe([self.member.partner_id.id])
        self.assertNotIn(self.member.partner_id, self._followers(self.folder))
        self.folder.sudo().message_unsubscribe(partner_ids=self.followers.ids)
        self.assertFalse(self._followers(self.folder))


@tagged('woow_docmgmt_core')
class TestFolderNamesInMail(TransactionCase, MockEmail):
    """Mail and activities on a folder never name an ancestor the reader cannot see (I-08)."""

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.admin.sudo().write({'email': 'admin@example.test'})
        self.reader = _document_user(self.env, 'wdoc-names-reader')
        self.ancestor = self.env['woow.document.folder'].sudo().create({
            'name': 'VRF Hidden Ancestor', 'owner_id': self.admin.id})
        self.child = self.env['woow.document.folder'].sudo().create({
            'name': 'VRF Visible Child', 'parent_id': self.ancestor.id,
            'inherit_access': False, 'owner_id': self.admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.child.id, 'subject_type': 'user',
            'user_id': self.reader.id, 'level': 'read'})
        self.child.sudo().message_subscribe(partner_ids=[self.reader.partner_id.id])
        self.env.flush_all()
        folders = self.env['woow.document.folder'].with_user(self.reader)
        self.assertEqual(folders.search_count([('id', '=', self.ancestor.id)]), 0)
        self.assertNotIn('VRF Hidden Ancestor', self.child.with_user(self.reader).display_name)
        self.assertIn('VRF Hidden Ancestor', self.child.with_user(self.admin).display_name)

    def test_posted_message_and_its_mail_name_only_the_folder(self):
        with self.mock_mail_gateway():
            message = self.child.with_user(self.admin).message_post(
                body='Status update', message_type='comment',
                subtype_xmlid='mail.mt_comment', partner_ids=[self.reader.partner_id.id])
        self.env.flush_all()
        message = message.sudo()
        self.assertEqual(message.record_name, 'VRF Visible Child')
        self.assertNotIn('VRF Hidden Ancestor', message.subject or '')
        self.assertTrue(self._new_mails)
        for mail in self._new_mails:
            self.assertNotIn('VRF Hidden Ancestor', mail.subject or '')
            self.assertNotIn('VRF Hidden Ancestor', mail.body_html or '')
        self.assertTrue(self._mails)
        for sent in self._mails:
            self.assertNotIn('VRF Hidden Ancestor', sent['subject'] or '')
            self.assertNotIn('VRF Hidden Ancestor', sent['body'] or '')
        read = self.env['mail.message'].with_user(self.reader).search_read(
            [('id', '=', message.id)], ['record_name', 'subject'])
        self.assertEqual(read[0]['record_name'], 'VRF Visible Child')
        self.assertNotIn('VRF Hidden Ancestor', read[0]['subject'] or '')

    def test_activity_names_only_the_folder(self):
        with self.mock_mail_gateway():
            activity = self.child.with_user(self.admin).activity_schedule(
                'mail.mail_activity_data_todo', user_id=self.reader.id, summary='Check')
        self.env.flush_all()
        self.assertEqual(activity.sudo().res_name, 'VRF Visible Child')
        self.assertEqual(self.env['mail.activity'].with_user(self.reader).search_read(
            [('id', '=', activity.id)], ['res_name'])[0]['res_name'], 'VRF Visible Child')
        for sent in self._mails:
            self.assertNotIn('VRF Hidden Ancestor', sent['subject'] or '')
            self.assertNotIn('VRF Hidden Ancestor', sent['body'] or '')


@tagged('woow_docmgmt_core')
class TestRevokedFollowersAreNotNotified(TransactionCase, MockEmail):
    """Following a record never outlasts the right to read it."""

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.admin.sudo().write({'email': 'admin@example.test'})
        self.member = _document_user(self.env, 'wdoc-follow-member')
        self.manager = _document_user(self.env, 'wdoc-follow-manager', groups_id=[Command.set([
            self.env.ref('woow_document_management.group_woow_document_manager').id])])
        self.portal_partner = self.env['res.partner'].sudo().create({
            'name': 'Follow portal', 'email': 'follow-portal@example.test'})
        self.portal = self.env['res.users'].sudo().create({
            'name': 'Follow portal', 'login': 'wdoc-follow-portal',
            'partner_id': self.portal_partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        self.contact = self.env['res.partner'].sudo().create({
            'name': 'Outside contact', 'email': 'outside-contact@example.test'})
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Follow folder', 'owner_id': self.admin.id})
        self.other = self.env['woow.document.folder'].sudo().create({
            'name': 'Other folder', 'owner_id': self.admin.id})
        access = self.env['woow.document.folder.access'].sudo()
        self.lines = access.create([
            {'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.member.id,
             'level': 'read'},
            {'folder_id': self.folder.id, 'subject_type': 'partner',
             'partner_id': self.portal_partner.id, 'level': 'read'}])
        self.document = self.env['woow.document'].sudo().create({
            'name': 'follow.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'follow fixture')})
        self.followers = (self.member.partner_id | self.manager.partner_id |
                          self.portal_partner | self.contact)
        for record in (self.folder, self.document):
            record.sudo().message_subscribe(partner_ids=self.followers.ids)
        self.env.flush_all()

    def _notified(self, record, **post):
        with self.mock_mail_gateway():
            message = record.with_user(self.admin).message_post(
                body='Follow update', message_type='comment',
                subtype_xmlid='mail.mt_comment', **post)
        self.env.flush_all()
        partners = message.sudo().notification_ids.res_partner_id
        mailed = {address for mail in self._mails for address in mail['email_to']}
        return partners, mailed

    def test_members_managers_and_contacts_are_notified(self):
        for record in (self.folder, self.document):
            with self.subTest(model=record._name):
                partners, mailed = self._notified(record)
                self.assertEqual(partners, self.followers)
                self.assertTrue(any('follow-portal@example.test' in address for address in mailed))

    def test_removed_members_are_no_longer_notified(self):
        self.lines.sudo().unlink()
        self.env.flush_all()
        self.assertFalse(self.folder.with_user(self.member).has_access('read'))
        self.assertFalse(self.document.with_user(self.portal).has_access('read'))
        for record in (self.folder, self.document):
            with self.subTest(model=record._name):
                partners, mailed = self._notified(record)
                self.assertEqual(partners, self.manager.partner_id | self.contact)
                self.assertFalse(any('follow-portal@example.test' in address for address in mailed))
                self.assertFalse(self.env['mail.message'].with_user(self.member).search_count(
                    [('model', '=', record._name), ('res_id', '=', record.id)]))
                # Naming a partner on purpose still reaches them (HD-10).
                partners, _mailed = self._notified(
                    record, partner_ids=[self.member.partner_id.id])
                self.assertIn(self.member.partner_id, partners)

    def test_moving_away_from_the_grant_stops_notifications(self):
        self.document.sudo().write({'folder_id': self.other.id})
        partners, _mailed = self._notified(self.document)
        self.assertEqual(partners, self.manager.partner_id | self.contact)
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Moving child', 'parent_id': self.folder.id, 'owner_id': self.admin.id})
        child.sudo().message_subscribe(partner_ids=self.followers.ids)
        self.assertEqual(self._notified(child)[0], self.followers)
        child.sudo().write({'parent_id': self.other.id})
        self.assertEqual(self._notified(child)[0], self.manager.partner_id | self.contact)

    def test_follower_filter_matches_the_record_rules(self):
        """The rule mirror used for notifications agrees with has_access for every account."""
        from ..models.mail_thread_guard import _readers_only
        owner = _document_user(self.env, 'wdoc-follow-owner')
        editor = _document_user(self.env, 'wdoc-follow-editor')
        shared = _document_user(self.env, 'wdoc-follow-shared')
        plain = self.env['res.users'].sudo().create({
            'name': 'Follow plain', 'login': 'wdoc-follow-plain', 'email': 'plain@example.test',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])]})
        stranger = _document_user(self.env, 'wdoc-follow-stranger')
        access = self.env['woow.document.folder.access'].sudo()
        access.create([
            {'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': owner.id,
             'level': 'upload'},
            {'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': editor.id,
             'level': 'edit'}])
        owned = self.env['woow.document'].sudo().create({
            'name': 'owned.txt', 'folder_id': self.folder.id, 'owner_id': owner.id,
            'datas': base64.b64encode(b'owned')})
        self.document.sudo().write({'shared_user_ids': [Command.set([shared.id])]})
        trashed = self.env['woow.document'].sudo().create({
            'name': 'trashed.txt', 'folder_id': self.folder.id, 'owner_id': owner.id,
            'datas': base64.b64encode(b'trashed')})
        trashed.sudo().write({'active': False})
        users = (self.member | self.manager | self.portal | owner | editor | shared | plain |
                 stranger)
        records = (self.folder, self.document, owned, trashed)
        for record in records:
            record.sudo().message_subscribe(partner_ids=users.partner_id.ids)
        self.env.flush_all()
        subtype = self.env.ref('mail.mt_comment').id
        for record in records:
            data = self.env['mail.followers']._get_recipient_data(
                record, 'comment', subtype)[record.id]
            recipients = [data[user.partner_id.id] for user in users]
            kept = {item['id'] for item in _readers_only(
                record.with_user(self.admin), recipients, self.env['mail.message'],
                {'partner_ids': []})}
            for user in users:
                with self.subTest(record=record.display_name, user=user.login):
                    self.assertEqual(user.partner_id.id in kept,
                                     record.with_user(user).with_context(
                                         active_test=False).has_access('read'))
