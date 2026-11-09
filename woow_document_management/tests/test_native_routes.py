# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""Core binary route must enforce document rules before streaming."""

import base64

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
