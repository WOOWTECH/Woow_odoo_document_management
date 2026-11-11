# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Trash transitions and legal hold in a real registry."""
import base64
import secrets
from datetime import timedelta
from unittest.mock import patch

from psycopg2 import errors

from odoo import Command, SUPERUSER_ID, api, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

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

    def test_concurrency_failures_skip_only_their_record(self):
        """A deadlock or serialization failure of one root or document never fails the run.

        Both errors leave the transaction usable once the record's savepoint is rolled
        back; the record counts as failed and the other records are still purged.
        """
        roots, documents = set(), set()
        purge_tree = trash_actions.WoowDocumentFolderTrash._woow_purge_tree
        unlink = type(self.documents).unlink
        for kind in (errors.DeadlockDetected, errors.SerializationFailure):
            with self.subTest(kind=kind.__name__):
                root = self.folders.create({'name': 'Concurrent root %s' % kind.__name__,
                                            'owner_id': self.admin.id})
                in_root = self._doc(root, 'in-root.txt')
                holder = self.folders.create({'name': 'Concurrent holder %s' % kind.__name__})
                alone, healthy = self._doc(holder, 'alone.txt'), self._doc(holder, 'healthy.txt')
                root.with_user(self.admin).action_trash()
                (alone | healthy).with_user(self.admin).action_trash()
                self._expire(root, alone, healthy)
                roots.add(root.id)
                documents.add(alone.id)

                def failing_tree(folder, limit=None, kind=kind):
                    if folder.id in roots:
                        raise kind()
                    return purge_tree(folder, limit)

                def failing_unlink(records, kind=kind):
                    if documents & set(records.ids):
                        raise kind()
                    return unlink(records)

                with patch.object(trash_actions.WoowDocumentFolderTrash, '_woow_purge_tree',
                                  failing_tree), \
                        patch.object(trash_actions.WoowDocumentTrash, 'unlink', failing_unlink):
                    progress = self._cron()
                self.assertTrue(self._all_exist(root, in_root, alone))
                self.assertFalse(healthy.exists())
                self.assertEqual(progress, {'done': 1, 'remaining': 0})


