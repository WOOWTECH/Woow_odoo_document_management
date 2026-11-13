# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..hooks import uninstall_hook
from ..models.ir_attachment import OPEN_ROW_CONSTRAINT


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

    def test_the_constraint_that_closes_the_attachments_is_owned_by_the_module_and_goes_with_it(self):
        """Uninstalling drops what the module added to the table of core: the CHECK of ``ir_attachment``.

        The constraint is reflected for the module (``ir.model.constraint`` and its external id), which
        is what the uninstall reads to drop it. Removing the record, as the uninstall does, drops the
        constraint of the database; the test is rolled back with the rest.
        """
        name = 'ir_attachment_%s' % OPEN_ROW_CONSTRAINT

        def in_database():
            self.env.cr.execute(
                "SELECT 1 FROM pg_constraint WHERE conrelid = 'ir_attachment'::regclass "
                "AND conname = %s", (name,))
            return bool(self.env.cr.fetchone())

        data = self.env['ir.model.data'].sudo().search([
            ('module', '=', 'woow_document_management'), ('model', '=', 'ir.model.constraint'),
            ('name', '=', 'constraint_' + name)])
        self.assertEqual(len(data), 1)
        constraint = self.env['ir.model.constraint'].browse(data.res_id)
        self.assertEqual((constraint.name, constraint.type, constraint.model.model,
                          constraint.module.name),
                         (name, 'u', 'ir.attachment', 'woow_document_management'))
        self.assertTrue(in_database())
        constraint.unlink()
        self.assertFalse(in_database())
