# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The attachments of documents and folders never carry or accept an access token.

Core stores a token on chatter attachments as superuser, for the portal chatter route and for the
links in the mail of a file that is too large to send, and the guard of ``ir.attachment.write``
skips the superuser. A token would then open the file with no session at all, after the reader
lost access and after the document was trashed. The module closes both ends: no token is stored
for an attachment of its records, and none is accepted for one, whoever stored it.
"""
import base64
import io
import re
import secrets

from markupsafe import Markup
from PIL import Image

from odoo import Command
from odoo.addons.mail.tests.common import MockEmail
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools.misc import limited_field_access_token

from ..services.constants import WOOW_MODELS

_USER = 'woow_document_management.group_woow_document_user'


def _png():
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(stream, format='PNG')
    return stream.getvalue()


class _ChatterFixture:
    """A folder with a document, an editor of the folder and a reader of the folder."""

    def _fixture(self):
        users = self.env['res.users'].sudo()
        group = self.env.ref(_USER)
        self.editor = users.create({
            'name': 'Token editor', 'login': 'wdoc-token-editor',
            'email': 'token-editor@example.test', 'groups_id': [Command.set([group.id])]})
        self.reader = users.create({
            'name': 'Token reader', 'login': 'wdoc-token-reader',
            'email': 'token-reader@example.test', 'groups_id': [Command.set([group.id])]})
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Token folder', 'owner_id': self.env.ref('base.user_admin').id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user',
            'user_id': self.editor.id, 'level': 'edit'})
        self._reader_grant()
        self.document = self.env['woow.document'].sudo().create({
            'name': 'token.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'token fixture')})
        self.env.flush_all()

    def _reader_grant(self):
        """The reader's read grant on the folder, made when there is none."""
        grants = self.env['woow.document.folder.access'].sudo()
        grant = grants.search([('folder_id', '=', self.folder.id),
                               ('user_id', '=', self.reader.id)]) or grants.create({
                                   'folder_id': self.folder.id, 'subject_type': 'user',
                                   'user_id': self.reader.id, 'level': 'read'})
        self.env.flush_all()
        return grant

    def _post_file(self, record, name, content, mimetype='text/plain'):
        """The editor's "Send message" comment (mt_comment) with a file, posted as the chatter does.

        The file is uploaded as a pending attachment and core links it to the record as
        superuser when the message is posted. Returns the attachment.
        """
        pending = self.env['ir.attachment'].with_user(self.editor).create({
            'name': name, 'raw': content, 'type': 'binary', 'mimetype': mimetype,
            'res_model': 'mail.compose.message', 'res_id': 0})
        message = record.with_user(self.editor).message_post(
            body='See the file', message_type='comment', subtype_xmlid='mail.mt_comment',
            attachment_ids=[pending.id])
        self.env.flush_all()
        attachment = message.sudo().attachment_ids
        self.assertEqual(attachment, pending.sudo())
        self.assertEqual((attachment.res_model, attachment.res_id), (record._name, record.id))
        return attachment

    def _stored_token(self, attachment):
        """What the database holds in the token column, read around the ORM."""
        self.env.flush_all()
        self.env.cr.execute('SELECT access_token FROM ir_attachment WHERE id = %s', (attachment.id,))
        return self.env.cr.fetchone()[0]


