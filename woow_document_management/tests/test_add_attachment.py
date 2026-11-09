# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import base64

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_attachment_import')
class TestAddAttachment(TransactionCase):

    def setUp(self):
        super().setUp()
        self.user = self.env.ref('base.user_admin')
        groups = self.env.ref('woow_document_management.group_woow_document_user') | \
            self.env.ref('woow_document_management.group_woow_document_attachment_overview')
        self.user.groups_id = [Command.link(group.id) for group in groups]
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Imported attachment folder', 'owner_id': self.user.id})
        partner = self.env['res.partner'].create({'name': 'Document source'})
        self.source = self.env['ir.attachment'].create({
            'name': 'source.txt', 'res_model': 'res.partner', 'res_id': partner.id,
            'datas': base64.b64encode(b'original-binary')})

    def _wizard(self, ids):
        return self.env['woow.document.add.attachment.wizard'].with_user(self.user).with_context(
            active_model='ir.attachment', active_ids=ids).create({'folder_id': self.folder.id})

    def test_import_reuses_filestore_and_keeps_source_after_document_unlink(self):
        source_name = self.source.store_fname
        self.assertTrue(source_name)
        action = self._wizard([self.source.id]).action_add()
        self.assertEqual(action['params']['type'], 'success')
        doc = self.env['woow.document'].search([
            ('source_res_model', '=', 'res.partner'), ('source_res_id', '=', self.source.res_id)])
        self.assertEqual(len(doc), 1)
        self.assertEqual(doc.attachment_id.store_fname, source_name)
        self.assertEqual(base64.b64decode(doc.datas), b'original-binary')
        doc.sudo().unlink()
        self.assertEqual(self.source.store_fname, source_name)
        self.assertTrue(self.source.exists())

    def test_missing_id_rejects_entire_selection(self):
        with self.assertRaises(AccessError):
            self._wizard([self.source.id, 999999999]).action_add()
        self.assertFalse(self.env['woow.document'].search([
            ('source_res_model', '=', 'res.partner'), ('source_res_id', '=', self.source.res_id)]))

    def test_invalid_attachment_is_skipped_with_reason(self):
        bad = self.env['ir.attachment'].create({
            'name': 'program.exe', 'res_model': 'res.partner',
            'res_id': self.source.res_id, 'datas': base64.b64encode(b'bad-binary')})
        action = self._wizard([self.source.id, bad.id]).action_add()
        self.assertEqual(action['params']['type'], 'warning')
        self.assertIn('1 added; 1 rejected', action['params']['message'])
        self.assertIn('exe', action['params']['message'])
        self.assertEqual(self.env['woow.document'].search_count([
            ('source_res_model', '=', 'res.partner'),
            ('source_res_id', '=', self.source.res_id)]), 1)

    def test_requires_both_document_and_overview_groups(self):
        self.user.groups_id = [Command.unlink(
            self.env.ref('woow_document_management.group_woow_document_attachment_overview').id)]
        with self.assertRaises(AccessError):
            self._wizard([self.source.id]).action_add()
