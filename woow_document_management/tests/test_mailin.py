# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Folder mailboxes: files e-mailed to a folder become documents, for the right senders only.

Every test drives the real entry (``message_process`` with the raw text of a mail, as fetchmail and
the mail gateway script do). A mail is stored only when core's alias check passes (the sender follows
the mailbox) and the sender is, at that moment, an active internal document user with upload level
on the folder; whatever the cause of a refusal, nothing is stored and nothing of the mail is kept.
"""
import ast
import base64
import secrets
from email.message import EmailMessage
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from ..models.mailin import DEFAULTS_KEY, MailInNote, _mail_file, _mail_key
from ..models.quota import LEDGER_KEY, QuotaLedger
from ..services import locks
from ..services.constants import MAIL_IN_MAX_FILES, MIB

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_NO_SIGNUP_MAIL = {'no_reset_password': True}
_DOMAIN = 'mailin-test.example'
_ADDRESS = 'folder-in@%s' % _DOMAIN


def _raw(sender, files=(), inline=(), to=_ADDRESS, extra=(), body='Mail body %s', cid_files=(),
         eml=()):
    """(Message-Id, bytes) of a mail from ``sender`` with ``files`` [(name, bytes)] attached."""
    mail = EmailMessage()
    message_id = '<%s@mailin.example.test>' % secrets.token_hex(12)
    mail['From'] = sender
    mail['To'] = to
    mail['Subject'] = 'Files %s' % secrets.token_hex(3)
    mail['Message-Id'] = message_id
    for name, value in extra:
        mail[name] = value
    mail.set_content(body % secrets.token_hex(4))
    if inline:
        mail.add_alternative('<p>look <img src="cid:picture1"></p>', subtype='html')
        for name, data in inline:
            mail.get_payload()[1].add_related(
                data, maintype='image', subtype='png', cid='<picture1>', filename=name)
    for name, data in files:
        mail.add_attachment(data, maintype='application', subtype='octet-stream', filename=name)
    for name, data, cid in cid_files:        # ordinary attachments that also carry a Content-ID
        mail.add_attachment(data, maintype='application', subtype='octet-stream', filename=name,
                            cid=cid)
    for name, inner in eml:                  # saved or forwarded mails (message/rfc822)
        mail.add_attachment(inner, filename=name)
    return message_id, mail.as_bytes()


class _MailinFixture:

    @classmethod
    def _user(cls, login, groups, **extra):
        return new_test_user(
            cls.env, login='wdoc-mi-%s' % login, groups=groups, name='Mailin %s' % login,
            email='Mailin.%s@Example.test' % login, context=_NO_SIGNUP_MAIL, **extra)

    @classmethod
    def _make_fixture(cls):
        env = cls.env
        cls.company = env.company
        cls.domain = env['mail.alias.domain'].sudo().create({
            'name': _DOMAIN, 'company_ids': [Command.link(cls.company.id)]})
        cls.admin = env.ref('base.user_admin')
        cls.manager = cls._user('manager', _MANAGER)
        cls.uploader = cls._user('uploader', _USER)
        cls.editor = cls._user('editor', _USER)
        cls.grouped = cls._user('grouped', _USER)
        cls.reader = cls._user('reader', _USER)
        cls.nogrant = cls._user('nogrant', _USER)
        cls.plain = cls._user('plain', 'base.group_user')
        cls.outsider = cls._user('outsider', _USER)
        cls.left = cls._user('left', _USER)
        cls.archived = cls._user('archived', _USER)
        cls.portal_partner = env['res.partner'].sudo().create({
            'name': 'Mailin portal contact', 'email': 'mailin-portal@example.test'})
        cls.portal = new_test_user(
            env, login='wdoc-mi-portal', groups='base.group_portal',
            partner_id=cls.portal_partner.id, email=cls.portal_partner.email,
            context=_NO_SIGNUP_MAIL)
        cls.contact = env['res.partner'].sudo().create({
            'name': 'Mailin staff contact', 'email': 'mailin-contact@example.test'})
        cls.folder = env['woow.document.folder'].sudo().create({
            'name': 'Mailin folder', 'owner_id': cls.admin.id})
        access = env['woow.document.folder.access'].sudo()
        for user, level in ((cls.uploader, 'upload'), (cls.editor, 'edit'), (cls.reader, 'read'),
                            (cls.left, 'upload'), (cls.archived, 'upload')):
            access.create({'folder_id': cls.folder.id, 'subject_type': 'user',
                           'user_id': user.id, 'level': level})
        cls.group = env['res.groups'].sudo().create({'name': 'Mailin senders'})
        cls.grouped.sudo().write({'groups_id': [Command.link(cls.group.id)]})
        access.create({'folder_id': cls.folder.id, 'subject_type': 'group',
                       'group_id': cls.group.id, 'level': 'upload'})
        cls.tag = env['woow.document.tag'].sudo().create({'name': 'Mailin tag'})
        cls.box = env['woow.document.mail.intake'].with_user(cls.manager).create({
            'folder_id': cls.folder.id, 'alias_name': 'folder-in',
            'tag_ids': [Command.set([cls.tag.id])]})
        cls.left.sudo().write({'groups_id': [Command.unlink(env.ref(_USER).id)]})
        cls.archived.sudo().write({'active': False})
        cls.box.sudo().message_subscribe(partner_ids=[
            user.partner_id.id for user in (
                cls.uploader, cls.editor, cls.grouped, cls.reader, cls.nogrant, cls.plain,
                cls.left, cls.archived, cls.portal, cls.manager)] + [cls.contact.id])
        env.flush_all()

    @property
    def gateway(self):
        """The mail gateway of fetchmail: the superuser."""
        return self.env['mail.thread'].sudo()

    def _state(self):
        """What a mail can write: documents, messages, files, followers, notifications, mails."""
        self.env.flush_all()
        env = self.env
        return {
            'documents': env['woow.document'].sudo().with_context(active_test=False).search_count(
                [('folder_id', '=', self.folder.id)]),
            'messages': env['mail.message'].sudo().search_count([('model', '=', self.box._name)]),
            'files': env['ir.attachment'].sudo().with_context(active_test=False).search_count([]),
            'followers': sorted(env['mail.followers'].sudo().search(
                [('res_model', '=', self.box._name)]).mapped('partner_id').ids),
            'notifications': env['mail.notification'].sudo().search_count([]),
        }

    def _send(self, sender, files=(), **kwargs):
        message_id, raw = _raw(sender, files, **kwargs)
        return message_id, self.gateway.message_process(False, raw)

    def _documents(self):
        return self.env['woow.document'].sudo().search(
            [('folder_id', '=', self.folder.id)], order='id')

    def _mail_in_rows(self):
        return self.env['woow.document.log'].sudo().search(
            [('event', '=', 'mail_in'), ('folder_id', '=', self.folder.id)], order='id')

    def _assert_nothing_stored(self, before):
        self.assertEqual({key: value for key, value in self._state().items()
                          if key != 'notifications'},
                         {key: value for key, value in before.items() if key != 'notifications'})
        self.assertFalse(self.env['ir.attachment'].sudo().search_count(
            [('res_model', '=', self.box._name)]))


@tagged('woow_docmgmt_core')
class TestMailinReceiving(_MailinFixture, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def test_the_alias_is_set_up_for_followers_and_names_its_mailbox(self):
        alias = self.box.alias_id.sudo()
        self.assertEqual(alias.alias_full_name, _ADDRESS)
        self.assertEqual(self.box.alias_email, _ADDRESS)
        self.assertEqual(alias.alias_contact, 'followers')
        self.assertEqual(alias.alias_model_id.model, 'woow.document.mail.intake')
        self.assertEqual((alias.alias_parent_model_id.model, alias.alias_parent_thread_id),
                         ('woow.document.mail.intake', self.box.id))
        self.assertFalse(alias.alias_force_thread_id)
        self.assertEqual(ast.literal_eval(alias.alias_defaults), {DEFAULTS_KEY: self.box.id})

    def test_a_follower_with_upload_level_files_the_attachments_and_nothing_else_is_kept(self):
        before = self._state()
        sender = self.uploader
        message_id, result = self._send(
            sender.email, [('one.txt', b'first file'), ('two.txt', b'second file')])
        self.assertEqual(result, self.box.id)
        documents = self._documents()
        self.assertEqual(len(documents), 2)
        self.assertEqual(sorted(documents.mapped('name')), ['one.txt', 'two.txt'])
        self.assertEqual(documents.mapped('owner_id'), sender)
        self.assertEqual(documents.mapped('tag_ids'), self.tag)
        self.assertEqual(documents.mapped('folder_id'), self.folder)
        self.assertTrue(all(doc.attachment_id and doc.checksum and doc.mimetype
                            for doc in documents))
        self.assertEqual(set(documents.mapped('document_type')), {'binary'})
        self.assertEqual(documents.mapped('datas') and [base64.b64decode(doc.datas)
                                                        for doc in documents.sorted('name')],
                         [b'first file', b'second file'])
        # the upload policy ran: the audit log has an upload row per file and one mail_in row
        uploads = self.env['woow.document.log'].sudo().search(
            [('event', '=', 'upload'), ('document_id', 'in', documents.ids)])
        self.assertEqual(len(uploads), 2)
        rows = self._mail_in_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.detail, {'files': 2, 'refused': 0, 'ignored': 0})
        self.assertEqual(rows.user_id, sender)
        self.assertEqual(rows.folder_id, self.folder)
        # nothing of the mail remains on the mailbox thread but one short internal note
        after = self._state()
        self.assertEqual(after['documents'], before['documents'] + 2)
        self.assertEqual(after['messages'], before['messages'] + 1)
        self.assertEqual(after['followers'], before['followers'])
        self.assertEqual(after['notifications'], before['notifications'])
        self.assertFalse(self.env['ir.attachment'].sudo().search_count(
            [('res_model', '=', self.box._name)]))
        note = self.env['mail.message'].sudo().search(
            [('model', '=', self.box._name), ('res_id', '=', self.box.id),
             ('message_id', '=', _mail_key(self.box.id, message_id))])
        self.assertEqual(len(note), 1)
        # the Message-Id header of the mail is not kept, only a one-way key of it
        self.assertFalse(self.env['mail.message'].sudo().search_count(
            [('message_id', '=', message_id)]))
        self.assertNotIn('mailin.example.test', note.message_id)
        self.assertEqual((note.message_type, note.is_internal), ('notification', True))
        self.assertFalse(note.attachment_ids)
        self.assertFalse(note.partner_ids)
        self.assertNotEqual(note.author_id, sender.partner_id)
        text = ' '.join(str(value) for value in (
            note.body, note.subject, note.email_from, note.preview, note.record_name))
        for secret in ('Mail body', 'Files ', sender.email.lower(), sender.partner_id.name,
                       'one.txt', 'two.txt', _ADDRESS):
            self.assertNotIn(secret.lower(), text.lower())
        self.assertIn('2', note.body)

    def test_the_followers_are_not_notified_and_nobody_is_subscribed(self):
        before = self._state()
        self._send(self.editor.email, [('three.txt', b'third')])
        self.assertEqual(self._state()['followers'], before['followers'])
        self.assertEqual(self._state()['notifications'], before['notifications'])

    def test_every_sender_with_upload_level_by_user_group_or_edit_is_accepted(self):
        for who in (self.uploader, self.editor, self.grouped, self.manager):
            with self.subTest(sender=who.login):
                before = self._state()
                _id, result = self._send(who.email, [('%s.txt' % who.login, b'by ' + who.login.encode())])
                self.assertEqual(result, self.box.id)
                self.assertEqual(self._state()['documents'], before['documents'] + 1)
                self.assertEqual(self._documents()[-1].owner_id, who)

    def test_a_follower_without_upload_level_stores_nothing(self):
        for who in (self.reader, self.nogrant):
            with self.subTest(sender=who.login):
                before = self._state()
                self._send(who.email, [('read.txt', b'by a reader')])
                self._assert_nothing_stored(before)

    def test_a_sender_who_does_not_follow_the_mailbox_stores_nothing(self):
        for who in (self.outsider,):
            with self.subTest(sender=who.login):
                before = self._state()
                self._send(who.email, [('out.txt', b'by an outsider')])
                self._assert_nothing_stored(before)

    def test_an_external_or_unknown_sender_stores_nothing(self):
        for sender in ('nobody@elsewhere.example.test', 'Somebody <somebody@elsewhere.example.test>',
                       'OdooBot <odoobot@example.com>', self.contact.email):
            with self.subTest(sender=sender):
                before = self._state()
                self._send(sender, [('ext.txt', b'by an external sender')])
                self._assert_nothing_stored(before)

    def test_a_portal_user_who_follows_the_mailbox_stores_nothing(self):
        before = self._state()
        self._send(self.portal.email, [('portal.txt', b'by a portal user')])
        self._assert_nothing_stored(before)

    def test_a_plain_internal_user_without_the_document_group_stores_nothing(self):
        before = self._state()
        self._send(self.plain.email, [('plain.txt', b'by a plain user')])
        self._assert_nothing_stored(before)

    def test_an_archived_user_and_one_who_left_the_document_group_store_nothing(self):
        for who in (self.archived, self.left):
            with self.subTest(sender=who.login):
                before = self._state()
                self._send(who.email, [('gone.txt', b'by a user who is gone')])
                self._assert_nothing_stored(before)

    def test_the_right_is_the_one_at_the_moment_of_the_mail(self):
        user = self._user('moving', _USER)
        access = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': user.id,
            'level': 'upload'})
        self.box.sudo().message_subscribe(partner_ids=[user.partner_id.id])
        before = self._state()
        self._send(user.email, [('first.txt', b'while allowed')])
        self.assertEqual(self._state()['documents'], before['documents'] + 1)
        access.write({'level': 'read'})
        self.env.flush_all()
        before = self._state()
        self._send(user.email, [('second.txt', b'after the right was lowered')])
        self._assert_nothing_stored(before)

    def test_a_folder_in_the_trash_stores_nothing(self):
        self.folder.sudo().write({'active': False})
        self.env.flush_all()
        for who in (self.uploader, self.manager):
            with self.subTest(sender=who.login):
                before = self._state()
                self._send(who.email, [('trash.txt', b'to a trashed folder')])
                self.assertEqual(self._state()['documents'], before['documents'])
                self.assertEqual(self._state()['messages'], before['messages'])

    def test_a_mail_to_an_inactive_mailbox_stores_nothing(self):
        self.box.with_user(self.manager).write({'active': False})
        self.env.flush_all()
        before = self._state()
        self._send(self.uploader.email, [('inactive.txt', b'to an inactive mailbox')])
        self._assert_nothing_stored(before)
        self.box.with_user(self.manager).write({'active': True})
        self.env.flush_all()
        self._send(self.uploader.email, [('active.txt', b'to an active mailbox')])
        self.assertEqual(self._state()['documents'], before['documents'] + 1)

    def test_only_the_first_files_of_a_mail_are_stored(self):
        files = [('many-%02d.txt' % index, b'file %d' % index)
                 for index in range(MAIL_IN_MAX_FILES + 3)]
        before = self._state()
        self._send(self.uploader.email, files)
        self.assertEqual(self._state()['documents'], before['documents'] + MAIL_IN_MAX_FILES)
        self.assertEqual(sorted(self._documents().mapped('name')),
                         [name for name, _data in files[:MAIL_IN_MAX_FILES]])
        self.assertEqual(self._mail_in_rows().detail,
                         {'files': MAIL_IN_MAX_FILES, 'refused': 0, 'ignored': 3})

    def test_a_refused_file_does_not_stop_the_others(self):
        before = self._state()
        self._send(self.uploader.email, [
            ('bad.exe', b'MZ not allowed'), ('good.txt', b'allowed'), ('empty.txt', b''),
            ('also-good.txt', b'allowed too')])
        self.assertEqual(sorted(self._documents().mapped('name')), ['also-good.txt', 'good.txt'])
        self.assertEqual(self._state()['documents'], before['documents'] + 2)
        self.assertEqual(self._mail_in_rows().detail, {'files': 2, 'refused': 2, 'ignored': 0})
        self.assertFalse(self.env['ir.attachment'].sudo().search_count(
            [('res_model', '=', self.box._name)]))

    def test_odd_file_names_never_break_the_mail(self):
        names = ['../../escape.txt', 'back\\slash.txt', 'x' * 300 + '.txt', '.txt', 'no-extension',
                 '  spaced  .txt', 'caf\u00e9 \u6587\u4ef6.txt']
        before = self._state()
        self._send(self.uploader.email, [(name, b'odd name') for name in names])
        detail = self._mail_in_rows().detail
        self.assertEqual(detail['files'] + detail['refused'], len(names))
        self.assertEqual(detail['ignored'], 0)
        self.assertEqual(self._state()['documents'], before['documents'] + detail['files'])
        for name in self._documents().mapped('name'):
            self.assertNotIn('/', name)
            self.assertNotIn('..', name)

    def test_a_mail_without_an_attachment_stores_nothing_and_leaves_a_note(self):
        before = self._state()
        _id, result = self._send(self.uploader.email, [])
        self.assertEqual(result, self.box.id)
        self.assertEqual(self._state()['documents'], before['documents'])
        self.assertEqual(self._state()['messages'], before['messages'] + 1)
        self.assertEqual(self._mail_in_rows().detail, {'files': 0, 'refused': 0, 'ignored': 0})

    def test_an_image_of_the_body_is_not_a_file(self):
        before = self._state()
        self._send(self.uploader.email, [('real.txt', b'a file')],
                   inline=[('picture.png', b'\x89PNG\r\n\x1a\nfake')])
        self.assertEqual(self._documents().mapped('name'), ['real.txt'])
        self.assertEqual(self._state()['documents'], before['documents'] + 1)

    def test_the_quota_stops_a_file_the_site_has_no_room_for(self):
        self._send(self.uploader.email, [('before-quota.txt', b'small')])
        self.assertEqual(len(self._documents()), 1)
        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '1')
        locks._reset_tracker(self.env.cr)   # the fixture took the lock of a member list in this transaction
        before = self._state()
        self._send(self.uploader.email, [('big.txt', b'x' * (2 * MIB))])
        self.assertEqual(self._state()['documents'], before['documents'])
        self.assertEqual(self._mail_in_rows()[-1].detail, {'files': 0, 'refused': 1, 'ignored': 0})
        self.assertFalse(self.env['ir.attachment'].sudo().search_count(
            [('res_model', '=', self.box._name)]))

    def test_a_mail_that_was_received_is_not_filed_twice(self):
        message_id, raw = _raw(self.uploader.email, [('once.txt', b'only once')])
        self.gateway.message_process(False, raw)
        before = self._state()
        self.gateway.message_process(False, raw)
        self.assertEqual(self._state()['documents'], before['documents'])
        self.assertEqual(self._state()['messages'], before['messages'])
        self.assertEqual(self._mail_in_rows().mapped('detail'),
                         [{'files': 1, 'refused': 0, 'ignored': 0}])

    def test_an_attachment_with_a_content_id_that_the_body_does_not_use_is_a_file(self):
        before = self._state()
        self._send(self.uploader.email, [('plain.txt', b'no content id')],
                   cid_files=[('report.txt', b'from a mail client', '<f_lm9abc>')])
        self.assertEqual(sorted(self._documents().mapped('name')), ['plain.txt', 'report.txt'])
        self.assertEqual(self._state()['documents'], before['documents'] + 2)
        self.assertEqual(self._mail_in_rows().detail, {'files': 2, 'refused': 0, 'ignored': 0})

    def test_only_an_image_that_the_body_points_at_is_inline(self):
        self._send(self.uploader.email, [('real.txt', b'a file')],
                   inline=[('picture.png', b'\x89PNG\r\n\x1a\nfake')],
                   cid_files=[('other.txt', b'has a content id, not in the body', '<other1>')])
        self.assertEqual(sorted(self._documents().mapped('name')), ['other.txt', 'real.txt'])

    def test_a_forwarded_mail_is_read_as_a_file_and_judged_by_the_policy(self):
        inner = EmailMessage()
        inner['From'] = 'someone@elsewhere.example.test'
        inner['Subject'] = 'forwarded'
        inner.set_content('the forwarded text')
        # the part is read as bytes (as core does) and handed to the upload policy, which does not
        # list .eml: the file is counted as refused, it does not vanish
        before = self._state()
        self._send(self.uploader.email, [('kept.txt', b'next to it')], eml=[('fwd.eml', inner)])
        self.assertEqual(self._documents().mapped('name'), ['kept.txt'])
        self.assertEqual(self._state()['documents'], before['documents'] + 1)
        self.assertEqual(self._mail_in_rows().detail, {'files': 1, 'refused': 1, 'ignored': 0})
        name, data = _mail_file(('fwd.eml', inner, {}))
        self.assertEqual(name, 'fwd.eml')
        self.assertIn(b'the forwarded text', data)

    def test_a_part_that_cannot_be_read_is_counted_as_refused(self):
        box = self.box.sudo()
        documents = box._woow_sender_documents({
            'author_id': self.uploader.partner_id.id, 'email_from': self.uploader.email})
        note = box._woow_store_mail_files(documents, {'attachments': [
            ('weird.bin', object(), {}), 'not a part', ('ok.txt', b'fine', {})]})
        self.assertEqual((note.stored, note.refused, note.ignored), (1, 2, 0))

    def test_the_files_of_one_mail_share_one_quota_ledger(self):
        """What the files of a mail have written is not committed: they must share one ledger."""
        quota_model = type(self.env['woow.document.quota'])
        real = quota_model._check_many
        seen = []

        def spy(quota, items, *args, **kwargs):
            seen.append(quota.env.context.get(LEDGER_KEY))
            return real(quota, items, *args, **kwargs)

        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '5')
        locks._reset_tracker(self.env.cr)
        with patch.object(quota_model, '_check_many', spy):
            self._send(self.uploader.email, [('led-%d.txt' % index, b'file %d' % index)
                                             for index in range(3)])
            self._send(self.uploader.email, [('next.txt', b'another mail')])
        self.assertEqual(len(seen), 4)
        self.assertTrue(all(isinstance(ledger, QuotaLedger) for ledger in seen))
        self.assertTrue(seen[0] is seen[1] is seen[2])
        self.assertIsNot(seen[2], seen[3])         # a new mail measures again
        self.assertEqual(len(self._documents()), 4)

    def test_a_busy_quota_lock_refuses_the_file_and_not_the_mail(self):
        real = type(self.env['woow.document.quota'])._check_many
        calls = []

        def busy(quota, items, *args, **kwargs):
            calls.append(items)
            if len(calls) == 1:
                raise locks.LockBusy('quota')
            return real(quota, items, *args, **kwargs)

        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '1')
        locks._reset_tracker(self.env.cr)
        with patch.object(type(self.env['woow.document.quota']), '_check_many', busy):
            self._send(self.uploader.email, [('first.txt', b'busy'), ('second.txt', b'free')])
        self.assertEqual(self._documents().mapped('name'), ['second.txt'])
        self.assertEqual(self._mail_in_rows()[-1].detail, {'files': 1, 'refused': 1, 'ignored': 0})
        self.assertEqual(self.box.alias_id.sudo().alias_status, 'valid')

    def test_a_tag_of_another_company_cannot_be_put_on_a_mailbox(self):
        other = self.env['res.company'].sudo().create({'name': 'Mailin company B'})
        foreign = self.env['woow.document.tag'].sudo().create({
            'name': 'Mailin foreign tag', 'company_id': other.id})
        with self.assertRaises(ValidationError):
            self.box.with_user(self.manager).write({'tag_ids': [Command.link(foreign.id)]})
        self.assertEqual(self.box.tag_ids, self.tag)

    def test_a_forged_from_header_is_not_accepted(self):
        before = self._state()
        spoof = '=?utf-8?q?%s?= <%s>' % (self.uploader.email.replace('@', '=40'),
                                         'someone-else@elsewhere.example.test')
        with self.assertRaises(AccessError):
            self._send(spoof, [('spoof.txt', b'spoofed')])
        self.assertEqual(self._state()['documents'], before['documents'])

    def test_the_gateway_script_of_a_system_administrator_files_the_mail_too(self):
        gateway = self.env['mail.thread'].with_user(self.admin)
        self.assertTrue(self.admin._is_system() and not gateway.env.su)
        before = self._state()
        _id, raw = _raw(self.uploader.email, [('script.txt', b'by the script')])
        self.assertEqual(gateway.message_process(False, raw), self.box.id)
        self.assertEqual(self._state()['documents'], before['documents'] + 1)
        _id, raw = _raw(self.reader.email, [('script2.txt', b'by a reader')])
        gateway.message_process(False, raw)
        self.assertEqual(self._state()['documents'], before['documents'] + 1)

    def test_an_ordinary_user_cannot_push_a_mail_through_the_gateway(self):
        before = self._state()
        # an administrator of documents sees the alias and is refused by the route guard; another
        # user does not even find the alias (it is hidden from them), so no route exists
        for user, refusal in ((self.manager, AccessError), (self.uploader, ValueError),
                              (self.plain, ValueError)):
            with self.subTest(user=user.login):
                _id, raw = _raw(self.uploader.email, [('rpc.txt', b'by rpc')])
                with self.assertRaises(refusal):
                    self.env['mail.thread'].with_user(user).message_process(False, raw)
                _id, raw = _raw(self.uploader.email, [('rpc.txt', b'by rpc')])
                with self.assertRaises(AccessError):
                    self.env['mail.thread'].with_user(user).message_process(
                        'woow.document.mail.intake', raw)
        self.assertEqual(self._state()['documents'], before['documents'])

    def test_a_reply_to_the_note_is_not_a_mail_for_the_mailbox(self):
        message_id, _result = self._send(self.uploader.email, [('note.txt', b'first')])
        note = self.env['mail.message'].sudo().search(
            [('message_id', '=', _mail_key(self.box.id, message_id))])
        self.assertEqual(len(note), 1)
        before = self._state()
        _id, raw = _raw(self.uploader.email, [('reply.txt', b'reply')],
                        extra=[('In-Reply-To', note.message_id), ('References', note.message_id)],
                        to='nobody@%s' % _DOMAIN)
        with self.assertRaises(AccessError):
            self.gateway.message_process(False, raw)
        self.assertEqual(self._state()['documents'], before['documents'])
        self.assertEqual(self._state()['messages'], before['messages'])


@tagged('woow_docmgmt_core')
class TestMailinDirectCalls(_MailinFixture, TransactionCase):
    """``message_new`` is a public name: only the gateway reaches it, whatever the values say."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def _msg(self, sender):
        return {'author_id': sender.partner_id.id, 'email_from': sender.email,
                'attachments': [('direct.txt', b'direct', {})], 'message_id': '<direct@x.test>'}

    def test_an_environment_that_is_not_the_superuser_is_refused(self):
        before = self._state()
        model = self.env['woow.document.mail.intake']
        for user in (self.manager, self.uploader, self.admin, self.plain, self.portal):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                model.with_user(user).message_new(self._msg(self.uploader), {DEFAULTS_KEY: self.box.id})
        self.assertEqual(self._state()['documents'], before['documents'])

    def test_values_that_name_no_mailbox_are_refused(self):
        before = self._state()
        model = self.env['woow.document.mail.intake'].sudo()
        for values in (None, {}, {DEFAULTS_KEY: 0}, {DEFAULTS_KEY: -1}, {DEFAULTS_KEY: 10 ** 7},
                       {DEFAULTS_KEY: str(self.box.id)}, {DEFAULTS_KEY: True},
                       {DEFAULTS_KEY: [self.box.id]}, {'other': self.box.id}):
            with self.subTest(values=values), self.assertRaises(AccessError):
                model.message_new(self._msg(self.uploader), values)
        for message in (None, {}, 'text', {'author_id': 0, 'email_from': self.uploader.email},
                        {'author_id': self.uploader.partner_id.id, 'email_from': 'a@b.test'},
                        {'author_id': self.uploader.partner_id.id, 'email_from': None}):
            with self.subTest(message=message), self.assertRaises(AccessError):
                model.message_new(message, {DEFAULTS_KEY: self.box.id})
        self.assertEqual(self._state()['documents'], before['documents'])

    def test_the_sender_is_judged_even_when_the_values_are_right(self):
        before = self._state()
        model = self.env['woow.document.mail.intake'].sudo()
        for who in (self.reader, self.outsider, self.plain, self.portal, self.left, self.archived):
            with self.subTest(sender=who.login), self.assertRaises(AccessError):
                model.message_new(self._msg(who), {DEFAULTS_KEY: self.box.id})
        self.assertEqual(self._state()['documents'], before['documents'])
        thread = model.message_new(self._msg(self.uploader), {DEFAULTS_KEY: self.box.id})
        self.assertEqual(thread, self.box)
        self.assertIsInstance(thread.env.context['woow_mail_in_note'], MailInNote)
        self.assertEqual(self._state()['documents'], before['documents'] + 1)

    def test_a_context_flag_from_a_client_is_not_the_marker(self):
        """A caller can send any JSON in the context, never an instance of the marker."""
        box = self.box.with_user(self.manager).with_context(woow_mail_in_note={'stored': 9})
        before = self._state()
        box.message_post(body='A note of an administrator', message_type='comment',
                         subtype_xmlid='mail.mt_note')
        self.assertEqual(self._state()['messages'], before['messages'] + 1)
        message = self.env['mail.message'].sudo().search(
            [('model', '=', self.box._name), ('body', 'ilike', 'administrator')])
        self.assertEqual(len(message), 1)

    def test_message_update_is_refused_for_every_caller(self):
        for user in (self.manager, self.uploader, self.admin):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.box.with_user(user).message_update(self._msg(self.uploader))
        with self.assertRaises(AccessError):
            self.box.sudo().message_update(self._msg(self.uploader))


