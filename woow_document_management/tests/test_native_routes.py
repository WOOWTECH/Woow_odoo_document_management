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
