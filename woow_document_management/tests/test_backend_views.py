# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Backend navigation contract checks."""
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_backend')
class TestBackendViews(TransactionCase):

    def test_primary_actions_and_menus_are_installed(self):
        for name, model in (
                ('folder', 'woow.document.folder'),
                ('document', 'woow.document'),
                ('tag', 'woow.document.tag'),
                ('trash', 'woow.document.folder'),
                ('log', 'woow.document.log')):
            action = self.env.ref('woow_document_management.action_woow_document_' + name)
            self.assertEqual(action.res_model, model)
            self.assertTrue(self.env.ref('woow_document_management.menu_woow_document_' + name))
        self.assertEqual(self.env.ref('woow_document_management.menu_woow_document_settings').action.res_model,
                         'res.config.settings')
        deleted = self.env.ref('woow_document_management.action_woow_document_trash_documents')
        self.assertEqual(deleted.res_model, 'woow.document')
        self.assertIn("('active', '=', False)", deleted.domain)
        self.assertTrue(self.env.ref('woow_document_management.menu_woow_document_deleted_files'))

    def test_trash_and_log_are_read_only(self):
        trash = self.env.ref('woow_document_management.view_woow_document_trash_folder_list')
        log = self.env.ref('woow_document_management.view_woow_document_log_list')
        self.assertIn('edit="0"', trash.arch_db)
        self.assertIn('create="0"', log.arch_db)
        self.assertIn('woow_user_rank != 4', trash.arch_db)
