# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Trash transitions and legal hold in a real registry."""
import base64
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models import cron_jobs, trash_actions


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


@tagged('woow_docmgmt_trash')
class TestPurgeBatches(TransactionCase):
    """Permanent deletion survives nested roots, poison records and large subtrees."""

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.folders = self.env['woow.document.folder'].sudo()
        self.documents = self.env['woow.document'].sudo()
        self.old = fields.Datetime.now() - timedelta(days=120)

    @staticmethod
    def _none_exist(*records):
        return not any(record.exists() for record in records)

    @staticmethod
    def _all_exist(*records):
        return all(len(record.exists()) == len(record) for record in records)

    def _doc(self, folder, name):
        return self.documents.create({'name': name, 'folder_id': folder.id,
                                      'datas': base64.b64encode(name.encode())})

    def _expire(self, *records):
        for record in records:
            record.with_context(active_test=False).sudo().write({'trashed_date': self.old})

    def _cron(self):
        progress = []
        with patch.object(type(self.env['ir.cron']), '_notify_progress',
                          lambda cron, **values: progress.append(values)):
            self.documents._cron_purge_trash()
        return progress[-1]

    def _nested(self, parent_name, child_name):
        parent = self.folders.create({'name': parent_name, 'owner_id': self.admin.id})
        child = self.folders.create({'name': child_name, 'parent_id': parent.id,
                                     'owner_id': self.admin.id})
        docs = self._doc(parent, 'in-parent.txt') | self._doc(child, 'in-child.txt')
        loose = self.folders.create({'name': 'Loose holder of %s' % parent_name,
                                     'owner_id': self.admin.id})
        unrelated = self._doc(loose, 'unrelated.txt')
        unrelated.with_user(self.admin).action_trash()
        return parent, child, docs, unrelated

    def test_nested_trashed_roots_are_purged_in_one_run(self):
        for order in ('child-first', 'together'):
            with self.subTest(order=order):
                parent, child, docs, unrelated = self._nested(
                    'A nested parent %s' % order, 'B nested child %s' % order)
                if order == 'child-first':
                    child.with_user(self.admin).action_trash()
                    parent.with_user(self.admin).action_trash()
                else:
                    (parent | child).with_user(self.admin).action_trash()
                self._expire(parent, child, unrelated)
                progress = self._cron()
                self.assertTrue(self._none_exist(parent, child, docs, unrelated))
                self.assertEqual(progress, {'done': 5, 'remaining': 0})

    def test_controls_child_sorting_first_or_expiring_earlier(self):
        parent, child, docs, unrelated = self._nested('Z nested parent', 'A nested child')
        child.with_user(self.admin).action_trash()
        parent.with_user(self.admin).action_trash()
        self._expire(parent, child, unrelated)
        self._cron()
        self.assertTrue(self._none_exist(parent, child, docs, unrelated))
        parent, child, docs, unrelated = self._nested('A2 nested parent', 'B2 nested child')
        child.with_user(self.admin).action_trash()
        self._expire(child)
        self._cron()
        self.assertFalse(child.exists())
        parent.with_user(self.admin).action_trash()
        self._expire(parent, unrelated)
        self._cron()
        self.assertTrue(self._none_exist(parent, docs, unrelated))

    def test_purge_wizard_accepts_a_folder_and_its_trashed_child(self):
        parent, child, docs, _unrelated = self._nested('A wizard parent', 'B wizard child')
        child.with_user(self.admin).action_trash()
        parent.with_user(self.admin).action_trash()
        wizard = self.env['woow.document.purge.wizard'].with_user(self.admin).create({
            'folder_ids': [Command.set([parent.id, child.id])], 'confirm': True})
        self.assertEqual(wizard.action_confirm_purge()['type'], 'ir.actions.act_window_close')
        self.assertTrue(self._none_exist(parent, child, docs))

    def test_purge_poison_record_skipped(self):
        poison = self.folders.create({'name': 'Poison root', 'owner_id': self.admin.id})
        poisoned = self._doc(poison, 'poison.txt')
        healthy = self.folders.create({'name': 'Healthy root', 'owner_id': self.admin.id})
        cleaned = self._doc(healthy, 'healthy.txt')
        (poison | healthy).with_user(self.admin).action_trash()
        self._expire(poison, healthy)
        original = trash_actions.WoowDocumentFolderTrash._woow_purge_tree

        def purge_tree(folder, limit=None):
            if folder.id == poison.id:
                raise UserError('poison record')
            return original(folder, limit)

        with patch.object(trash_actions.WoowDocumentFolderTrash, '_woow_purge_tree', purge_tree):
            progress = self._cron()
        self.assertTrue(self._all_exist(poison, poisoned))
        self.assertTrue(self._none_exist(healthy, cleaned))
        self.assertEqual(progress, {'done': 2, 'remaining': 0})

    def test_large_trashed_subtree_is_purged_in_bounded_batches(self):
        root = self.folders.create({'name': 'Large root', 'owner_id': self.admin.id})
        middle = self.folders.create({'name': 'Large middle', 'parent_id': root.id})
        leaf = self.folders.create({'name': 'Large leaf', 'parent_id': middle.id})
        documents = self.documents
        for index in range(16):
            documents |= self._doc((root, middle, leaf)[index % 3], 'large-%02d.txt' % index)
        single = self._doc(self.folders.create({'name': 'Single holder'}), 'single.txt')
        root.with_user(self.admin).action_trash()
        single.with_user(self.admin).action_trash()
        self._expire(root, single)
        batches = []
        with patch.object(cron_jobs, 'PURGE_BATCH', 5):
            for _run in range(10):
                before = self.documents.with_context(active_test=False).search_count(
                    [('id', 'in', (documents | single).ids)]) + self.folders.with_context(
                    active_test=False).search_count([('id', 'in', (root | middle | leaf).ids)])
                progress = self._cron()
                after = self.documents.with_context(active_test=False).search_count(
                    [('id', 'in', (documents | single).ids)]) + self.folders.with_context(
                    active_test=False).search_count([('id', 'in', (root | middle | leaf).ids)])
                batches.append((before - after, progress['done'], progress['remaining']))
                if not progress['remaining']:
                    break
        self.assertTrue(all(deleted == done <= 5 for deleted, done, _left in batches), batches)
        self.assertEqual(sum(deleted for deleted, _done, _left in batches), 20)
        self.assertEqual(batches[-1][2], 0)
        self.assertTrue(self._none_exist(root, middle, leaf, documents, single))

    def test_legal_hold_protects_the_held_item_and_its_subtree_as_a_whole(self):
        """US-15 AC2: a hold protects the held item and every folder whose subtree holds it.

        Items trashed on their own inside a held folder follow their own hold. If folder
        holds are meant to preserve all content below them, this test changes with it.
        """
        held = self.folders.create({'name': 'Held hold folder', 'owner_id': self.admin.id})
        kept = self._doc(held, 'kept.txt')
        alone = self._doc(held, 'trashed-alone.txt')
        sub = self.folders.create({'name': 'Held hold sub', 'parent_id': held.id,
                                   'owner_id': self.admin.id})
        in_sub = self._doc(sub, 'in-sub.txt')
        held.with_user(self.admin).action_set_legal_hold(True)
        alone.with_user(self.admin).action_trash()
        sub.with_user(self.admin).action_trash()
        self._expire(alone, sub)
        self._cron()
        self.assertTrue(self._all_exist(held, kept))
        self.assertTrue(self._none_exist(alone, sub, in_sub))
        held.with_user(self.admin).action_trash()
        self._expire(held)
        self._cron()
        self.assertTrue(self._all_exist(held, kept))
        wizard = self.env['woow.document.purge.wizard'].with_user(self.admin).create({
            'folder_ids': [Command.set([held.id])], 'confirm': True})
        with self.assertRaises(UserError):
            wizard.action_confirm_purge()
        self.assertTrue(self._all_exist(held, kept))