@tagged('woow_docmgmt_http')
class TestChatterAttachmentTokensOverHttp(_ChatterFixture, HttpCase):
    """What a visitor, and the reader, get from the routes that serve a chatter file."""

    def setUp(self):
        super().setUp()
        self._fixture()
        self.records = (self.document, self.folder)

    def _visitor(self, path):
        """The answer to a request that carries no session."""
        self.authenticate(None, None)
        return self.url_open(path, allow_redirects=False)

    def _assert_closed(self, path, content):
        """A visitor gets neither the file nor a response that shared caches may keep."""
        response = self._visitor(path)
        if path.startswith('/web/image'):
            # The image route answers a refused file with its placeholder picture.
            self.assertEqual(response.status_code, 200, path)
        else:
            self.assertEqual(response.status_code, 404, path)
        self.assertNotEqual(response.content, content, path)
        self.assertNotIn('public', response.headers.get('Cache-Control', ''), path)

    def test_the_chatter_route_gives_no_token_and_the_reader_still_downloads_with_the_session(self):
        for record in self.records:
            with self.subTest(model=record._name):
                attachment = self._post_file(record, 'chatter.txt', b'chatter bytes')
                self.authenticate(self.reader.login, self.reader.login)
                result = self.make_jsonrpc_request('/mail/chatter_fetch', {
                    'thread_model': record._name, 'thread_id': record.id})
                sent = {item['id']: item for message in result['data']['mail.message']
                        for item in message['attachment_ids']}
                self.assertEqual(set(sent), {attachment.id})
                self.assertFalse(sent[attachment.id]['access_token'])
                self.assertIsNone(self._stored_token(attachment))
                self.env.invalidate_all()
                self.assertFalse(attachment.access_token)
                download = self.url_open('/web/content/%d?download=1' % attachment.id,
                                         allow_redirects=False)
                self.assertEqual((download.status_code, download.content), (200, b'chatter bytes'))
                # The link that core mails for a file too large to send has an empty token: the
                # session of a reader opens it, and a visitor gets nothing.
                link = '/web/content/%d?download=1&access_token=' % attachment.id
                opened = self.url_open(link, allow_redirects=False)
                self.assertEqual((opened.status_code, opened.content), (200, b'chatter bytes'))
                self._assert_closed(link, b'chatter bytes')

    def test_a_stored_token_does_not_open_the_file_for_a_visitor(self):
        """A value that a superuser wrote opens nothing, with or without ``unique`` (caching)."""
        picture = _png()
        for record in self.records:
            with self.subTest(model=record._name):
                text = self._post_file(record, 'secret.txt', b'secret bytes')
                image = self._post_file(record, 'secret.png', picture, 'image/png')
                token = 'stored-%s' % secrets.token_hex(8)
                (text | image).sudo().write({'access_token': token})
                self.env.flush_all()
                for value in (token, 'any-other-value'):
                    for path in ('/web/content/%d?access_token=%s',
                                 '/web/content/%d?access_token=%s&unique=1',
                                 '/web/content/%d?download=1&access_token=%s',
                                 '/web/content/%d/secret.txt?access_token=%s',
                                 '/web/content?model=ir.attachment&id=%d&field=raw&access_token=%s'):
                        self._assert_closed(path % (text.id, value), b'secret bytes')
                    for path in ('/web/image/%d?access_token=%s',
                                 '/web/image/%d?access_token=%s&unique=1',
                                 '/web/image/%d/secret.png?access_token=%s'):
                        self._assert_closed(path % (image.id, value), picture)

    def test_a_signed_token_for_the_file_does_not_open_it_for_a_visitor(self):
        """The signed token of a record and field (core's other kind) is refused as well."""
        picture = _png()
        for record in self.records:
            with self.subTest(model=record._name):
                text = self._post_file(record, 'signed.txt', b'signed bytes')
                image = self._post_file(record, 'signed.png', picture, 'image/png')
                for path in ('/web/content/%d?access_token=%s',
                             '/web/content/%d?access_token=%s&unique=1'):
                    self._assert_closed(path % (text.id, limited_field_access_token(text, 'raw')),
                                        b'signed bytes')
                self._assert_closed(
                    '/web/image/%d?access_token=%s&unique=1' % (
                        image.id, limited_field_access_token(image, 'raw')), picture)

    def test_the_file_stays_closed_to_a_visitor_when_access_is_removed_or_the_record_is_trashed(self):
        for record in self.records:
            with self.subTest(model=record._name):
                grant = self._reader_grant()
                attachment = self._post_file(record, 'late.txt', b'late bytes')
                token = 'stored-%s' % secrets.token_hex(8)
                attachment.sudo().write({'access_token': token})
                self.env.flush_all()
                path = '/web/content/%d?access_token=%s&unique=1' % (attachment.id, token)
                self._assert_closed(path, b'late bytes')
                self.authenticate(self.reader.login, self.reader.login)
                own = '/web/content/%d?download=1' % attachment.id
                self.assertEqual(self.url_open(own, allow_redirects=False).content, b'late bytes')
                grant.unlink()
                self.env.flush_all()
                self.assertEqual(self.url_open(own, allow_redirects=False).status_code, 404)
                self._assert_closed(path, b'late bytes')
                record.sudo().action_trash()
                self.env.flush_all()
                self._assert_closed(path, b'late bytes')

    def test_files_of_other_records_keep_their_tokens(self):
        """The same requests open a file that belongs to a record of another model."""
        partner = self.env['res.partner'].sudo().create({'name': 'Token control partner'})
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'control.txt', 'raw': b'control bytes', 'type': 'binary',
            'mimetype': 'text/plain', 'res_model': 'res.partner', 'res_id': partner.id})
        tokens = attachment.generate_access_token()
        self.assertEqual(len(tokens), 1)
        self.assertTrue(tokens[0])
        self.assertEqual(self._stored_token(attachment), tokens[0])
        signed = limited_field_access_token(attachment, 'raw')
        for path in ('/web/content/%d?access_token=%s' % (attachment.id, tokens[0]),
                     '/web/content/%d?access_token=%s&unique=1' % (attachment.id, tokens[0]),
                     '/web/content/%d?access_token=%s' % (attachment.id, signed)):
            with self.subTest(path=path):
                response = self._visitor(path)
                self.assertEqual((response.status_code, response.content), (200, b'control bytes'))
        self.assertEqual(self._visitor(
            '/web/content/%d?access_token=any-other-value' % attachment.id).status_code, 404)


