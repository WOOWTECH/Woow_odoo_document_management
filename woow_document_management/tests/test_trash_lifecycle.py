# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Trash transitions and legal hold in a real registry."""
import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_trash')
class TestTrash(TransactionCase):

    def test_direct_unlink_cannot_skip_trash_or_legal_hold(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Direct delete folder'})
        doc = self.env['woow.document'].sudo().create({
            'name': 'direct.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'direct deletion probe'),
        })
        with self.assertRaises(UserError):
            doc.with_user(admin).unlink()
        self.assertTrue(doc.exists())
        held_leaf = self.env['woow.document.folder'].sudo().create({'name': 'Held leaf'})
        held_leaf.sudo().write({'legal_hold': True})
        with self.assertRaises(UserError):
            held_leaf.with_user(admin).unlink()
        self.assertTrue(held_leaf.exists())

    def test_direct_folder_restore_cannot_skip_restored_timestamp(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Direct restore folder'})
        folder.with_user(admin).action_trash()
        with self.assertRaises(UserError):
            folder.with_user(admin).with_context(active_test=False).write({'active': True})
        self.assertFalse(folder.active)

    def test_folder_trash_hides_descendants_then_restores_them(self):
        admin = self.env.ref('base.user_admin')
        root = self.env['woow.document.folder'].sudo().create({
            'name': 'Root', 'owner_id': admin.id})
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Child', 'parent_id': root.id, 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'note.txt', 'folder_id': child.id,
            'datas': base64.b64encode(b'data')})
        self.assertTrue(root.with_user(admin).action_trash())
        self.assertTrue(child.in_trash)
        self.assertTrue(doc.folder_in_trash)
        self.assertTrue(root.with_user(admin).with_context(active_test=False).action_restore())
        self.assertFalse(child.in_trash)
        self.assertFalse(doc.folder_in_trash)

    def test_document_hold_and_trash_are_audited(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Held', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'note.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'data')})
        self.assertTrue(doc.with_user(admin).action_set_legal_hold(True))
        self.assertTrue(doc.legal_hold)
        self.assertTrue(doc.with_user(admin).action_trash())
        self.assertFalse(doc.active)
        self.assertTrue(doc.with_user(admin).with_context(active_test=False).action_restore())
        events = self.env['woow.document.log'].sudo().search([
            ('document_id', '=', doc.id)])
        self.assertTrue({'legal_hold', 'trash', 'restore'} <= set(events.mapped('event')))

    def test_purge_requires_trash_and_refuses_legal_hold(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Purge fixture', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'note.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'data')})
        self.assertEqual(doc.with_user(admin)._woow_purge(), (0, 1))
        doc.with_user(admin).action_set_legal_hold(True)
        doc.with_user(admin).action_trash()
        self.assertEqual(doc.with_user(admin).with_context(active_test=False)._woow_purge(), (0, 1))
        doc.with_user(admin).action_set_legal_hold(False)
        wizard = self.env['woow.document.purge.wizard'].with_user(admin).create({
            'document_ids': [(6, 0, [doc.id])], 'confirm': True})
        self.assertEqual(wizard.action_confirm_purge()['type'], 'ir.actions.act_window_close')
        self.assertFalse(doc.exists())

    def test_folder_purge_removes_nested_documents(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Nested purge', 'owner_id': admin.id})
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Child', 'parent_id': folder.id, 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'note.txt', 'folder_id': child.id,
            'datas': base64.b64encode(b'data')})
        folder.with_user(admin).action_trash()
        self.assertEqual(folder.with_user(admin).with_context(active_test=False)._woow_purge(),
                         (1, 0))
        self.assertFalse(folder.exists())
        self.assertFalse(child.exists())
        self.assertFalse(doc.exists())

    def test_restore_wizard_moves_document_to_available_folder(self):
        admin = self.env.ref('base.user_admin')
        old = self.env['woow.document.folder'].sudo().create({
            'name': 'Old folder', 'owner_id': admin.id})
        target = self.env['woow.document.folder'].sudo().create({
            'name': 'New folder', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'note.txt', 'folder_id': old.id,
            'datas': base64.b64encode(b'data')})
        doc.with_user(admin).action_trash()
        old.with_user(admin).action_trash()
        wizard = self.env['woow.document.restore.wizard'].with_user(admin).create({
            'document_ids': [(6, 0, [doc.id])], 'target_folder_id': target.id})
        self.assertEqual(wizard.action_restore_to()['type'], 'ir.actions.act_window_close')
        self.assertTrue(doc.active)
        self.assertEqual(doc.folder_id, target)
