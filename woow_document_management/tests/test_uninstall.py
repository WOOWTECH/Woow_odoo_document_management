# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..hooks import uninstall_hook


@tagged('woow_docmgmt_core')
class TestUninstall(TransactionCase):

    def setUp(self):
        super().setUp()
        self.folder = self.env['woow.document.folder'].sudo().create({'name': 'Delete fixture'})
        self.document = self.env['woow.document'].sudo().create({
            'name': 'delete.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'delete fixture'),
        })

    def test_legal_hold_blocks_even_with_confirmation(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_document_management.uninstall_confirm',
                         params.get_param('database.uuid'))
        self.document.write({'legal_hold': True})
        with self.assertRaises(UserError):
            uninstall_hook(self.env)
        self.assertTrue(self.document.exists())

    def test_unconfirmed_documents_block_uninstall(self):
        with self.assertRaises(UserError):
            uninstall_hook(self.env)
        self.assertTrue(self.document.exists())

    def test_confirmed_uninstall_removes_documents_and_attachments(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_document_management.uninstall_confirm',
                         params.get_param('database.uuid'))
        attachment_id = self.document.attachment_id.id
        self.assertTrue(attachment_id)
        uninstall_hook(self.env)
        self.assertFalse(self.document.exists())
        self.assertFalse(self.folder.exists())
        self.assertFalse(self.env['ir.attachment'].sudo().browse(attachment_id).exists())
        self.assertFalse(params.get_param('woow_document_management.uninstall_confirm'))