@tagged('woow_docmgmt_core')
class TestChatterAttachmentTokenCalls(_ChatterFixture, TransactionCase):
    """The two calls of core that give a token and that take one, made directly."""

    def setUp(self):
        super().setUp()
        self._fixture()

    def _row(self, model, res_id, name='row.txt'):
        return self.env['ir.attachment'].sudo().create({
            'name': name, 'raw': b'row bytes', 'type': 'binary', 'mimetype': 'text/plain',
            'res_model': model, 'res_id': res_id})

    def test_no_token_is_stored_for_the_files_of_the_modules_records(self):
        """Superuser or not, the call returns ``False`` in the place of the token."""
        for model in WOOW_MODELS:
            with self.subTest(model=model):
                row = self._row(model, 0)
                self.assertEqual(row.generate_access_token(), [False])
                self.assertIsNone(self._stored_token(row))
        for record in (self.document, self.folder):
            attachment = self._post_file(record, 'direct.txt', b'direct bytes')
            for env_user in (self.editor, self.reader):
                with self.subTest(model=record._name, user=env_user.login):
                    self.assertEqual(attachment.with_user(env_user).generate_access_token(),
                                     [False])
                    self.assertIsNone(self._stored_token(attachment))
            self.assertEqual(attachment.sudo().generate_access_token(), [False])
            self.assertIsNone(self._stored_token(attachment))

    def test_a_token_stored_by_another_way_is_not_handed_out_either(self):
        """The call never returns a token for the file of a module record, a stored one included."""
        attachment = self._post_file(self.document, 'old.txt', b'old bytes')
        attachment.sudo().write({'access_token': 'stored-by-superuser'})
        self.assertEqual(attachment.generate_access_token(), [False])

    def test_a_mixed_set_gets_a_token_for_the_other_rows_only_and_keeps_the_order(self):
        partner = self.env['res.partner'].sudo().create({'name': 'Token mixed partner'})
        first = self._row('res.partner', partner.id, 'first.txt')
        owned = self._row('woow.document', self.document.id, 'owned.txt')
        last = self._row('res.partner', partner.id, 'last.txt')
        tokens = (first | owned | last).generate_access_token()
        self.assertEqual(len(tokens), 3)
        self.assertTrue(tokens[0] and tokens[2] and tokens[0] != tokens[2])
        self.assertIs(tokens[1], False)
        self.assertEqual(self._stored_token(first), tokens[0])
        self.assertEqual(self._stored_token(last), tokens[2])
        self.assertIsNone(self._stored_token(owned))
        # The same set again: stored tokens are returned as they are, the module's row stays empty.
        self.assertEqual((first | owned | last).generate_access_token(), tokens)
        self.assertEqual(self.env['ir.attachment'].generate_access_token(), [])
        # Rows of other records only: core's own answer.
        self.assertEqual((last | first).generate_access_token(), [tokens[2], tokens[0]])

    def test_the_access_check_refuses_a_token_for_the_files_of_the_modules_records(self):
        """Every caller of ``validate_access`` gets the refusal core gives for a wrong token."""
        partner = self.env['res.partner'].sudo().create({'name': 'Token check partner'})
        other = self._row('res.partner', partner.id)
        other_token = other.generate_access_token()[0]
        owned = self._row('woow.document', self.document.id)
        owned.sudo().write({'access_token': 'stored-by-superuser'})
        public = self.env.ref('base.public_user')
        for user in (public, self.reader, self.editor, self.env.user):
            for token in ('stored-by-superuser', 'any-other-value'):
                with self.subTest(user=user.login, token=token):
                    with self.assertRaises(AccessError) as caught:
                        owned.with_user(user).validate_access(token)
                    self.assertEqual(str(caught.exception), 'Invalid access token')
            # Core's own answers for the file of another record are unchanged.
            with self.subTest(user=user.login, row='other'):
                self.assertEqual(other.with_user(user).validate_access(other_token), other)
                with self.assertRaises(AccessError):
                    other.with_user(user).validate_access('any-other-value')
        # Without a token the call is core's: the file is returned as it was asked for.
        self.assertEqual(owned.with_user(public).validate_access(None), owned)
        self.assertEqual(owned.with_user(public).validate_access(''), owned)

    def test_a_picture_inside_the_message_gets_no_token_and_the_post_goes_through(self):
        """Core stores a token on the file of an embedded picture, as the poster does it."""
        picture = base64.b64encode(_png()).decode()
        for record in (self.document, self.folder):
            with self.subTest(model=record._name):
                message = record.with_user(self.editor).message_post(
                    body=Markup('<p>Look <img src="data:image/png;base64,%s"/></p>') % picture,
                    message_type='comment', subtype_xmlid='mail.mt_comment')
                attachment = message.sudo().attachment_ids
                self.assertEqual(len(attachment), 1)
                self.assertEqual((attachment.res_model, attachment.res_id),
                                 (record._name, record.id))
                self.assertIsNone(self._stored_token(attachment))


