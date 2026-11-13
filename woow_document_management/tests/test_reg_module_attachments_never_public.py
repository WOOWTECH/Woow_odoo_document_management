# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The attachments of documents and folders are never public and never carry an access token.

Core makes a file reachable without a session in two ways: the ``public`` flag and an access
token. It also moves the files of a post onto the record as superuser: a poster's pending file is
re-parented with a superuser write (``mail.thread._process_attachments_for_post``), whatever the
poster put on it, and a file that the body of the post refers to is created there with a token that
core has just made. The guard of ``ir.attachment`` refuses the flag and the token to a caller who
is not the superuser; for the superuser it sets both to ``False`` in ``create`` and in ``write``.
So no row of a record of the module holds either after any create or write, in any environment, and
no file of the module is shown to a visitor, kept by a shared cache, or left open once the access
is withdrawn or the record is in the trash.
"""
import base64
import secrets
from email.message import EmailMessage
from unittest.mock import patch

from markupsafe import Markup

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.ir_attachment import IrAttachment
from ..services.constants import WOOW_MODELS
from .test_reg_chatter_attachment_tokens import _ChatterFixture, _png


class _OpenFiles(_ChatterFixture):
    """What a poster can do to make a file open, and how the database holds the result."""

    def _stored(self, attachment):
        """(public, access token) as the database holds them, read around the ORM."""
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT COALESCE(public, FALSE), access_token FROM ir_attachment WHERE id = %s',
            (attachment.id,))
        return self.env.cr.fetchone()

    def _pending_open(self, user, name, content, mimetype='text/plain'):
        """The pending file of ``user`` with the flag and a token that ``user`` put on it.

        Any internal user can create an attachment with both, through the ORM: the composer of the
        web client sets neither, nothing else stops a poster from doing it.
        """
        return self.env['ir.attachment'].with_user(user).create({
            'name': name, 'raw': content, 'type': 'binary', 'mimetype': mimetype,
            'res_model': 'mail.compose.message', 'res_id': 0,
            'public': True, 'access_token': 'pending-%s' % secrets.token_hex(8)})

    def _post_open_file(self, user, record, name, content, mimetype='text/plain'):
        """``user`` posts a comment with a file that is public and tokenised before the post.

        Returns the attachment and the token the poster had put on it.
        """
        pending = self._pending_open(user, name, content, mimetype)
        token = pending.sudo().access_token
        self.assertEqual(self._stored(pending), (True, token))
        message = record.with_user(user).message_post(
            body='See the file', message_type='comment', subtype_xmlid='mail.mt_comment',
            attachment_ids=[pending.id])
        self.env.flush_all()
        attachment = message.sudo().attachment_ids
        self.assertEqual(attachment, pending.sudo())
        self.assertEqual((attachment.res_model, attachment.res_id), (record._name, record.id))
        return attachment, token


@tagged('woow_docmgmt_http')
class TestAFileOfAModuleRecordNeverOpensForAVisitor(_OpenFiles, HttpCase):
    """What a visitor without a session gets for the file of a post whose poster opened it."""

    def setUp(self):
        super().setUp()
        self._fixture()
        self.records = (self.document, self.folder)

    def _visitor(self, path):
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

    def _assert_closed_everywhere(self, attachment, token, content, route='content'):
        """The requests that gave the file away while it was public, with and without ``unique``."""
        base = '/web/%s/%d' % (route, attachment.id)
        paths = [base, base + '?unique=1', base + '/open.bin', base + '/open.bin?unique=1',
                 base + '?access_token=%s' % token, base + '?access_token=%s&unique=1' % token]
        if route == 'content':
            paths += [base + '?download=1', base + '?download=1&unique=1',
                      '/web/content?model=ir.attachment&id=%d&field=raw' % attachment.id]
        for path in paths:
            self._assert_closed(path, content)

    def _assert_editor_still_reads(self, attachment, content):
        self.authenticate(self.editor.login, self.editor.login)
        download = self.url_open('/web/content/%d?download=1' % attachment.id,
                                 allow_redirects=False)
        self.assertEqual((download.status_code, download.content), (200, content))

    def test_a_file_that_the_poster_made_public_and_tokenised_is_closed_by_the_post(self):
        picture = _png()
        for record in self.records:
            with self.subTest(model=record._name):
                text, text_token = self._post_open_file(
                    self.editor, record, 'open.txt', b'open bytes')
                image, image_token = self._post_open_file(
                    self.editor, record, 'open.png', picture, 'image/png')
                self.assertEqual(self._stored(text), (False, None))
                self.assertEqual(self._stored(image), (False, None))
                self._assert_closed_everywhere(text, text_token, b'open bytes')
                self._assert_closed_everywhere(image, image_token, picture, route='image')
                self._assert_closed_everywhere(image, image_token, picture)
                self._assert_editor_still_reads(text, b'open bytes')

    def test_a_read_only_member_who_follows_the_record_can_post_and_the_file_is_closed_too(self):
        """The module lets a follower with the right to read only post a comment with a file."""
        picture = _png()
        for record in self.records:
            with self.subTest(model=record._name):
                record.sudo().message_subscribe(partner_ids=[self.reader.partner_id.id])
                self.assertFalse(record.with_user(self.reader).has_access('write'))
                text, text_token = self._post_open_file(
                    self.reader, record, 'reader.txt', b'reader bytes')
                image, image_token = self._post_open_file(
                    self.reader, record, 'reader.png', picture, 'image/png')
                self.assertEqual(self._stored(text), (False, None))
                self.assertEqual(self._stored(image), (False, None))
                self._assert_closed_everywhere(text, text_token, b'reader bytes')
                self._assert_closed_everywhere(image, image_token, picture, route='image')

    def _assert_stays_closed(self, record):
        """The file stays closed to a visitor when the grants are removed and the record is trashed."""
        attachment, token = self._post_open_file(self.editor, record, 'late.txt', b'late bytes')
        self._assert_closed_everywhere(attachment, token, b'late bytes')
        self.env['woow.document.folder.access'].sudo().search(
            [('folder_id', '=', self.folder.id)]).unlink()
        self.env.flush_all()
        self._assert_closed_everywhere(attachment, token, b'late bytes')
        record.sudo().action_trash()
        self.env.flush_all()
        self._assert_closed_everywhere(attachment, token, b'late bytes')
        # The one who had the grant gets nothing either: the record rules decide for a session.
        self.authenticate(self.editor.login, self.editor.login)
        self.assertEqual(self.url_open('/web/content/%d?download=1' % attachment.id,
                                       allow_redirects=False).status_code, 404)

    def test_the_file_of_a_document_stays_closed_when_the_grants_go_and_it_is_trashed(self):
        self._assert_stays_closed(self.document)

    def test_the_file_of_a_folder_stays_closed_when_the_grants_go_and_it_is_trashed(self):
        self._assert_stays_closed(self.folder)

    def test_the_url_of_an_inline_picture_in_a_post_opens_nothing_for_a_visitor(self):
        """Core puts the token it made into the body, the row does not keep it."""
        picture = _png()
        for record in self.records:
            with self.subTest(model=record._name):
                message = record.with_user(self.editor).message_post(
                    body=Markup('<p>Look <img src="cid:pic"/></p>'), message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                    attachments=[('inline.png', picture, {'cid': 'pic'})])
                self.env.flush_all()
                attachment = message.sudo().attachment_ids
                self.assertEqual(self._stored(attachment), (False, None))
                body = str(message.body)
                self.assertIn('/web/image/%d?access_token=' % attachment.id, body)
                token = body.split('access_token=')[1].split('"')[0]
                self.assertTrue(token)
                self._assert_closed_everywhere(attachment, token, picture, route='image')

    def test_a_picture_that_the_html_editor_copies_onto_the_record_is_closed(self):
        """The editor of the html editor copies a public, tokenised picture onto the record.

        Its route makes the copy as superuser and the copy keeps the flag and the token of the
        original unless something takes them off. The answer holds a url that opens nothing.
        """
        partner = self.env['res.partner'].sudo().create({'name': 'Html editor partner'})
        picture = _png()
        source = self.env['ir.attachment'].sudo().create({
            'name': 'source.png', 'raw': picture, 'type': 'binary', 'mimetype': 'image/png',
            'res_model': 'res.partner', 'res_id': partner.id, 'public': True,
            'access_token': 'source-token'})
        self.env.flush_all()
        for record in self.records:
            with self.subTest(model=record._name):
                self.authenticate(self.editor.login, self.editor.login)
                url = self.make_jsonrpc_request(
                    '/html_editor/modify_image/%d' % source.id,
                    {'res_model': record._name, 'res_id': record.id, 'name': 'edited-%s.png' % record.id,
                     'data': base64.b64encode(picture).decode(), 'mimetype': 'image/png'})
                copy = self.env['ir.attachment'].sudo().search([
                    ('original_id', '=', source.id), ('res_model', '=', record._name),
                    ('res_id', '=', record.id)])
                self.assertEqual(len(copy), 1)
                self.assertEqual(self._stored(copy), (False, None))
                self.assertTrue(url.endswith('?access_token=False'), url)
                self._assert_closed_everywhere(copy, 'False', picture, route='image')
        self.assertEqual(self._stored(source), (True, 'source-token'))

    def test_a_public_file_of_another_model_stays_open_to_visitors(self):
        """The control: the same requests are answered for a public file of a contact."""
        partner = self.env['res.partner'].sudo().create({'name': 'Open control partner'})
        picture = _png()
        files = {}
        for name, content, mimetype in (('open-control.txt', b'control bytes', 'text/plain'),
                                        ('open-control.png', picture, 'image/png')):
            files[name] = self.env['ir.attachment'].sudo().create({
                'name': name, 'raw': content, 'type': 'binary', 'mimetype': mimetype,
                'res_model': 'res.partner', 'res_id': partner.id, 'public': True})
        self.env.flush_all()
        for name, content in (('open-control.txt', b'control bytes'),
                              ('open-control.png', picture)):
            attachment = files[name]
            for path in ('/web/content/%d', '/web/content/%d?unique=1',
                         '/web/content/%d?download=1&unique=1'):
                with self.subTest(path=path, file=name):
                    response = self._visitor(path % attachment.id)
                    self.assertEqual((response.status_code, response.content), (200, content))
                    if 'unique=1' in path:
                        self.assertIn('public', response.headers.get('Cache-Control', ''))
        response = self._visitor('/web/image/%d?unique=1' % files['open-control.png'].id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, picture)


@tagged('woow_docmgmt_core')
class TestTheSuperuserNeverMakesTheFilesOfAModuleRecordOpen(_OpenFiles, TransactionCase):
    """``create`` and ``write`` of ``ir.attachment`` in superuser mode, called directly."""

    def setUp(self):
        super().setUp()
        self._fixture()
        self.attachments = self.env['ir.attachment'].sudo()
        self.partner = self.env['res.partner'].sudo().create({'name': 'Open rows partner'})

    def _values(self, name, model, res_id, **extra):
        return dict({'name': name, 'raw': b'row bytes', 'type': 'binary',
                     'mimetype': 'text/plain', 'res_model': model, 'res_id': res_id}, **extra)

    def _open_row(self, name='open.txt'):
        """A row of another model that is public and tokenised, as core and other modules make them."""
        return self.attachments.create(self._values(
            name, 'res.partner', self.partner.id, public=True,
            access_token='open-%s' % secrets.token_hex(8)))

    def _module_row(self, model, name='owned.txt'):
        return self.attachments.create(self._values(name, model, 0))

    # -- create ---------------------------------------------------------------------------------
    def test_a_create_for_a_module_model_drops_the_flag_and_the_token(self):
        for model in WOOW_MODELS:
            with self.subTest(model=model, way='values'):
                row = self.attachments.create(self._values(
                    'values.txt', model, 0, public=True, access_token='create-token'))
                self.assertEqual(self._stored(row), (False, None))
            with self.subTest(model=model, way='the flag only'):
                row = self.attachments.create(self._values('flag.txt', model, 0, public=True))
                self.assertEqual(self._stored(row), (False, None))
            with self.subTest(model=model, way='the token only'):
                row = self.attachments.create(self._values(
                    'token.txt', model, 0, access_token='create-token'))
                self.assertEqual(self._stored(row), (False, None))
            with self.subTest(model=model, way='context defaults'):
                row = self.attachments.with_context(
                    default_res_model=model, default_res_id=0, default_public=True,
                    default_access_token='default-token').create(
                        {'name': 'context.txt', 'raw': b'row bytes', 'type': 'binary'})
                self.assertEqual((row.res_model, self._stored(row)), (model, (False, None)))
            with self.subTest(model=model, way='defaults for the flag, the model in the values'):
                row = self.attachments.with_context(
                    default_public=True, default_access_token='default-token').create(
                        self._values('mixed.txt', model, 0))
                self.assertEqual(self._stored(row), (False, None))

    def test_a_create_for_another_model_is_left_alone(self):
        row = self._open_row()
        self.assertEqual(self._stored(row), (True, row.access_token))
        row = self.attachments.with_context(
            default_public=True, default_access_token='default-token').create(
                self._values('context.txt', 'res.partner', self.partner.id))
        self.assertEqual(self._stored(row), (True, 'default-token'))
        row = self.attachments.create({'name': 'free.txt', 'raw': b'free', 'public': True})
        self.assertEqual(self._stored(row), (True, None))

    def test_a_batch_create_changes_only_the_rows_of_module_models(self):
        rows = self.attachments.create([
            self._values('other.txt', 'res.partner', self.partner.id, public=True,
                         access_token='batch-token-1'),
            self._values('document.txt', 'woow.document', self.document.id, public=True,
                         access_token='batch-token-2'),
            self._values('plain.txt', 'woow.document.folder', self.folder.id),
            self._values('folder.txt', 'woow.document.folder', self.folder.id, public=True)])
        self.assertEqual([self._stored(row) for row in rows],
                         [(True, 'batch-token-1'), (False, None), (False, None), (False, None)])
        self.assertEqual(rows.mapped('name'),
                         ['other.txt', 'document.txt', 'plain.txt', 'folder.txt'])

    def test_a_caller_who_is_not_the_superuser_is_still_refused(self):
        """Today's answer is not changed: the refusal, not a silent reset."""
        for user in (self.editor, self.reader):
            for model in WOOW_MODELS:
                for extra in ({'public': True}, {'access_token': 'refused'}):
                    with self.subTest(user=user.login, model=model, extra=extra), \
                            self.assertRaises(AccessError):
                        self.env['ir.attachment'].with_user(user).create(
                            self._values('refused.txt', model, 0, **extra))
        row = self._open_row()
        with self.assertRaises(AccessError):
            row.with_user(self.editor).copy({'res_model': 'woow.document',
                                              'res_id': self.document.id})

    def test_a_copy_of_an_open_row_onto_a_module_record_is_closed(self):
        """The editor of the html editor copies an existing image onto the record as superuser."""
        source = self._open_row()
        for model, res_id in (('woow.document', self.document.id),
                              ('woow.document.folder', self.folder.id)):
            with self.subTest(model=model):
                copy = source.copy({'res_model': model, 'res_id': res_id})
                self.assertEqual(self._stored(copy), (False, None))
        # A copy that stays with the other model keeps what the original has.
        again = source.copy({})
        self.assertEqual(self._stored(again), (True, source.access_token))

    # -- write ----------------------------------------------------------------------------------
    def test_a_write_that_moves_an_open_row_onto_a_module_model_resets_both(self):
        for model in WOOW_MODELS:
            for extra in ({}, {'public': True, 'access_token': 'moved-token'}):
                with self.subTest(model=model, extra=extra):
                    row = self._open_row()
                    self.assertEqual(self._stored(row), (True, row.access_token))
                    row.write(dict(extra, res_model=model, res_id=0))
                    self.assertEqual((row.res_model, self._stored(row)), (model, (False, None)))
        with self.subTest(way='the model only'):
            row = self._open_row()
            row.write({'res_model': 'woow.document'})
            self.assertEqual(self._stored(row), (False, None))

    def test_a_write_that_moves_several_open_rows_resets_all_of_them(self):
        first, second = self._open_row('first.txt'), self._open_row('second.txt')
        other = self.attachments.create(self._values(
            'third.txt', 'res.partner', self.partner.id, public=True, access_token='third-token'))
        (first | second).write({'res_model': 'woow.document', 'res_id': self.document.id,
                                'description': 'moved'})
        self.assertEqual([self._stored(row) for row in (first, second)],
                         [(False, None), (False, None)])
        self.assertEqual((first | second).mapped('description'), ['moved', 'moved'])
        self.assertEqual(self._stored(other), (True, 'third-token'))
        # Moved away from the module again, a row is an ordinary row: it may be public once more.
        first.write({'res_model': 'res.partner', 'res_id': self.partner.id, 'public': True})
        self.assertEqual(self._stored(first), (True, None))

    def test_a_write_of_the_flag_or_a_token_leaves_the_rows_of_module_models_closed(self):
        for model in WOOW_MODELS:
            row = self._module_row(model)
            for vals in ({'public': True}, {'access_token': 'written-token'},
                         {'public': True, 'access_token': 'written-token'},
                         {'public': True, 'description': 'kept'}):
                with self.subTest(model=model, vals=vals):
                    row.write(vals)
                    self.assertEqual(self._stored(row), (False, None))
            self.assertEqual(row.description, 'kept')

    def test_a_mixed_write_changes_only_what_it_must(self):
        other = self._open_row()
        owned = self._module_row('woow.document')
        folder_owned = self._module_row('woow.document.folder')
        plain = self.attachments.create(self._values('plain.txt', 'res.partner', self.partner.id))
        rows = other | owned | plain | folder_owned
        rows.write({'public': True, 'access_token': 'mixed-token', 'description': 'mixed'})
        self.assertEqual([self._stored(row) for row in rows],
                         [(True, 'mixed-token'), (False, None), (True, 'mixed-token'),
                          (False, None)])
        self.assertEqual(rows.mapped('description'), ['mixed'] * 4)
        # The other order of the set, and a write that names only the token.
        (owned | plain | other | folder_owned).write({'access_token': 'second-token'})
        self.assertEqual([self._stored(row) for row in rows],
                         [(True, 'second-token'), (False, None), (True, 'second-token'),
                          (False, None)])
        # A write that sets neither is core's, for every row.
        rows.write({'description': 'last'})
        self.assertEqual(rows.mapped('description'), ['last'] * 4)
        self.assertEqual([self._stored(row) for row in rows],
                         [(True, 'second-token'), (False, None), (True, 'second-token'),
                          (False, None)])
        # Taking the flag and the token off is allowed for every row.
        rows.write({'public': False, 'access_token': False})
        self.assertEqual([self._stored(row) for row in rows], [(False, None)] * 4)

    def test_only_a_write_that_can_open_a_row_is_looked_at(self):
        """The cost: no row is read for a write that cannot set the flag or a token."""
        row = self._module_row('woow.document')
        with patch.object(IrAttachment, '_woow_write_closed', autospec=True,
                          side_effect=IrAttachment._woow_write_closed) as inspected:
            for vals in ({'name': 'renamed.txt'}, {'description': 'd'}, {'public': False},
                         {'access_token': False}, {'public': 0, 'access_token': ''},
                         {'res_id': 0}, {'res_model': 'res.partner', 'res_id': 0},
                         {'res_model': False}):
                with self.subTest(vals=vals):
                    row.write(vals)
                    inspected.assert_not_called()
                    row.write({'res_model': 'woow.document', 'res_id': 0})
                    inspected.reset_mock()
            for vals in ({'public': True}, {'access_token': 'x'},
                         {'res_model': 'woow.document', 'res_id': 0}):
                with self.subTest(vals=vals):
                    row.write(vals)
                    self.assertEqual(inspected.call_count, 1)
                    inspected.reset_mock()
            # Not the superuser: the guard raises or the write is core's, never the superuser's.
            own_row = self.env['ir.attachment'].with_user(self.editor).create(
                {'name': 'own.txt', 'raw': b'own bytes', 'public': True})
            own_row.write({'public': True})
            inspected.assert_not_called()

    # -- the files of a post --------------------------------------------------------------------
    def _post_with_picture(self, record, way):
        picture = _png()
        poster = record.with_user(self.editor)
        if way == 'a cid':
            return poster.message_post(
                body=Markup('<p>Look <img src="cid:pic"/></p>'), message_type='comment',
                subtype_xmlid='mail.mt_comment',
                attachments=[('inline.png', picture, {'cid': 'pic'})])
        if way == 'a file name':
            return poster.message_post(
                body=Markup('<p>Look <img data-filename="inline.png" src="x"/></p>'),
                message_type='comment', subtype_xmlid='mail.mt_comment',
                attachments=[('inline.png', picture)])
        return self.env['res.partner'].with_user(self.editor).message_notify(
            model=record._name, res_id=record.id, body=Markup('<p>Look <img src="cid:pic"/></p>'),
            partner_ids=[self.editor.partner_id.id],
            attachments=[('inline.png', picture, {'cid': 'pic'})])

    def test_a_post_that_refers_to_a_file_in_its_body_stores_no_token_for_it(self):
        for record in (self.document, self.folder):
            for way in ('a cid', 'a file name', 'a notification'):
                with self.subTest(model=record._name, way=way):
                    message = self._post_with_picture(record, way)
                    self.env.flush_all()
                    attachment = message.sudo().attachment_ids
                    self.assertEqual(len(attachment), 1)
                    self.assertEqual((attachment.res_model, attachment.res_id),
                                     (record._name, record.id))
                    self.assertEqual(self._stored(attachment), (False, None))

    def _inbound_reply(self, parent, picture):
        """The raw mail of an outside sender who replies to ``parent`` with an inline picture."""
        mail = EmailMessage()
        message_id = '<%s@example.test>' % secrets.token_hex(8)
        mail['From'] = 'Outside sender <outside-sender@example.test>'
        mail['To'] = 'nobody@example.test'
        mail['Subject'] = 'Re: a file'
        mail['Message-Id'] = message_id
        mail['In-Reply-To'] = parent.message_id
        mail['References'] = parent.message_id
        mail.set_content('Look at the picture')
        mail.add_alternative('<p>Look at <img src="cid:inline-pic"/></p>', subtype='html')
        mail.get_payload()[1].add_related(
            picture, 'image', 'png', cid='<inline-pic>', filename='inline.png')
        return message_id, mail.as_bytes()

    def test_an_inbound_reply_with_an_inline_picture_stores_no_token_for_it(self):
        picture = _png()
        gateway = self.env['mail.thread'].sudo()
        for record in (self.document, self.folder):
            with self.subTest(model=record._name):
                parent = record.with_user(self.editor).message_post(
                    body='A question', message_type='comment', subtype_xmlid='mail.mt_comment')
                message_id, raw = self._inbound_reply(parent, picture)
                self.assertEqual(
                    gateway.message_process(record._name, raw, thread_id=record.id), record.id)
                self.env.flush_all()
                reply = self.env['mail.message'].sudo().search([('message_id', '=', message_id)])
                self.assertEqual((len(reply), reply.model, reply.res_id),
                                 (1, record._name, record.id))
                attachment = reply.attachment_ids
                self.assertEqual(len(attachment), 1)
                self.assertEqual((attachment.res_model, attachment.res_id, attachment.name),
                                 (record._name, record.id, 'inline.png'))
                self.assertEqual(self._stored(attachment), (False, None))
                self.assertIn('/web/image/%d?access_token=' % attachment.id, str(reply.body))
