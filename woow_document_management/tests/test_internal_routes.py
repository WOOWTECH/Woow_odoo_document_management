# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import base64
import hmac
import io
import time
import zipfile

from odoo import Command
from odoo.tests import HttpCase, tagged

from .common import png_declaring


@tagged('woow_docmgmt_http')
class TestInternalRoutes(HttpCase):

    def setUp(self):
        super().setUp()
        self.folder = self.env['woow.document.folder'].sudo().create({'name': 'HTTP folder'})
        self.doc = self.env['woow.document'].sudo().create({
            'name': 'note.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'route-content'),
        })
        self.authenticate('admin', 'admin')

    def test_download_preview_and_zip_have_bounded_content_and_headers(self):
        download = self.url_open('/woow_document/doc/%d/download' % self.doc.id)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b'route-content')
        self.assertIn('attachment', download.headers['Content-Disposition'])
        self.assertEqual(download.headers['Cache-Control'], 'no-store, private')
        preview = self.url_open('/woow_document/doc/%d/preview' % self.doc.id)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.content, b'route-content')
        self.assertIn('inline', preview.headers['Content-Disposition'])
        archive = self.url_open('/woow_document/zip?document_ids=%d' % self.doc.id)
        self.assertEqual(archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            self.assertEqual(zipped.read('HTTP folder/note.txt'), b'route-content')

    def test_hidden_and_missing_documents_have_the_same_response(self):
        self.doc.write({'active': False})
        hidden = self.url_open('/woow_document/doc/%d/download' % self.doc.id)
        missing = self.url_open('/woow_document/doc/%d/download' % (self.doc.id + 1000000))
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(hidden.content, missing.content)
        self.assertEqual(hidden.headers['Cache-Control'], 'no-store, private')

    def test_folder_zip_refuses_more_than_configured_file_count(self):
        self.env['woow.document'].sudo().create({
            'name': 'second.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'second'),
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.zip_max_files', '1')
        response = self.url_open('/woow_document/zip?folder_ids=%d' % self.folder.id)
        self.assertEqual(response.status_code, 400, response.text)

    def _upload_form(self):
        """The form fields of an upload into the fixture folder, with this session's CSRF token."""
        secret = self.env['ir.config_parameter'].sudo().get_param('database.secret')
        expires = int(time.time() + 3600)
        message = ('%s%d' % (self.session.sid, expires)).encode('utf-8')
        csrf = hmac.new(secret.encode('ascii'), message, 'sha1').hexdigest() + 'o' + str(expires)
        return {'folder_id': str(self.folder.id), 'csrf_token': csrf}

    def test_upload_accepts_text_and_rejects_mixed_batch_atomically(self):
        data = self._upload_form()
        success = self.url_open('/woow_document/upload', data=data, files={
            'files': ('new.txt', b'upload-content', 'text/plain')})
        self.assertEqual(success.status_code, 200, success.text)
        self.assertTrue(success.json()['ok'])
        before = self.env['woow.document'].sudo().search_count([
            ('folder_id', '=', self.folder.id)])
        refused = self.url_open('/woow_document/upload', data=data, files=[
            ('files', ('okay.txt', b'valid', 'text/plain')),
            ('files', ('bad.exe', b'blocked', 'application/octet-stream')),
        ])
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertFalse(refused.json()['ok'])
        self.assertEqual(self.env['woow.document'].sudo().search_count([
            ('folder_id', '=', self.folder.id)]), before)

    def test_upload_refuses_an_image_header_declaring_too_many_pixels(self):
        before = self.env['woow.document'].sudo().search_count([
            ('folder_id', '=', self.folder.id)])
        refused = self.url_open('/woow_document/upload', data=self._upload_form(), files=[
            ('files', ('okay.txt', b'valid', 'text/plain')),
            ('files', ('huge.png', png_declaring(200000, 200000), 'image/png')),
        ])
        self.assertEqual(refused.status_code, 400, refused.text)
        body = refused.json()
        self.assertFalse(body['ok'])
        self.assertEqual([error['code'] for error in body['errors']], ['pixels'])
        self.assertEqual(self.env['woow.document'].sudo().search_count([
            ('folder_id', '=', self.folder.id)]), before)

    def test_unrelated_user_cannot_read_or_zip_a_known_document(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Unrelated fixture', 'login': 'wdoc-unrelated-fixture',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])],
        })
        self.authenticate(user.login, user.login)
        denied = self.url_open('/woow_document/doc/%d/download' % self.doc.id)
        absent = self.url_open('/woow_document/doc/%d/download' % (self.doc.id + 1000000))
        zipped = self.url_open('/woow_document/zip?document_ids=%d' % self.doc.id)
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.content, absent.content)
        self.assertEqual(zipped.status_code, 404)