@tagged('woow_docmgmt_core')
class TestLargeFilesInMail(_ChatterFixture, TransactionCase, MockEmail):
    """Core mails a link, with the token of the file, in place of a file that is too large."""

    def setUp(self):
        super().setUp()
        self._fixture()
        self.env['ir.config_parameter'].sudo().set_param('base.default_max_email_size', '0.001')

    def _notify(self, thread, record, elevated=False):
        """The editor posts a comment with a file; the reader follows ``thread``."""
        thread.sudo().message_subscribe(partner_ids=[self.reader.partner_id.id])
        pending = self.env['ir.attachment'].with_user(self.editor).create({
            'name': 'large-file.txt', 'raw': b'large bytes ' * 100, 'type': 'binary',
            'mimetype': 'text/plain', 'res_model': 'mail.compose.message', 'res_id': 0})
        poster = thread.with_user(self.editor)
        with self.mock_mail_gateway():
            message = (poster.sudo() if elevated else poster).message_post(
                body='Please read the file', message_type='comment',
                subtype_xmlid='mail.mt_comment', attachment_ids=[pending.id])
        self.env.flush_all()
        attachment = message.sudo().attachment_ids
        self.assertEqual((attachment, attachment.res_model, attachment.res_id),
                         (pending.sudo(), record._name, record.id))
        sent = [mail for mail in self._mails if 'token-reader@example.test' in mail['email_to'][0]]
        self.assertEqual(len(sent), 1)
        return attachment, sent[0]

    @staticmethod
    def _link_tokens(attachment, body):
        """The ``access_token`` values of the download links to ``attachment`` in a mail body."""
        pattern = r'/web/content/%d\?download=1&(?:amp;)?access_token=([^"&\s]*)' % attachment.id
        return re.findall(pattern, body)

    def test_the_mail_of_a_large_file_still_goes_out_and_carries_no_token(self):
        for record in (self.document, self.folder):
            with self.subTest(model=record._name):
                attachment, mail = self._notify(record, record)
                self.assertIsNone(self._stored_token(attachment))
                # The mail is sent, the file is a link in it and not an attachment of it, and the
                # link has no token (nothing says "False" where the token would be).
                self.assertTrue(self._new_mails.filtered(lambda sent: sent.state == 'sent'))
                self.assertFalse(mail['attachments'])
                self.assertIn('large-file.txt', mail['body'])
                self.assertEqual(self._link_tokens(attachment, mail['body']), [''])
                self.assertNotIn('access_token=False', mail['body'])

    def test_the_mail_of_a_large_file_of_another_record_keeps_the_token_link(self):
        partner = self.env['res.partner'].sudo().create({'name': 'Token mail partner'})
        # An employee may not post on a contact: the comment is posted as the editor, elevated.
        attachment, mail = self._notify(partner, partner, elevated=True)
        token = self._stored_token(attachment)
        self.assertTrue(token)
        self.assertFalse(mail['attachments'])
        self.assertEqual(self._link_tokens(attachment, mail['body']), [token])
