# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import base64
import io
import re
import secrets
import zipfile
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, MissingError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import TEST_CURSOR_COOKIE_NAME, JsonRpcException, Opener


@tagged('woow_docmgmt_core')
class TestAttachmentGuard(TransactionCase):

    def test_document_field_cannot_be_forged_or_published(self):
        admin = self.env.ref('base.user_admin')
        root = self.env['woow.document.folder'].sudo().create({'name': 'Attachment root'})
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'Primary image', 'type': 'binary', 'raw': b'file',
            'res_model': 'woow.document.folder', 'res_id': root.id,
            'res_field': 'image_128',
        })
        with self.assertRaises(AccessError):
            attachment.with_user(admin).write({'public': True})
        with self.assertRaises(AccessError):
            attachment.with_user(admin).write({'raw': b'other'})
        with self.assertRaises(AccessError):
            self.env['ir.attachment'].with_user(admin).create({
                'name': 'Forged image', 'type': 'binary', 'raw': b'file',
                'res_model': 'woow.document.folder', 'res_id': root.id,
                'res_field': 'image_128',
            })

    def test_unrelated_attachment_can_be_edited(self):
        admin = self.env.ref('base.user_admin')
        partner = self.env['res.partner'].sudo().create({'name': 'Unrelated target'})
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'Other file', 'type': 'binary', 'raw': b'file',
            'res_model': 'res.partner', 'res_id': partner.id,
        })
        attachment.with_user(admin).write({'name': 'Renamed by admin'})
        self.assertEqual(attachment.name, 'Renamed by admin')
        attachment.with_user(admin).write({'raw': b'other'})
        self.assertEqual(attachment.raw, b'other')

    def test_chatter_attachment_cannot_gain_public_access(self):
        admin = self.env.ref('base.user_admin')
        root = self.env['woow.document.folder'].sudo().create({'name': 'Chatter root'})
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'Discussion file', 'type': 'binary', 'raw': b'file',
            'res_model': 'woow.document.folder', 'res_id': root.id,
        })
        with self.assertRaises(AccessError):
            attachment.with_user(admin).write({'access_token': 'never-public'})
        with self.assertRaises(AccessError):
            attachment.with_user(admin).write({'public': True})

    def test_context_defaults_cannot_forge_document_field_or_public_link(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Restricted attachment user', 'login': 'wdoc-attachment-defaults',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Restricted folder'})
        attachments = self.env['ir.attachment'].with_user(user)
        for defaults in (
            {'default_res_field': 'image_128'},
            {'default_public': True},
            {'default_access_token': 'untrusted'},
        ):
            with self.subTest(defaults=defaults), self.assertRaises(AccessError):
                attachments.with_context(default_res_model='woow.document.folder',
                                         default_res_id=folder.id, **defaults).create({
                                             'name': 'blocked.txt', 'raw': b'blocked',
                                             'type': 'binary', 'mimetype': 'text/plain',
                                         })
        with self.assertRaises(AccessError):
            attachments.with_context(default_res_model='woow.document.folder',
                                     default_res_id=folder.id).create({
                                         'name': 'blocked.txt', 'raw': b'blocked',
                                         'type': 'binary', 'mimetype': 'text/plain',
                                     })


