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

    def _note(self, record, text):
        """An internal log note on ``record`` (never sent to followers)."""
        return record.with_user(self.env.ref('base.user_admin')).message_post(
            body=text, message_type='comment', subtype_xmlid='mail.mt_note')

    def test_portal_reader_cannot_test_chatter_text_through_the_message_fields(self):
        """A term that starts at a message field must not answer for the note's text.

        ``message_ids`` and ``website_message_ids`` are auto-join one2many fields:
        core answers a term such as ``message_ids.body`` with a sub-select on the
        message table that skips the message record rules (R-M0), so a share user
        who may read the record could test whether its internal notes contain a
        string. The fields follow the internal group, like the other chatter fields.
        """
        secret = 'ledger-%s' % secrets.token_hex(6)
        notes = {'woow.document': self._note(self.document, 'Internal note %s' % secret),
                 'woow.document.folder': self._note(self.folder, 'Folder note %s' % secret)}
        self.env.flush_all()
        for model, record in (('woow.document', self.document),
                              ('woow.document.folder', self.folder)):
            records = self.env[model].with_user(self.portal)
            self.assertEqual(records.search_count([('id', '=', record.id)]), 1)
            internal = self.env[model].with_user(self.member)
            for field in ('message_ids', 'website_message_ids'):
                domain = [('id', '=', record.id), ('%s.body' % field, 'ilike', secret)]
                probes = {
                    'search': lambda: records.search(domain),
                    'search_count': lambda: records.search_count(domain),
                    'read_group': lambda: records.read_group(domain, ['__count'], []),
                    'any': lambda: records.search_count([
                        ('id', '=', record.id),
                        (field, 'any', [('body', 'ilike', secret)])]),
                }
                for name, probe in probes.items():
                    with self.subTest(model=model, field=field, probe=name), \
                            self.assertRaises(AccessError):
                        probe()
                # Internal readers keep the core behaviour of the same terms.
                self.assertEqual(internal.search_count(domain), 1, (model, field))
            has_message = [('id', '=', record.id), ('has_message', '=', True)]
            with self.subTest(model=model, field='has_message'), self.assertRaises(AccessError):
                records.search_count(has_message)
            self.assertEqual(internal.search_count(has_message), 1)
        self.assertEqual(notes['woow.document'].model, 'woow.document')

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

    def test_portal_rpc_cannot_test_chatter_text_through_the_message_fields(self):
        password = secrets.token_urlsafe(16)
        portal, _member, folder, document, _document_message, _folder_message = _chatter_fixture(
            self.env, 'probe', password=password)
        secret = 'ledger-%s' % secrets.token_hex(6)
        document.with_user(self.env.ref('base.user_admin')).message_post(
            body='Internal note %s' % secret, message_type='comment',
            subtype_xmlid='mail.mt_note')
        self.env.flush_all()
        for model, record in (('woow.document', document), ('woow.document.folder', folder)):
            for field in ('message_ids', 'website_message_ids'):
                domain = [['id', '=', record.id], ['%s.body' % field, 'ilike', secret]]
                for method, args, kwargs in (
                        ('search_count', [domain], {}), ('search', [domain], {}),
                        ('search_read', [domain], {'fields': ['id']}),
                        ('read_group', [domain, ['__count'], []], {})):
                    with self.subTest(user='portal', model=model, field=field, method=method):
                        self.authenticate(portal.login, password)
                        answer = self._call_kw(model, method, args, kwargs)
                        self.assertIn('error', answer)
                        self.assertEqual(answer['error']['data']['name'],
                                         'odoo.exceptions.AccessError')
                        self.assertNotIn(secret, json.dumps(answer))
        self.authenticate('admin', 'admin')
        found = self._call_kw('woow.document', 'search_count',
                              [[['id', '=', document.id], ['message_ids.body', 'ilike', secret]]])
        self.assertEqual(found.get('result'), 1)


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
        """A reply of a user who can read the record is delivered (who else may reply: see
        ``test_reg_inbound_mail_replies.py``); a mail never creates a record, whoever wrote it."""
        gateway = self.env['mail.thread'].sudo()
        subject = 'Reply %s' % secrets.token_hex(4)
        self.assertEqual(gateway.message_process(
            'woow.document', _raw_email(self.reader.email, subject),
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
            _raw_email(self.reader.email, subject), thread_id=self.folder.id), self.folder.id)
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

    def _portal_user(self, login, **extra):
        partner = self.env['res.partner'].sudo().create({
            'name': login, 'email': '%s@example.test' % login})
        return self.env['res.users'].sudo().create(dict({
            'name': login, 'login': login, 'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]}, **extra))

    def _other_company(self):
        """A folder and a document of company B, granted to followers of both companies.

        ``self.portal`` and ``self.member`` belong to company A only, so the company
        rules R-F0 and R-D0 keep them out although they are folder members.
        """
        company_a = self.env.company
        company_b = self.env['res.company'].sudo().create({'name': 'Follow company B'})
        both = _document_user(self.env, 'wdoc-follow-both-companies', company_ids=[
            Command.set([company_a.id, company_b.id])])
        portal_b = self._portal_user('wdoc-follow-portal-b', company_id=company_b.id,
                                     company_ids=[Command.set([company_b.id])])
        folder_b = self.env['woow.document.folder'].sudo().create({
            'name': 'Follow folder B', 'company_id': company_b.id, 'owner_id': False})
        self.env['woow.document.folder.access'].sudo().create([
            {'folder_id': folder_b.id, 'subject_type': 'partner',
             'partner_id': self.portal_partner.id, 'level': 'read'},
            {'folder_id': folder_b.id, 'subject_type': 'partner',
             'partner_id': portal_b.partner_id.id, 'level': 'read'},
            {'folder_id': folder_b.id, 'subject_type': 'user', 'user_id': self.member.id,
             'level': 'read'},
            {'folder_id': folder_b.id, 'subject_type': 'user', 'user_id': both.id,
             'level': 'read'}])
        document_b = self.env['woow.document'].sudo().create({
            'name': 'follow-b.txt', 'folder_id': folder_b.id,
            'datas': base64.b64encode(b'company B')})
        document_b.sudo().write({'shared_partner_ids': [Command.set([self.portal_partner.id])]})
        return company_b, both, portal_b, folder_b, document_b

    def test_follower_filter_matches_the_record_rules(self):
        """The rule mirror used for notifications agrees with has_access for every account.

        Covers per-user and group grants, an archived document, a trashed folder and
        its document, a document shared directly with a portal contact (and with the
        contact of a public account, which no portal rule covers), and a second
        company whose company rule also applies to portal accounts.
        """
        from ..models.mail_thread_guard import _readers_only
        owner = _document_user(self.env, 'wdoc-follow-owner')
        editor = _document_user(self.env, 'wdoc-follow-editor')
        shared = _document_user(self.env, 'wdoc-follow-shared')
        plain = self.env['res.users'].sudo().create({
            'name': 'Follow plain', 'login': 'wdoc-follow-plain', 'email': 'plain@example.test',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])]})
        stranger = _document_user(self.env, 'wdoc-follow-stranger')
        groups = self.env['res.groups'].sudo()
        readers, editors = groups.create({'name': 'Follow readers'}), groups.create(
            {'name': 'Follow editors'})
        user_group = self.env.ref('woow_document_management.group_woow_document_user')
        group_reader = _document_user(self.env, 'wdoc-follow-group-reader', groups_id=[
            Command.set([user_group.id, readers.id])])
        group_editor = _document_user(self.env, 'wdoc-follow-group-editor', groups_id=[
            Command.set([user_group.id, editors.id])])
        portal_shared = self._portal_user('wdoc-follow-portal-shared')
        public_shared = self._portal_user('wdoc-follow-public-shared', groups_id=[
            Command.set([self.env.ref('base.group_public').id])])
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
        folders = self.env['woow.document.folder'].sudo()
        group_folder = folders.create({'name': 'Follow group folder', 'owner_id': self.admin.id})
        access.create({'folder_id': group_folder.id, 'subject_type': 'group',
                       'group_id': readers.id, 'level': 'read'})
        group_document = self.env['woow.document'].sudo().create({
            'name': 'group.txt', 'folder_id': group_folder.id,
            'datas': base64.b64encode(b'group')})
        trashed_folder = folders.create({'name': 'Follow trashed folder',
                                         'owner_id': self.admin.id})
        access.create([
            {'folder_id': trashed_folder.id, 'subject_type': 'user', 'user_id': self.member.id,
             'level': 'read'},
            {'folder_id': trashed_folder.id, 'subject_type': 'partner',
             'partner_id': self.portal_partner.id, 'level': 'read'},
            {'folder_id': trashed_folder.id, 'subject_type': 'group', 'group_id': editors.id,
             'level': 'edit'}])
        in_trashed_folder = self.env['woow.document'].sudo().create({
            'name': 'in-trashed-folder.txt', 'folder_id': trashed_folder.id,
            'datas': base64.b64encode(b'in trashed folder')})
        trashed_folder.with_user(self.admin).action_trash()
        shared_with_portal = self.env['woow.document'].sudo().create({
            'name': 'shared-with-portal.txt', 'folder_id': self.other.id,
            'datas': base64.b64encode(b'shared with portal')})
        shared_with_portal.sudo().write({'shared_partner_ids': [Command.set([
            portal_shared.partner_id.id, public_shared.partner_id.id])]})
        _company_b, both, portal_b, folder_b, document_b = self._other_company()
        self.assertTrue(trashed_folder.in_trash and in_trashed_folder.folder_in_trash)
        users = (self.member | self.manager | self.portal | owner | editor | shared | plain |
                 stranger | group_reader | group_editor | portal_shared | public_shared | both |
                 portal_b)
        records = (self.folder, self.document, owned, trashed, group_folder, group_document,
                   trashed_folder, in_trashed_folder, shared_with_portal, folder_b, document_b)
        for record in records:
            record.sudo().message_subscribe(partner_ids=users.partner_id.ids)
        self.env.flush_all()
        subtype = self.env.ref('mail.mt_comment').id
        readable = set()
        for record in records:
            data = self.env['mail.followers']._get_recipient_data(
                record, 'comment', subtype)[record.id]
            recipients = [data[user.partner_id.id] for user in users]
            kept = {item['id'] for item in _readers_only(
                record.with_user(self.admin), recipients, self.env['mail.message'],
                {'partner_ids': []})}
            for user in users:
                reads = record.with_user(user).with_context(active_test=False).has_access('read')
                if reads:
                    readable.add((record, user))
                with self.subTest(record=record.display_name, user=user.login):
                    self.assertEqual(user.partner_id.id in kept, reads)
        # The fixtures reach both answers on every kind of grant.
        for record, user, reads in (
                (folder_b, both, True), (document_b, portal_b, True),
                (folder_b, self.portal, False), (document_b, self.portal, False),
                (folder_b, self.member, False), (folder_b, self.manager, False),
                (group_folder, group_reader, True), (group_document, group_reader, True),
                (trashed_folder, group_editor, True), (in_trashed_folder, group_editor, False),
                (trashed_folder, self.member, False), (trashed_folder, self.portal, False),
                (shared_with_portal, portal_shared, True), (shared_with_portal, self.portal, False),
                (shared_with_portal, public_shared, False)):
            with self.subTest(expected=record.display_name, user=user.login):
                self.assertEqual((record, user) in readable, reads)

    def test_followers_outside_the_record_company_are_not_notified(self):
        """Posting on a company B folder or document mails no follower who is only in company A.

        The company rule has no groups, so it applies to portal accounts as well: a
        portal member of the folder (and the contact a document is shared with) whose
        account is only in company A gets nothing, like internal members and
        administrators of company A.
        """
        company_b, both, portal_b, folder_b, document_b = self._other_company()
        poster = _document_user(self.env, 'wdoc-follow-poster', company_ids=[
            Command.set([self.env.company.id, company_b.id])], groups_id=[Command.set([
                self.env.ref('woow_document_management.group_woow_document_manager').id])])
        followers = self.followers | both.partner_id | portal_b.partner_id
        company_a_only = self.portal_partner | self.member.partner_id | self.manager.partner_id
        for record in (folder_b, document_b):
            record.sudo().message_subscribe(partner_ids=followers.ids)
        self.env.flush_all()
        for record in (folder_b, document_b):
            with self.subTest(model=record._name):
                self.assertFalse(record.with_user(self.portal).has_access('read'))
                with self.mock_mail_gateway():
                    message = record.with_user(poster).message_post(
                        body='Company B update', message_type='comment',
                        subtype_xmlid='mail.mt_comment')
                self.env.flush_all()
                mailed = {address for mail in self._mails for address in mail['email_to']}
                self.assertFalse(self.env['mail.notification'].sudo().search_count([
                    ('mail_message_id', '=', message.id),
                    ('res_partner_id', 'in', company_a_only.ids)]))
                self.assertEqual(message.sudo().notification_ids.res_partner_id,
                                 both.partner_id | portal_b.partner_id | self.contact)
                self.assertFalse(any('follow-portal@example.test' in address
                                     for address in mailed))
                self.assertTrue(any('wdoc-follow-portal-b@example.test' in address
                                    for address in mailed))