@tagged('woow_docmgmt_trash')
class TestPurgeRacesWithLegalHold(TransactionCase):
    """A legal hold committed while a purge batch runs is never bypassed.

    Real Repeatable Read transactions of their own (no registry test mode). The
    fixture is committed first: an expired trashed root with one child folder that
    holds four documents. The purge batch T takes its snapshot, a hold H commits
    after it, and T then runs the daily job with room for two records only, so it
    would delete documents without touching the held folder's row.
    """

    def setUp(self):
        super().setUp()
        self._documents, self._folders = [], []
        self.addCleanup(self._drop_committed)
        self.admin_id = self.env.ref('base.user_admin').id

    def _commit_fixture(self):
        old = fields.Datetime.now() - timedelta(days=400)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {
                'tracking_disable': True, 'mail_create_nolog': True,
                'mail_create_nosubscribe': True})
            folders = env['woow.document.folder']
            # The lowest sequence puts this root first in the job's folder search.
            root = folders.create({'name': 'Race root %s' % secrets.token_hex(6),
                                   'owner_id': self.admin_id, 'sequence': -1000})
            child = folders.create({'name': 'Race child', 'parent_id': root.id,
                                    'owner_id': self.admin_id})
            documents = env['woow.document'].create([{
                'name': 'race-%d.txt' % index, 'folder_id': child.id,
                'datas': base64.b64encode(b'race fixture %d' % index)} for index in range(4)])
            root.with_user(self.admin_id).action_trash()
            root.with_context(active_test=False).write({'trashed_date': old})
            self._folders += [child.id, root.id]
            self._documents += documents.ids
            return root.id, child.id, documents.ids

    def _commit_trashed_document(self):
        """An expired document trashed on its own in a folder that stays available."""
        old = fields.Datetime.now() - timedelta(days=400)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {
                'tracking_disable': True, 'mail_create_nolog': True,
                'mail_create_nosubscribe': True})
            folder = env['woow.document.folder'].create({
                'name': 'Race holder %s' % secrets.token_hex(6), 'owner_id': self.admin_id})
            document = env['woow.document'].create({
                'name': 'race-alone.txt', 'folder_id': folder.id,
                'datas': base64.b64encode(b'race fixture alone')})
            document.with_user(self.admin_id).action_trash()
            document.with_context(active_test=False).write({'trashed_date': old})
            self._folders.append(folder.id)
            self._documents.append(document.id)
            return document.id

    def _drop_committed(self):
        if not self._folders:
            return
        with self.registry.cursor() as cr:
            documents, folders = self._documents, self._folders
            cr.execute('DELETE FROM woow_document_log WHERE document_id = ANY(%s) '
                       'OR folder_id = ANY(%s)', (documents, folders))
            cr.execute("DELETE FROM mail_followers WHERE (res_model = 'woow.document' "
                       "AND res_id = ANY(%s)) OR (res_model = 'woow.document.folder' "
                       "AND res_id = ANY(%s))", (documents, folders))
            cr.execute("DELETE FROM mail_message WHERE (model = 'woow.document' "
                       "AND res_id = ANY(%s)) OR (model = 'woow.document.folder' "
                       "AND res_id = ANY(%s))", (documents, folders))
            cr.execute('DELETE FROM woow_document WHERE id = ANY(%s)', (documents,))
            # After the documents: their attachment_id restricts the attachment rows.
            cr.execute("DELETE FROM ir_attachment WHERE res_model = 'woow.document' "
                       "AND res_id = ANY(%s)", (documents,))
            for folder_id in folders:   # children first
                cr.execute('DELETE FROM woow_document_folder WHERE id = %s', (folder_id,))

    def _hold(self, model, record_id):
        """H: a document administrator sets the hold and commits."""
        with self.registry.cursor() as cr:
            api.Environment(cr, self.admin_id, {})[model].browse(
                record_id).action_set_legal_hold(True)

    def _purge_batch(self, cr):
        """T: the daily job with room for two records; returns (progress, failure)."""
        env = api.Environment(cr, SUPERUSER_ID, {})
        progress = []
        failure = None
        with patch.object(cron_jobs, 'PURGE_BATCH', 2), \
                patch.object(type(env['ir.cron']), '_notify_progress',
                             lambda cron, **values: progress.append(values)), \
                mute_logger('odoo.sql_db'):
            try:
                env['woow.document']._cron_purge_trash()
            except errors.SerializationFailure as exc:
                failure = exc
        if failure:
            cr.rollback()
        else:
            cr.commit()
        return progress, failure

    def _remaining(self, root_id, child_id, document_ids):
        with self.registry.cursor() as cr:
            cr.execute('SELECT count(*) FROM woow_document WHERE id = ANY(%s)', (document_ids,))
            documents = cr.fetchone()[0]
            cr.execute('SELECT count(*) FROM woow_document_folder WHERE id = ANY(%s)',
                       ([root_id, child_id],))
            return documents, cr.fetchone()[0]

    def test_folder_hold_committed_after_the_batch_snapshot_keeps_the_folder_content(self):
        """The hold rewrites the root row only; deleting documents would not conflict."""
        root_id, child_id, document_ids = self._commit_fixture()
        with self.registry.cursor() as cr_t:
            cr_t.execute('SELECT 1')   # T's first statement takes its snapshot.
            self._hold('woow.document.folder', child_id)
            progress, failure = self._purge_batch(cr_t)
        self.assertIsNone(failure)
        self.assertEqual(len(progress), 1)
        self.assertEqual(self._remaining(root_id, child_id, document_ids), (4, 2))
        with self.registry.cursor() as cr:
            cr.execute('SELECT id, legal_hold IS TRUE, subtree_legal_hold IS TRUE '
                       'FROM woow_document_folder WHERE id = ANY(%s) ORDER BY id',
                       ([root_id, child_id],))
            self.assertEqual(cr.fetchall(), sorted([(root_id, False, True),
                                                    (child_id, True, True)]))
            cr.execute("SELECT count(*) FROM woow_document_log WHERE event = 'purge' "
                       "AND (document_id = ANY(%s) OR folder_id = ANY(%s))",
                       (document_ids, [root_id, child_id]))
            self.assertEqual(cr.fetchone()[0], 0)

    def test_document_hold_committed_after_the_batch_snapshot_skips_the_root(self):
        """The delete fails on the held row; the batch commits and skips the root."""
        root_id, child_id, document_ids = self._commit_fixture()
        held = min(document_ids)   # documents are deleted in id order: first chunk
        with self.registry.cursor() as cr_t:
            cr_t.execute('SELECT 1')   # T's first statement takes its snapshot.
            self._hold('woow.document', held)
            progress, failure = self._purge_batch(cr_t)
        self.assertIsNone(failure, 'the batch failed instead of skipping the held root')
        self.assertEqual(len(progress), 1)
        self.assertEqual(self._remaining(root_id, child_id, document_ids), (4, 2))
        with self.registry.cursor() as cr:
            cr.execute('SELECT legal_hold IS TRUE FROM woow_document WHERE id = %s', (held,))
            self.assertEqual(cr.fetchone(), (True,))

    def test_hold_on_a_document_trashed_on_its_own_skips_only_that_document(self):
        """The job's document pass: the delete fails on the held row and the batch commits."""
        document_id = self._commit_trashed_document()
        with self.registry.cursor() as cr_t:
            cr_t.execute('SELECT 1')   # T's first statement takes its snapshot.
            self._hold('woow.document', document_id)
            progress, failure = self._purge_batch(cr_t)
        self.assertIsNone(failure, 'the batch failed instead of skipping the held document')
        self.assertEqual(len(progress), 1)
        with self.registry.cursor() as cr:
            cr.execute('SELECT legal_hold IS TRUE FROM woow_document WHERE id = %s',
                       (document_id,))
            self.assertEqual(cr.fetchone(), (True,))

    def test_a_hold_started_after_the_root_lock_waits_for_the_batch(self):
        """The root lock is the barrier for later holds as well: they wait for T."""
        root_id, child_id, _document_ids = self._commit_fixture()
        with self.registry.cursor() as cr_t, self.registry.cursor() as cr_h:
            cr_t.execute('SELECT 1')
            api.Environment(cr_t, SUPERUSER_ID, {})['woow.document.folder'].browse(
                root_id)._woow_lock_purge_root()
            cr_h.execute("SET LOCAL lock_timeout = '300ms'")
            child = api.Environment(cr_h, self.admin_id, {})['woow.document.folder'].browse(
                child_id)
            with mute_logger('odoo.sql_db'), self.assertRaises(errors.LockNotAvailable):
                child.action_set_legal_hold(True)
            cr_h.rollback()
            cr_t.rollback()
        with self.registry.cursor() as cr:
            cr.execute('SELECT legal_hold IS TRUE FROM woow_document_folder WHERE id = %s',
                       (child_id,))
            self.assertEqual(cr.fetchone(), (False,))
