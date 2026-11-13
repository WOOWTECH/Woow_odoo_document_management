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

A token or a field name is judged by what the column keeps: the ORM stores ``str(value)`` for every
value that is not ``None`` or ``False``, so a token of ``0`` or ``[]`` is the text ``'0'`` or
``'[]'`` in the database although the value is not truthy. The superuser's reset does not look at
the values.
"""
import base64
import secrets
import xmlrpc.client
from email.message import EmailMessage
from unittest.mock import patch

from markupsafe import Markup

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import JsonRpcException

from ..models.ir_attachment import IrAttachment
from ..services.constants import WOOW_MODELS
from .test_reg_chatter_attachment_tokens import _ChatterFixture, _png
from .test_reg_server_columns_ignore_defaults import WAYS, _forget, _store

# Values that are not truthy and that a Char column nevertheless keeps as text ('0', '0.0', '[]',
# '{}', '()'): the ORM stores ``str(value)`` for everything that is not None or False.
TYPED_FALSY = (0, 0.0, [], {}, ())
# Values of other types that a Char column keeps as the text they hold: bytes (decoded), the Binary that
# an XML-RPC call delivers for base64 (``str()`` of it is the text), blanks. A record is added by the
# test (``str(record)`` is its representation). Together with ``TYPED_FALSY`` these are the typed
# values of a token or a field name.
TYPED_TEXT = (b'typed', xmlrpc.client.Binary(b'typed'), ' ', '\n')
# Values that a Boolean column keeps as True although they are not ``True``.
TYPED_TRUTHY = (1, 2.5, 'yes', 'false', [0], {'a': 1})


def kept(value):
    """What a Char column keeps for ``value`` (the same rule as the ORM's, written out here)."""
    return value.decode() if isinstance(value, bytes) else str(value)


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

    def _notify_open_file(self, user, record, name, content, mimetype='text/plain'):
        """``user`` sends a notification on ``record`` that links the pending file they made open.

        ``message_notify`` takes any model and id and links the pending files as superuser, as a
        post does. Returns the attachment and the token the poster had put on it.
        """
        pending = self._pending_open(user, name, content, mimetype)
        token = pending.sudo().access_token
        self.assertEqual(self._stored(pending), (True, token))
        message = self.env['res.partner'].with_user(user).message_notify(
            model=record._name, res_id=record.id, body='See the file',
            partner_ids=[user.partner_id.id], attachment_ids=[pending.id])
        self.env.flush_all()
        attachment = message.sudo().attachment_ids
        self.assertEqual(attachment, pending.sudo())
        self.assertEqual((attachment.res_model, attachment.res_id), (record._name, record.id))
        return attachment, token

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

    @staticmethod
    def _shapes(attachment, token, route='content'):
        """The requests that gave the file away while it was public, with and without ``unique``.

        The control (a public file of another model) makes the same requests, so that none of
        them is closed only because the route does not exist.
        """
        base = '/web/%s/%d' % (route, attachment.id)
        paths = [base, base + '?unique=1', base + '/open.bin', base + '/open.bin?unique=1',
                 base + '?access_token=%s' % token, base + '?access_token=%s&unique=1' % token]
        if route == 'content':
            paths += [base + '?download=1', base + '?download=1&unique=1',
                      '/web/content?model=ir.attachment&id=%d&field=raw' % attachment.id]
        return paths

    def _assert_closed_everywhere(self, attachment, token, content, route='content'):
        for path in self._shapes(attachment, token, route):
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
        """The control: the requests of the closed tests, answered for a public file of a contact.

        Every shape is made for the file as it is made for the closed ones, with the token the file
        of another model keeps: the same url that is a 404 above is a 200 here.
        """
        partner = self.env['res.partner'].sudo().create({'name': 'Open control partner'})
        picture = _png()
        files = {}
        for name, content, mimetype in (('open-control.txt', b'control bytes', 'text/plain'),
                                        ('open-control.png', picture, 'image/png')):
            files[name] = self.env['ir.attachment'].sudo().create({
                'name': name, 'raw': content, 'type': 'binary', 'mimetype': mimetype,
                'res_model': 'res.partner', 'res_id': partner.id, 'public': True})
            files[name].generate_access_token()
        self.env.flush_all()
        for name, content, route in (('open-control.txt', b'control bytes', 'content'),
                                     ('open-control.png', picture, 'image'),
                                     ('open-control.png', picture, 'content')):
            attachment = files[name]
            self.assertEqual(self._stored(attachment), (True, attachment.access_token))
            for path in self._shapes(attachment, attachment.access_token, route):
                with self.subTest(path=path, file=name):
                    response = self._visitor(path)
                    self.assertEqual((response.status_code, response.content), (200, content))
                    if 'unique=1' in path:
                        self.assertIn('public', response.headers.get('Cache-Control', ''))

    def test_a_pending_file_that_a_notification_links_is_closed_for_a_visitor(self):
        """``message_notify`` links the pending file of its caller as superuser, as a post does."""
        for record in self.records:
            with self.subTest(model=record._name):
                attachment, token = self._notify_open_file(
                    self.editor, record, 'notice.txt', b'notice bytes')
                self.assertEqual(self._stored(attachment), (False, None))
                self._assert_closed_everywhere(attachment, token, b'notice bytes')
                self._assert_editor_still_reads(attachment, b'notice bytes')


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

    def test_only_a_write_that_names_the_flag_the_token_or_a_module_model_is_looked_at(self):
        """The cost: no row is read for a write that names none of the three, whatever it holds."""
        row = self._module_row('woow.document')
        with patch.object(IrAttachment, '_woow_write_closed', autospec=True,
                          side_effect=IrAttachment._woow_write_closed) as inspected:
            for vals in ({'name': 'renamed.txt'}, {'description': 'd'}, {'res_id': 0},
                         {'res_model': 'res.partner', 'res_id': 0}, {'res_model': False}):
                with self.subTest(vals=vals):
                    row.write(vals)
                    inspected.assert_not_called()
                    row.write({'res_model': 'woow.document', 'res_id': 0})
                    inspected.reset_mock()
            # The values are not judged: a write that names the flag or the token is looked at
            # whatever it holds (``0`` and ``[]`` are kept by the ORM as text).
            for vals in ({'public': True}, {'public': False}, {'public': 0},
                         {'access_token': 'x'}, {'access_token': False}, {'access_token': 0},
                         {'access_token': []}, {'access_token': ''},
                         {'public': 0, 'access_token': ''},
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
        """The raw mail of the editor, who replies to ``parent`` by email with an inline picture.

        Only an internal user who can read the record is accepted as the sender of a reply.
        """
        mail = EmailMessage()
        message_id = '<%s@example.test>' % secrets.token_hex(8)
        mail['From'] = 'Token editor <%s>' % self.editor.email
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

    # -- the files of a post, with a stored default in force ------------------------------------
    def _defaults_in_force(self, way, defaults=None):
        """Put the defaults of ``way`` in force and return the context keys of the 'context' way.

        'personal' is what the editor stored for himself (any internal user may), 'site' what an
        administrator stored for everybody, 'context' the ``default_`` keys of a call. Core makes
        the files of a post as superuser under the uid of the poster, so the poster's own defaults
        and the site's reach that create call. The keys of a context do not: ``sudo()`` takes the
        ``default_`` keys out of the context of a caller who is not the superuser, so they reach
        the create only in a call that was a superuser's already (``_as_the_caller``).
        """
        defaults = defaults or {'public': True, 'access_token': 'default-token'}
        if way == 'context':
            _forget(self.env, 'ir.attachment')
            return {'default_%s' % name: value for name, value in defaults.items()}
        _store(self.env, 'ir.attachment', defaults, user=self.editor if way == 'personal' else None)
        return {}

    def _as_the_caller(self, records, context):
        """``records`` as the editor calls them, with the keys of ``context`` set.

        Keys that are set before ``sudo()`` are dropped by it. The call is made a superuser's
        first when there are keys (a cron or a gateway is one already), and that is the way they
        reach the create.
        """
        records = records.with_user(self.editor)
        return records.sudo().with_context(**context) if context else records

    def _post_a_file(self, record, how, context):
        """The editor posts a file on ``record``: core creates it, or links it, as superuser."""
        poster = self._as_the_caller(record, context)
        if how == 'a file':
            return poster.message_post(
                body='See the file', message_type='comment', subtype_xmlid='mail.mt_comment',
                attachments=[('plain.txt', b'plain bytes')])
        if how == 'a picture':
            return poster.message_post(
                body=Markup('<p>Look <img src="cid:pic"/></p>'), message_type='comment',
                subtype_xmlid='mail.mt_comment',
                attachments=[('inline.png', _png(), {'cid': 'pic'})])
        if how == 'a notification':
            return self._as_the_caller(self.env['res.partner'], context).message_notify(
                model=record._name, res_id=record.id, body='See the file',
                partner_ids=[self.editor.partner_id.id],
                attachments=[('notice.txt', b'notice bytes')])
        # A pending file: the editor makes it (not a row of a module record, so it takes the
        # defaults) and the post links it.
        pending = self.env['ir.attachment'].with_user(self.editor).with_context(**context).create(
            self._values('pending.txt', 'mail.compose.message', 0))
        return poster.message_post(
            body='See the file', message_type='comment', subtype_xmlid='mail.mt_comment',
            attachment_ids=[pending.id])

    def test_a_default_of_any_kind_does_not_open_the_file_of_a_post(self):
        """The defaults reach the create of the file in three ways, and none of them opens it.

        The ones the poster stored and the ones of the site are not keys of the context, so a guard
        that read only ``default_`` keys would leave the file public and tokenised.
        """
        partner = self.env['res.partner'].sudo().create({'name': 'Default control partner'})
        for way in WAYS:
            context = self._defaults_in_force(way)
            with self.subTest(way=way, model='res.partner'):
                # The control: the same post on a contact keeps what the default gave the file, so
                # the default does reach the create call that the guard sees for a module record.
                poster = self._as_the_caller(partner, context).sudo()
                control = poster.message_post(
                    body='See the file', message_type='comment',
                    subtype_xmlid='mail.mt_comment', attachments=[('plain.txt', b'plain bytes')])
                self.env.flush_all()
                self.assertEqual(self._stored(control.sudo().attachment_ids),
                                 (True, 'default-token'))
            for record in (self.document, self.folder):
                for how in ('a file', 'a picture', 'a notification', 'a pending file'):
                    with self.subTest(way=way, model=record._name, how=how):
                        message = self._post_a_file(record, how, context)
                        self.env.flush_all()
                        attachment = message.sudo().attachment_ids
                        self.assertEqual(len(attachment), 1)
                        self.assertEqual((attachment.res_model, attachment.res_id),
                                         (record._name, record.id))
                        self.assertEqual(self._stored(attachment), (False, None))

    def test_a_default_makes_a_caller_who_is_not_the_superuser_refused(self):
        """The same three ways, judged for the caller who stored them: the refusal of today."""
        editor = self.env['ir.attachment'].with_user(self.editor)
        for way in WAYS:
            for name, value in (('public', True), ('access_token', 'default-token')):
                with self.subTest(way=way, default=name):
                    context = self._defaults_in_force(way, {name: value})
                    with self.assertRaises(AccessError):
                        editor.with_context(**context).create(
                            self._values('refused.txt', 'woow.document', self.document.id))
        _forget(self.env, 'ir.attachment')
        row = editor.create(self._values('allowed.txt', 'woow.document', self.document.id))
        self.assertEqual(self._stored(row), (False, None))

    def test_a_pending_file_that_a_notification_links_ends_closed(self):
        for record in (self.document, self.folder):
            with self.subTest(model=record._name):
                attachment, _token = self._notify_open_file(
                    self.editor, record, 'notice.txt', b'notice bytes')
                self.assertEqual(self._stored(attachment), (False, None))

    # -- what the look at the rows costs --------------------------------------------------------
    def _statements(self, rows, vals, cold):
        """The statements that ``write(vals)`` on ``rows`` runs, its UPDATE included.

        ``cold`` empties the cache of the rows first, so that reading ``res_model`` is a SELECT.
        Every call writes a new description: the ORM skips the UPDATE of a value that the cache
        already holds, and the count would otherwise depend on the call before.
        """
        self._runs = getattr(self, '_runs', 0) + 1
        vals = dict(vals, description='run %d' % self._runs)
        self.env.flush_all()
        if cold:
            rows.invalidate_recordset()
        else:
            rows.mapped('res_model')
        before = self.env.cr.sql_log_count
        rows.write(vals)
        self.env.flush_all()
        return self.env.cr.sql_log_count - before

    def test_the_look_at_the_rows_is_one_select_for_the_whole_set(self):
        """A write that only names the flag or the token reads ``res_model`` once; others do not."""
        for size in (1, 5):
            rows = self.attachments.browse([
                self._module_row('woow.document' if index % 2 else 'woow.document.folder').id
                for index in range(size)])
            for name, vals in (('the flag', {'public': True}), ('the token', {'access_token': 'x'}),
                               ('a typed token', {'access_token': 0})):
                with self.subTest(rows=size, vals=name):
                    cold = self._statements(rows, vals, cold=True)
                    warm = self._statements(rows, vals, cold=False)
                    self.assertEqual(cold - warm, 1)
            # The model is named, or nothing of the three: no row is read for the guard, so the
            # cache makes no difference.
            for name, vals in (('the model', {'res_model': 'woow.document', 'res_id': 0}),
                               ('something else', {})):
                with self.subTest(rows=size, vals=name):
                    self.assertEqual(self._statements(rows, vals, cold=True),
                                     self._statements(rows, vals, cold=False))


@tagged('woow_docmgmt_core')
class TestATokenOrAFieldNameIsJudgedByWhatTheColumnKeeps(_OpenFiles, TransactionCase):
    """``0``, ``0.0``, ``[]``, ``{}`` and ``()`` are a token or a field name in the database.

    A Char column keeps ``str(value)`` for every value that is not ``None`` or ``False``, so an
    ``access_token`` or a ``res_field`` of ``0`` is stored as ``'0'`` although ``0`` is not truthy.
    The guard judges both by what the column keeps (a caller who is not the superuser is refused,
    ``False`` and ``None`` pass), and the reset of the superuser does not judge the values at all.
    The control is a row of another model, which keeps what was written.
    """

    def setUp(self):
        super().setUp()
        self._fixture()
        self.attachments = self.env['ir.attachment']
        self.partner = self.env['res.partner'].sudo().create({'name': 'Typed values partner'})
        # The record of each model that a row can be made for: a caller who may write the record
        # (the editor) can make one for the document and the folder, the others have no record.
        self.targets = {'woow.document': self.document.id, 'woow.document.folder': self.folder.id,
                        'woow.document.folder.access': 0, 'woow.document.tag': 0,
                        'woow.document.log': 0}
        self.assertEqual(set(self.targets), set(WOOW_MODELS))

    def _values(self, name, model, res_id, **extra):
        return dict({'name': name, 'raw': b'typed bytes', 'type': 'binary',
                     'mimetype': 'text/plain', 'res_model': model, 'res_id': res_id}, **extra)

    def _columns(self, attachment):
        """(public, access token, field name) as the database holds them, read around the ORM."""
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT COALESCE(public, FALSE), access_token, res_field FROM ir_attachment '
            'WHERE id = %s', (attachment.id,))
        return self.env.cr.fetchone()

    def _pending(self, user, name='pending.txt'):
        """What the composer uploads: a file of ``user`` that belongs to no record yet."""
        return self.attachments.with_user(user).create(self._values(
            name, 'mail.compose.message', 0))

    # -- the superuser --------------------------------------------------------------------------
    def _typed(self):
        """The typed values of a token or a field name: what is falsy, other types of text, a record."""
        return TYPED_FALSY + TYPED_TEXT + (self.partner,)

    def test_the_superuser_stores_no_typed_value_as_a_token_for_a_module_model(self):
        su = self.attachments.sudo()
        for model, res_id in self.targets.items():
            for value in self._typed():
                with self.subTest(model=model, value=value, way='create'):
                    row = su.create(self._values('created.txt', model, res_id, access_token=value))
                    self.assertEqual(self._columns(row), (False, None, None))
                with self.subTest(model=model, value=value, way='write'):
                    row = su.create(self._values('written.txt', model, res_id))
                    row.write({'access_token': value})
                    self.assertEqual(self._columns(row), (False, None, None))

    def test_the_superuser_resets_both_columns_whichever_one_a_write_names(self):
        """A write that names the flag or the token, whatever it holds, closes the rows it hits."""
        su = self.attachments.sudo()
        for model, res_id in self.targets.items():
            for value in self._typed():
                with self.subTest(model=model, value=value, way='the flag with the token'):
                    row = su.create(self._values('both.txt', model, res_id))
                    row.write({'public': True, 'access_token': value})
                    self.assertEqual(self._columns(row), (False, None, None))
                with self.subTest(model=model, value=value, way='a move'):
                    row = su.create(self._values('move.txt', 'res.partner', self.partner.id))
                    row.write({'res_model': model, 'res_id': res_id, 'access_token': value})
                    self.assertEqual(self._columns(row), (False, None, None))
                with self.subTest(model=model, value=value, way='a mixed set'):
                    owned = su.create(self._values('owned.txt', model, res_id))
                    other = su.create(self._values('other.txt', 'res.partner', self.partner.id))
                    (owned | other).write({'access_token': value})
                    self.assertEqual(self._columns(owned), (False, None, None))
                    self.assertEqual(self._columns(other), (False, kept(value), None))
            with self.subTest(model=model, way='a token left by a statement'):
                row = su.create(self._values('stored.txt', model, res_id))
                self._allow_open_rows()
                self.env.cr.execute('UPDATE ir_attachment SET access_token = %s WHERE id = %s',
                                    ('left-by-a-statement', row.id))
                row.invalidate_recordset()
                row.write({'access_token': 0})
                self.assertEqual(self._columns(row), (False, None, None))

    def test_the_superuser_cannot_move_a_row_onto_a_module_record_as_a_field_of_a_typed_name(self):
        su = self.attachments.sudo()
        for model, res_id in self.targets.items():
            for value in self._typed():
                with self.subTest(model=model, value=value):
                    row = su.create(self._values('field.txt', 'mail.compose.message', 0))
                    with self.assertRaises(AccessError):
                        row.write({'res_model': model, 'res_id': res_id, 'res_field': value})
                    self.assertEqual(self._columns(row), (False, None, None))
                    self.assertEqual(row.res_model, 'mail.compose.message')

    def test_a_row_with_a_typed_field_name_is_the_file_of_a_field_for_the_own_defaults(self):
        """The files of binary fields, made as superuser, take the own defaults of such a file.

        A row is one when the column keeps a field name for it, ``'0'`` and ``'[]'`` included. A row
        that backs no field takes the default that the site stored.
        """
        _store(self.env, 'ir.attachment', {'description': 'from a default'})
        su = self.attachments.sudo()
        for value in self._typed():
            with self.subTest(value=value):
                row = su.create(self._values(
                    'field.txt', 'woow.document', self.document.id, res_field=value))
                self.assertEqual((row.res_field, row.description), (kept(value), False))
        # the model of such a row is read as the column keeps it as well: bytes are the model's name
        for model in ('woow.document', 'woow.document.folder'):
            with self.subTest(model=model, way='the model as bytes'):
                row = su.create(self._values(
                    'field.txt', model.encode(), self.targets[model], res_field='datas'))
                self.assertEqual((row.res_model, row.res_field, row.description), (model, 'datas', False))
        row = su.create(self._values('plain.txt', 'woow.document', self.document.id))
        self.assertEqual((row.res_field, row.description), (False, 'from a default'))

    # -- a caller who is not the superuser ------------------------------------------------------
    def test_an_editor_is_refused_a_typed_value_as_a_token_or_a_field_name(self):
        editor = self.attachments.with_user(self.editor)
        for model, res_id in self.targets.items():
            for value in self._typed():
                for key in ('access_token', 'res_field'):
                    with self.subTest(model=model, value=value, key=key, way='create'), \
                            self.assertRaises(AccessError):
                        editor.create(self._values('refused.txt', model, res_id, **{key: value}))

    def test_an_editor_is_refused_a_flag_that_is_not_true_but_is_kept_as_true(self):
        """A Boolean column keeps ``bool(value)``: ``1``, ``'yes'`` and ``[0]`` are True, as ``True`` is."""
        editor = self.attachments.with_user(self.editor)
        for model, res_id in self.targets.items():
            for value in (True,) + TYPED_TRUTHY:
                with self.subTest(model=model, value=value, way='create'), \
                        self.assertRaises(AccessError):
                    editor.create(self._values('refused.txt', model, res_id, public=value))
                with self.subTest(model=model, value=value, way='move'):
                    pending = self._pending(self.editor)
                    with self.assertRaises(AccessError):
                        pending.write({'res_model': model, 'res_id': res_id, 'public': value})
                    self.assertEqual(self._columns(pending), (False, None, None))
                    self.assertEqual(pending.sudo().res_model, 'mail.compose.message')

    def test_the_superuser_closes_a_row_for_a_flag_of_any_type(self):
        su = self.attachments.sudo()
        for model, res_id in self.targets.items():
            for value in (True, False, None) + TYPED_TRUTHY + TYPED_FALSY:
                with self.subTest(model=model, value=value, way='create'):
                    row = su.create(self._values('flag.txt', model, res_id, public=value))
                    self.assertEqual(self._columns(row), (False, None, None))
                with self.subTest(model=model, value=value, way='write'):
                    row = su.create(self._values('flag.txt', model, res_id))
                    row.write({'public': value})
                    self.assertEqual(self._columns(row), (False, None, None))

    def test_an_editor_cannot_move_a_pending_file_with_a_typed_token_or_field_name(self):
        for model, res_id in self.targets.items():
            for value in self._typed():
                for key in ('access_token', 'res_field'):
                    with self.subTest(model=model, value=value, key=key):
                        pending = self._pending(self.editor)
                        with self.assertRaises(AccessError):
                            pending.write({'res_model': model, 'res_id': res_id, key: value})
                        self.assertEqual(self._columns(pending), (False, None, None))
                        self.assertEqual(pending.sudo().res_model, 'mail.compose.message')

    def test_an_editor_still_may_pass_values_that_are_empty(self):
        """``False`` and ``None`` hold nothing in the column, and so does the empty string."""
        editor = self.attachments.with_user(self.editor)
        for value in (False, None):
            with self.subTest(value=value, way='create'):
                row = editor.create(self._values(
                    'empty.txt', 'woow.document', self.document.id,
                    public=value, access_token=value, res_field=value))
                self.assertEqual(self._columns(row), (False, None, None))
            with self.subTest(value=value, way='move'):
                pending = self._pending(self.editor)
                pending.write({'res_model': 'woow.document', 'res_id': self.document.id,
                               'public': value, 'access_token': value, 'res_field': value})
                self.assertEqual((pending.sudo().res_model, self._columns(pending)),
                                 ('woow.document', (False, None, None)))
        with self.subTest(value='', way='create'):
            row = editor.create(self._values('blank.txt', 'woow.document', self.document.id,
                                             access_token='', res_field=''))
            public, token, field = self._columns(row)
            self.assertEqual((public, token or None, field or None), (False, None, None))

    # -- the control ----------------------------------------------------------------------------
    def test_the_same_values_on_a_row_of_another_model_are_left_as_written(self):
        su = self.attachments.sudo()
        for value in self._typed():
            with self.subTest(value=value, way='create'):
                row = su.create(self._values('control.txt', 'res.partner', self.partner.id,
                                             access_token=value, res_field=value))
                self.assertEqual(self._columns(row), (False, kept(value), kept(value)))
            with self.subTest(value=value, way='write'):
                row = su.create(self._values('control.txt', 'res.partner', self.partner.id))
                row.write({'access_token': value, 'res_field': value})
                self.assertEqual(self._columns(row), (False, kept(value), kept(value)))
            with self.subTest(value=value, way='an editor, create and write'):
                # A pending file of a caller who is not the superuser: nothing of the module.
                row = self.attachments.with_user(self.editor).create(
                    self._values('own.txt', 'res.partner', 0, access_token=value))
                self.assertEqual(self._columns(row), (False, kept(value), None))
                pending = self._pending(self.editor)
                pending.write({'access_token': value})
                self.assertEqual(self._columns(pending), (False, kept(value), None))
        for value in (True,) + TYPED_TRUTHY:
            with self.subTest(value=value, way='a flag of a row of another model'):
                row = su.create(self._values('flag.txt', 'res.partner', self.partner.id,
                                             public=value))
                self.assertEqual(self._columns(row)[0], True)


@tagged('woow_docmgmt_http')
class TestATypedTokenSentOverRpcIsRefused(_OpenFiles, HttpCase):
    """The call that the web client makes: ``/web/dataset/call_kw/ir.attachment/create``."""

    def setUp(self):
        super().setUp()
        self._fixture()
        self.authenticate(self.editor.login, self.editor.login)

    def _create(self, **extra):
        values = dict({'name': 'rpc-typed.txt', 'datas': base64.b64encode(b'rpc bytes').decode(),
                       'res_model': 'woow.document', 'res_id': self.document.id}, **extra)
        return self.make_jsonrpc_request('/web/dataset/call_kw/ir.attachment/create', {
            'model': 'ir.attachment', 'method': 'create', 'args': [values], 'kwargs': {}})

    def test_the_call_of_an_editor_that_carries_a_typed_token_is_refused(self):
        # What JSON carries: a tuple is a list on the wire.
        for value in (0, 0.0, [], {}, ' ', 'false'):
            for key in ('access_token', 'res_field'):
                with self.subTest(value=value, key=key):
                    with self.assertRaises(JsonRpcException) as caught:
                        self._create(**{key: value})
                    self.assertEqual(str(caught.exception), 'odoo.exceptions.AccessError')
        self.assertEqual(self.env['ir.attachment'].sudo().search_count(
            [('name', '=', 'rpc-typed.txt')]), 0)
        # The same call with an empty value is accepted, and the row holds neither.
        row = self.env['ir.attachment'].sudo().browse(self._create(access_token=False))
        self.assertEqual((row.res_model, row.res_id), ('woow.document', self.document.id))
        self.assertEqual(self._stored(row), (False, None))