@tagged('woow_docmgmt_core')
class TestDocumentFileAttachment(TransactionCase):
    """A document is only ever served from the attachment it records."""

    def setUp(self):
        super().setUp()
        group = self.env.ref('woow_document_management.group_woow_document_user')
        users = self.env['res.users'].sudo()
        self.editor = users.create({
            'name': 'File editor', 'login': 'wdoc-file-editor', 'email': 'editor@example.test',
            'groups_id': [Command.set([group.id])]})
        self.reader = users.create({
            'name': 'File reader', 'login': 'wdoc-file-reader', 'email': 'reader@example.test',
            'groups_id': [Command.set([group.id])]})
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'File folder', 'owner_id': self.env.ref('base.user_admin').id})
        for user, level in ((self.editor, 'edit'), (self.reader, 'read')):
            self.env['woow.document.folder.access'].sudo().create({
                'folder_id': self.folder.id, 'subject_type': 'user',
                'user_id': user.id, 'level': level})
        self.document = self.env['woow.document'].sudo().create({
            'name': 'original.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'ORIGINAL-BYTES')})
        self.env.flush_all()
        self.original = self.document.sudo().attachment_id
        self.assertTrue(self.original)

    def _file_attachments(self, record=None, field='datas'):
        record = record or self.document
        return self.env['ir.attachment'].sudo().search([
            ('res_model', '=', record._name), ('res_id', '=', record.id),
            ('res_field', '=', field)])

    def _streamed_bytes(self, user):
        """The bytes ``ir.binary`` streams for the document file, as ``user``."""
        stream = self.env['ir.binary'].with_user(user)._record_to_stream(
            self.document.with_user(user), 'datas')
        if stream.type == 'path':
            with open(stream.path, 'rb') as source:
                return source.read()
        self.assertEqual(stream.type, 'data')
        return stream.data

    def _assert_original_file(self):
        self.assertEqual(self._file_attachments(), self.original)
        self.assertEqual(self.document.sudo().attachment_id, self.original)
        self.assertFalse(self._file_attachments(self.folder, 'image_128'))
        self.env.invalidate_all()
        self.assertEqual(base64.b64decode(self.document.with_user(self.reader).datas),
                         b'ORIGINAL-BYTES')

    def _pending(self, user, **vals):
        """A pending chatter attachment of ``user`` that names a binary field."""
        return self.env['ir.attachment'].with_user(user).create(dict({
            'name': 'datas', 'raw': b'FORGED-BYTES', 'type': 'binary',
            'res_model': 'mail.compose.message', 'res_id': 0, 'res_field': 'datas'}, **vals))

    def test_field_attachment_cannot_be_moved_onto_a_document_in_two_steps(self):
        attachments = self.env['ir.attachment'].with_user(self.editor)
        for vals in ({'type': 'binary', 'raw': b'FORGED-BYTES'},
                     {'type': 'url', 'url': 'https://example.invalid/landing'}):
            with self.subTest(kind=vals['type']):
                forged = attachments.create(dict(
                    vals, name='datas', res_model='ir.attachment', res_field='datas'))
                with self.assertRaises(AccessError):
                    forged.write({'res_model': 'woow.document', 'res_id': self.document.id})
                with self.assertRaises(AccessError):
                    forged.write({'res_model': 'woow.document.folder', 'res_id': self.folder.id})
                self._assert_original_file()
        # A plain attachment may still become a chatter attachment of a writable document.
        plain = attachments.create({'name': 'note.txt', 'raw': b'plain', 'type': 'binary',
                                    'res_model': 'mail.compose.message', 'res_id': 0})
        plain.write({'res_model': 'woow.document', 'res_id': self.document.id})
        self.assertEqual((plain.res_model, plain.res_id), ('woow.document', self.document.id))
        self._assert_original_file()

    def test_follower_cannot_post_an_attachment_that_backs_a_field(self):
        """A read-only member may follow and post, but never with a field attachment.

        Core links the poster's pending attachments to the record as superuser, so
        the module guard on attachment writes is not what stops this one.
        """
        document = self.document.with_user(self.reader)
        document.message_subscribe(partner_ids=[self.reader.partner_id.id])
        pending = self._pending(self.reader, raw=b'FORGED-VIA-POST')
        with self.assertRaises(AccessError):
            document.message_post(body='forged', attachment_ids=[pending.id],
                                  message_type='comment', subtype_xmlid='mail.mt_comment')
        self._assert_original_file()
        self.assertEqual(self._streamed_bytes(self.reader), b'ORIGINAL-BYTES')
        plain = self.env['ir.attachment'].with_user(self.reader).create({
            'name': 'reply.txt', 'raw': b'reply', 'type': 'binary',
            'res_model': 'mail.compose.message', 'res_id': 0})
        message = document.message_post(body='reply', attachment_ids=[plain.id],
                                        message_type='comment', subtype_xmlid='mail.mt_comment')
        self.assertEqual(message.sudo().attachment_ids, plain)
        self._assert_original_file()

    def test_notification_on_another_model_cannot_move_a_field_attachment(self):
        """``message_notify`` links pending attachments to any model and id it is given.

        The notification is created on an empty recordset of another model, so the
        module's chatter overrides never run; the attachment guard stops the move
        that core makes as superuser.
        """
        partners = self.env['res.partner'].with_user(self.reader)
        for model, record_id, field in (('woow.document', self.document.id, 'datas'),
                                        ('woow.document.folder', self.folder.id, 'image_128')):
            with self.subTest(model=model):
                pending = self._pending(self.reader, res_field=field)
                with self.assertRaises(AccessError):
                    partners.message_notify(
                        model=model, res_id=record_id, body='notice',
                        partner_ids=[self.reader.partner_id.id], attachment_ids=[pending.id])
                self.assertEqual(pending.sudo().res_model, 'mail.compose.message')
                self._assert_original_file()

    def test_superuser_moves_never_add_a_field_attachment_to_a_module_record(self):
        """The rule holds in sudo: only binary fields themselves create their attachments."""
        other = self.env['woow.document'].sudo().create({
            'name': 'other.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'OTHER-BYTES')})
        self.env.flush_all()
        chatter = self.env['ir.attachment'].sudo().create({
            'name': 'chatter.txt', 'raw': b'chatter', 'type': 'binary',
            'res_model': 'woow.document', 'res_id': self.document.id})
        moves = (
            (self._pending(self.reader).sudo(),
             {'res_model': 'woow.document', 'res_id': self.document.id}),
            (self._pending(self.reader, res_field='image_128').sudo(),
             {'res_model': 'woow.document.folder', 'res_id': self.folder.id}),
            (other.sudo().attachment_id, {'res_id': self.document.id}),
            (chatter, {'res_field': 'datas'}),
            (chatter, {'res_model': 'woow.document', 'res_id': self.document.id,
                       'res_field': 'datas'}),
        )
        for attachment, vals in moves:
            with self.subTest(vals=vals), self.assertRaises(AccessError):
                attachment.write(vals)
        self._assert_original_file()
        self.assertEqual(self._file_attachments(other), other.sudo().attachment_id)
        # Moves that leave no field attachment on a module record are unchanged.
        plain = self.env['ir.attachment'].sudo().create({
            'name': 'plain.txt', 'raw': b'plain', 'type': 'binary',
            'res_model': 'mail.compose.message', 'res_id': 0})
        plain.write({'res_model': 'woow.document', 'res_id': self.document.id})
        self.assertEqual((plain.res_model, plain.res_id), ('woow.document', self.document.id))
        self.original.write({'res_model': 'woow.document', 'res_id': self.document.id})
        self._assert_original_file()

    def test_a_second_file_attachment_is_never_streamed(self):
        """Rows left by any other path never replace the document's own file."""
        for vals in ({'type': 'binary', 'raw': b'FORGED-BYTES'},
                     {'type': 'url', 'url': 'https://example.invalid/landing'}):
            with self.subTest(kind=vals['type']):
                forged = self.env['ir.attachment'].sudo().create(dict(
                    vals, name='datas', res_model='woow.document', res_id=self.document.id,
                    res_field='datas'))
                for user in (self.reader, self.env.ref('base.user_admin')):
                    self.assertEqual(self._streamed_bytes(user), b'ORIGINAL-BYTES')
                forged.unlink()
        self._assert_original_file()
        # The recorded attachment itself must be a stored file, never a URL.
        self.original.write({'type': 'url', 'url': 'https://example.invalid/landing'})
        with self.assertRaises(MissingError):
            self._streamed_bytes(self.reader)

    def test_a_file_kept_in_the_database_streams_from_its_own_row(self):
        """Sites that store attachments in the database stream the recorded row as data."""
        self.env['ir.config_parameter'].sudo().set_param('ir_attachment.location', 'db')
        self.document = self.env['woow.document'].sudo().create({
            'name': 'kept-in-db.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'DATABASE-BYTES')})
        self.env.flush_all()
        recorded = self.document.sudo().attachment_id
        self.assertFalse(recorded.store_fname)
        stream = self.env['ir.binary'].with_user(self.reader)._record_to_stream(
            self.document.with_user(self.reader), 'datas')
        self.assertEqual((stream.type, stream.data, stream.size, stream.etag),
                         ('data', b'DATABASE-BYTES', 14, recorded.checksum))


@tagged('woow_docmgmt_core')
class TestDocumentFileRoutes(HttpCase):
    """Every route that streams a document file serves the attachment it records."""

    def setUp(self):
        super().setUp()
        group = self.env.ref('woow_document_management.group_woow_document_user')
        self.password = secrets.token_urlsafe(16)
        self.reader = self.env['res.users'].sudo().create({
            'name': 'Route reader', 'login': 'wdoc-route-reader', 'password': self.password,
            'email': 'route-reader@example.test', 'groups_id': [Command.set([group.id])]})
        admin = self.env.ref('base.user_admin')
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Route file folder', 'owner_id': admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user',
            'user_id': self.reader.id, 'level': 'read'})
        self.document = self.env['woow.document'].sudo().create({
            'name': 'original.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'ORIGINAL-BYTES')})
        self.share, self.code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': self.folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        self.env.flush_all()
        self.original = self.document.sudo().attachment_id

    def _file_attachments(self):
        return self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document'), ('res_id', '=', self.document.id),
            ('res_field', '=', 'datas')])

    @staticmethod
    def _zip_member(response):
        """(status, the document's bytes in the archive) or (status, body) when refused."""
        if response.status_code != 200:
            return response.status_code, response.content
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = [name for name in archive.namelist() if name.endswith('original.txt')]
            return 200, b''.join(archive.read(name) for name in names)

    def _served_files(self):
        """The bytes every file route returns for the document: (route, status, body)."""
        doc = self.document.id
        self.authenticate(self.reader.login, self.password)
        served = []
        for path in ('/woow_document/doc/%d/download' % doc,
                     '/woow_document/doc/%d/preview' % doc,
                     '/web/content/woow.document/%d/datas' % doc,
                     '/web/content?model=woow.document&id=%d&field=datas&download=1' % doc):
            response = self.url_open(path, allow_redirects=False)
            served.append((path, response.status_code, response.content))
        served.append(('/woow_document/zip', *self._zip_member(
            self.url_open('/woow_document/zip?document_ids=%d' % doc))))
        # A fresh anonymous client: opening a link rotates the session, and a
        # cookie set by authenticate() would shadow the rotated one.
        self.opener = Opener(self.cr)
        self.opener.cookies[TEST_CURSOR_COOKIE_NAME] = self.http_request_key
        landing = self.url_open('/woow-docs/s')
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content).group(1)
        opened = self.url_open('/woow-docs/s/open', data={
            'token': self.code, 'csrf_token': csrf.decode()}, allow_redirects=False)
        self.assertEqual(opened.status_code, 303, opened.text[:200])
        response = self.url_open('/woow-docs/s/%d/file/%d' % (self.share.id, doc),
                                 allow_redirects=False)
        served.append(('/woow-docs/s/file', response.status_code, response.content))
        served.append(('/woow-docs/s/zip', *self._zip_member(
            self.url_open('/woow-docs/s/%d/zip' % self.share.id))))
        return served

    def _assert_original_everywhere(self):
        for path, status, body in self._served_files():
            with self.subTest(path=path):
                self.assertEqual((status, body), (200, b'ORIGINAL-BYTES'))
        self.assertEqual(self._file_attachments(), self.original)

    def _rpc(self, rpc_model, rpc_method, args, **kwargs):
        return self.make_jsonrpc_request('/web/dataset/call_kw/%s/%s' % (rpc_model, rpc_method), {
            'model': rpc_model, 'method': rpc_method, 'args': args, 'kwargs': kwargs})

    def _pending_rpc(self, content):
        """The reader's own pending chatter attachment that names the file field."""
        return self._rpc('ir.attachment', 'create', [{
            'name': 'datas', 'datas': base64.b64encode(content).decode(), 'type': 'binary',
            'res_model': 'mail.compose.message', 'res_id': 0, 'res_field': 'datas'}])

    def test_read_only_follower_post_never_changes_the_served_file(self):
        """A read-only member follows the document and posts a pending field attachment."""
        self.authenticate(self.reader.login, self.password)
        pending = self._pending_rpc(b'FORGED-VIA-POST')
        self._rpc('woow.document', 'message_subscribe', [[self.document.id]],
                  partner_ids=[self.reader.partner_id.id])
        refused = False
        try:
            self._rpc('woow.document', 'message_post', [[self.document.id]],
                      body='forged', attachment_ids=[pending],
                      message_type='comment', subtype_xmlid='mail.mt_comment')
        except JsonRpcException:
            refused = True
        self._assert_original_everywhere()
        self.assertTrue(refused, 'the post linked a field attachment to the document')

    def test_notification_on_another_model_never_changes_the_served_file(self):
        self.authenticate(self.reader.login, self.password)
        pending = self._pending_rpc(b'FORGED-VIA-NOTIFY')
        refused = False
        try:
            self._rpc('res.partner', 'message_notify', [[]], model='woow.document',
                      res_id=self.document.id, body='notice',
                      partner_ids=[self.reader.partner_id.id], attachment_ids=[pending])
        except JsonRpcException:
            refused = True
        self._assert_original_everywhere()
        self.assertTrue(refused, 'the notification linked a field attachment to the document')

    def test_every_route_serves_the_recorded_file_when_another_attachment_claims_it(self):
        """Residual rows (a stored file and a URL) never change what any route serves."""
        for vals in ({'type': 'binary', 'raw': b'FORGED-BYTES'},
                     {'type': 'url', 'url': 'https://example.invalid/landing'}):
            with self.subTest(kind=vals['type']):
                forged = self.env['ir.attachment'].sudo().create(dict(
                    vals, name='datas', res_model='woow.document', res_id=self.document.id,
                    res_field='datas'))
                for path, status, body in self._served_files():
                    with self.subTest(path=path):
                        self.assertEqual((status, body), (200, b'ORIGINAL-BYTES'))
                forged.unlink()
        self._assert_original_everywhere()
