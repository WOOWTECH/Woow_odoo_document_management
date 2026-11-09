# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Portal HTTP checks for visibility and identical refusal pages."""
import base64
import io
import re
import secrets
import zipfile

from odoo import Command
from odoo.tests import HttpCase, tagged


@tagged('woow_docmgmt_portal_http')
class TestPortalHttp(HttpCase):

    def test_portal_list_download_and_unrelated_record_404(self):
        group = self.env.ref('base.group_portal')
        password = secrets.token_urlsafe(16)
        partner = self.env['res.partner'].sudo().create({'name': 'Reader'})
        user = self.env['res.users'].sudo().create({
            'name': 'Reader', 'login': 'wdoc-portal-http', 'password': password,
            'partner_id': partner.id, 'groups_id': [Command.set([group.id])]})
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Visible folder', 'owner_id': admin.id})
        hidden = self.env['woow.document.folder'].sudo().create({
            'name': 'Hidden folder', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'visible.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'portal-content')})
        direct = self.env['woow.document'].sudo().create({
            'name': 'direct.txt', 'folder_id': hidden.id,
            'datas': base64.b64encode(b'direct-content')})
        direct.sudo().write({'shared_partner_ids': [Command.set([partner.id])]})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'partner',
            'partner_id': partner.id, 'level': 'read'})
        self.authenticate('wdoc-portal-http', password)
        home = self.url_open('/my/docs')
        self.assertEqual(home.status_code, 200, home.text)
        self.assertIn(b'Visible folder', home.content)
        self.assertIn(b'direct.txt', home.content)
        self.assertNotIn(b'Hidden folder', home.content)
        direct_page = self.url_open('/my/docs/file/%d' % direct.id)
        self.assertEqual(direct_page.status_code, 200)
        self.assertNotIn(b'Hidden folder', direct_page.content)
        self.assertNotIn(('/my/docs/folder/%d' % hidden.id).encode(), direct_page.content)
        folder_page = self.url_open('/my/docs/folder/%d' % folder.id)
        self.assertEqual(folder_page.status_code, 200, folder_page.text)
        self.assertIn(b'visible.txt', folder_page.content)
        downloaded = self.url_open('/my/docs/file/%d/download' % doc.id)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b'portal-content')
        archive = self.url_open('/my/docs/folder/%d/zip' % folder.id)
        self.assertEqual(archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            self.assertEqual(zipped.read('Visible folder/visible.txt'), b'portal-content')
        self.env['woow.document'].sudo().create({
            'name': 'another.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'another'),
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.zip_max_files', '1')
        over_limit = self.url_open('/my/docs/folder/%d/zip' % folder.id)
        self.assertEqual(over_limit.status_code, 400, over_limit.text)
        refused = self.url_open('/my/docs/folder/%d' % hidden.id)
        missing = self.url_open('/my/docs/folder/%d' % (hidden.id + 1000000))
        self.assertEqual(refused.status_code, 404)
        self.assertEqual(refused.content, missing.content)

    def test_portal_upload_uses_partner_grant_and_csrf(self):
        password = secrets.token_urlsafe(16)
        partner = self.env['res.partner'].sudo().create({'name': 'Uploader'})
        user = self.env['res.users'].sudo().create({
            'name': 'Uploader', 'login': 'wdoc-portal-uploader', 'password': password,
            'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Upload target', 'owner_id': admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'partner',
            'partner_id': partner.id, 'level': 'upload'})
        self.authenticate(user.login, password)
        page = self.url_open('/my/docs/folder/%d' % folder.id)
        self.assertEqual(page.status_code, 200)
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', page.content)
        self.assertIsNotNone(csrf)
        response = self.url_open('/my/docs/folder/%d/upload' % folder.id,
                                 data={'csrf_token': csrf.group(1).decode()},
                                 files={'files': ('new.txt', b'portal-upload', 'text/plain')})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(self.env['woow.document'].sudo().search_count([
            ('folder_id', '=', folder.id), ('name', '=', 'new.txt')]), 1)