@tagged('woow_docmgmt_core')
class TestMailinAdministration(_MailinFixture, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def test_only_document_administrators_use_the_model(self):
        model = self.env['woow.document.mail.intake']
        for user in (self.uploader, self.editor, self.plain, self.portal, self.reader):
            with self.subTest(user=user.login):
                for right in ('read', 'write', 'create', 'unlink'):
                    self.assertFalse(model.with_user(user).check_access_rights(
                        right, raise_exception=False))
                with self.assertRaises(AccessError):
                    model.with_user(user).search([])
                with self.assertRaises(AccessError):
                    self.box.with_user(user).read(['folder_id'])
                with self.assertRaises(AccessError):
                    model.with_user(user).create({'folder_id': self.folder.id, 'alias_name': 'x-in'})
                with self.assertRaises(AccessError):
                    self.box.with_user(user).write({'active': False})
                with self.assertRaises(AccessError):
                    self.box.with_user(user).unlink()
        self.assertEqual(model.with_user(self.manager).search_count([('id', '=', self.box.id)]), 1)

    def test_the_chatter_of_a_mailbox_is_not_open_to_others(self):
        for user in (self.uploader, self.plain, self.portal):
            with self.subTest(user=user.login):
                self.assertFalse(self.env['woow.document.mail.intake'].with_user(
                    user)._get_thread_with_access(self.box.id))
        self.assertEqual(self.env['woow.document.mail.intake'].with_user(
            self.manager)._get_thread_with_access(self.box.id), self.box.with_user(self.manager))

    def test_a_manager_of_another_company_does_not_see_the_mailbox(self):
        other = self.env['res.company'].sudo().create({'name': 'Mailin company B'})
        manager_b = self._user('manager-b', _MANAGER, company_id=other.id,
                               company_ids=[Command.set([other.id])])
        self.assertEqual(self.env['woow.document.mail.intake'].with_user(manager_b).search_count(
            [('id', '=', self.box.id)]), 0)
        with self.assertRaises(AccessError):
            self.box.with_user(manager_b).read(['folder_id'])
        self.assertEqual(self.box.company_id, self.company)

    def test_the_address_and_the_followers_of_a_mailbox_are_hidden_from_other_internal_users(self):
        model = self.env['ir.model']._get_id('res.partner')
        other = self.env['mail.alias'].sudo().create({
            'alias_name': 'other-alias', 'alias_model_id': model,
            'alias_domain_id': self.domain.id})
        both = [self.box.alias_id.id, other.id]
        for user in (self.uploader, self.editor, self.plain):
            with self.subTest(user=user.login):
                self.assertEqual(self.env['mail.alias'].with_user(user).search(
                    [('id', 'in', both)]), other)
                self.assertFalse(self.env['mail.followers'].with_user(user).search(
                    [('res_model', '=', self.box._name)]))
                with self.assertRaises(AccessError):
                    self.box.alias_id.with_user(user).read(['alias_name'])
        for user in (self.manager, self.admin):
            with self.subTest(user=user.login):
                self.assertEqual(self.env['mail.alias'].with_user(user).search(
                    [('id', 'in', both)]), self.box.alias_id | other)
                self.assertTrue(self.env['mail.followers'].with_user(user).search(
                    [('res_model', '=', self.box._name)]))

    def test_a_folder_has_one_mailbox(self):
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['woow.document.mail.intake'].with_user(self.manager).create({
                    'folder_id': self.folder.id, 'alias_name': 'second-in'})

    def test_the_routing_of_the_alias_cannot_be_changed(self):
        box = self.box.with_user(self.manager)
        model = self.env['ir.model']._get_id('res.partner')
        for vals in ({'alias_defaults': '{}'}, {'alias_contact': 'everyone'},
                     {'alias_force_thread_id': self.box.id}, {'alias_model_id': model},
                     {'alias_parent_thread_id': 1}, {'alias_status': 'valid'}):
            with self.subTest(vals=vals), self.assertRaises(UserError):
                box.write(vals)
        for vals in ({'alias_contact': 'everyone'}, {'alias_defaults': '{}'},
                     {'alias_model_id': model}, {'alias_force_thread_id': 1}):
            with self.subTest(create=vals), self.assertRaises(UserError):
                self.env['woow.document.mail.intake'].with_user(self.manager).create(
                    dict(vals, folder_id=self.env['woow.document.folder'].sudo().create(
                        {'name': 'Another mailin folder'}).id, alias_name='other-in'))
        alias = self.box.alias_id.sudo()
        self.assertEqual((alias.alias_contact, alias.alias_model_id.model),
                         ('followers', 'woow.document.mail.intake'))
        # the name, the tags and the archive flag are the administrator's to change
        box.write({'alias_name': 'renamed-in', 'tag_ids': [Command.clear()]})
        self.assertEqual(self.box.alias_email, 'renamed-in@%s' % _DOMAIN)
        self.assertFalse(self.box.tag_ids)

    def test_a_mailbox_cannot_take_the_alias_of_another_record(self):
        manager = self.env['woow.document.mail.intake'].with_user(self.manager)
        foreign = self.env['mail.alias'].sudo().create({
            'alias_name': 'poc-foreign', 'alias_model_id': self.env['ir.model']._get_id('res.partner'),
            'alias_contact': 'everyone', 'alias_defaults': "{'name': 'orig'}"})
        other_folder = self.env['woow.document.folder'].sudo().create({'name': 'Mailin alias folder'})
        snapshot = (foreign.alias_model_id.model, foreign.alias_contact, foreign.alias_defaults)
        for alias_id in (foreign.id, self.box.alias_id.id, 99999999, foreign):
            with self.subTest(create=alias_id), self.assertRaises(UserError):
                manager.create({'folder_id': other_folder.id, 'alias_id': alias_id})
        own = manager.create({'folder_id': other_folder.id, 'alias_name': 'own-alias-in'})
        for value in (foreign.id, self.box.alias_id.id, False, 99999999):
            with self.subTest(write=value), self.assertRaises(UserError):
                own.write({'alias_id': value})
        own.write({'alias_id': own.alias_id.id, 'tag_ids': [Command.clear()]})   # its own: no change
        own.unlink()
        self.assertTrue(foreign.exists())
        self.assertEqual((foreign.alias_model_id.model, foreign.alias_contact,
                          foreign.alias_defaults), snapshot)
        self.assertTrue(self.box.exists() and self.box.alias_id.exists())

    def test_an_alias_belongs_to_one_mailbox_and_routes_to_the_mailboxes(self):
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                other = self.env['woow.document.folder'].sudo().create({'name': 'Mailin dup alias'})
                self.env['woow.document.mail.intake'].sudo().create({
                    'folder_id': other.id, 'alias_name': 'dup-in'})
                self.env.cr.execute(
                    'UPDATE woow_document_mail_intake SET alias_id = %s WHERE folder_id = %s',
                    [self.box.alias_id.id, other.id])
        foreign = self.env['mail.alias'].sudo().create({
            'alias_name': 'poc-foreign2', 'alias_model_id': self.env['ir.model']._get_id('res.partner')})
        other = self.env['woow.document.folder'].sudo().create({'name': 'Mailin wrong model'})
        box = self.env['woow.document.mail.intake'].sudo().create({
            'folder_id': other.id, 'alias_name': 'wrong-in'})
        self.env.cr.execute('UPDATE woow_document_mail_intake SET alias_id = %s WHERE id = %s',
                            [foreign.id, box.id])
        box.invalidate_recordset()
        with self.assertRaises(ValidationError):
            box._check_alias_is_ours()

    def test_copying_a_mailbox_is_not_offered(self):
        with self.assertRaises(UserError):
            self.box.with_user(self.manager).copy()

    def test_deleting_a_folder_for_good_takes_its_mailbox_and_alias_along(self):
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Mailin to purge'})
        box = self.env['woow.document.mail.intake'].with_user(self.manager).create({
            'folder_id': folder.id, 'alias_name': 'purge-in'})
        alias = box.alias_id.sudo()
        # a folder that is not in the trash is refused, and its mailbox stays
        with self.assertRaises(UserError):
            folder.with_user(self.manager).unlink()
        self.assertTrue(box.exists() and alias.exists())
        folder.write({'active': False})
        folder.with_user(self.manager).unlink()
        self.assertFalse(box.exists() or alias.exists())
        with self.assertRaises(AccessError):
            folder.with_user(self.uploader).unlink()

    def test_the_screens_load_for_an_administrator(self):
        model = self.env['woow.document.mail.intake'].with_user(self.manager)
        for view in ('list', 'form', 'search'):
            with self.subTest(view=view):
                self.assertTrue(model.get_view(view_type=view)['arch'])
        action = self.env.ref('woow_document_management.action_woow_document_mail_intake')
        self.assertEqual(action.res_model, 'woow.document.mail.intake')
        menu = self.env.ref('woow_document_management.menu_woow_document_mail_intake')
        self.assertEqual(menu.parent_id, self.env.ref('woow_document_management.menu_woow_document_config'))
        self.assertEqual(menu.groups_id, self.env.ref(_MANAGER))
