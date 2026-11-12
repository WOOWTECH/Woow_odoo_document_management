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
from ..services.client_ip import hmac_ip, source_key


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

    def _purge_with_failing_records(self, kind, roots, documents):
        """One root and one document fail with ``kind`` during a purge run.

        ``roots`` and ``documents`` hold the ids that fail; the caller keeps them
        across calls, so the records that failed earlier keep failing in a later run.
        Returns the failing records, the record that does not fail, and the progress.
        """
        purge_tree = trash_actions.WoowDocumentFolderTrash._woow_purge_tree
        unlink = type(self.documents).unlink
        root = self.folders.create({'name': 'Failing root %s' % kind.__name__,
                                    'owner_id': self.admin.id})
        in_root = self._doc(root, 'in-root.txt')
        holder = self.folders.create({'name': 'Failing holder %s' % kind.__name__})
        alone, healthy = self._doc(holder, 'alone.txt'), self._doc(holder, 'healthy.txt')
        root.with_user(self.admin).action_trash()
        (alone | healthy).with_user(self.admin).action_trash()
        self._expire(root, alone, healthy)
        roots.add(root.id)
        documents.add(alone.id)

        def failing_tree(folder, limit=None):
            if folder.id in roots:
                raise kind()
            return purge_tree(folder, limit)

        def failing_unlink(records):
            if documents & set(records.ids):
                raise kind()
            return unlink(records)

        with patch.object(trash_actions.WoowDocumentFolderTrash, '_woow_purge_tree',
                          failing_tree), \
                patch.object(trash_actions.WoowDocumentTrash, 'unlink', failing_unlink):
            progress = self._cron()
        return root, in_root, alone, healthy, progress

    def test_concurrency_failures_skip_only_their_record(self):
        """A deadlock or serialization failure of one root or document never fails the run.

        Both errors leave the transaction usable once the record's savepoint is rolled
        back; the record counts as failed and the other records are still purged.
        """
        roots, documents = set(), set()
        for kind in (errors.DeadlockDetected, errors.SerializationFailure):
            with self.subTest(kind=kind.__name__):
                root, in_root, alone, healthy, progress = self._purge_with_failing_records(
                    kind, roots, documents)
                self.assertTrue(self._all_exist(root, in_root, alone))
                self.assertFalse(healthy.exists())
                self.assertEqual(progress, {'done': 1, 'remaining': 0})

    @staticmethod
    def _skipped_messages(roots, documents):
        return (['Purge skipped a folder the database refused to delete: id %d' % root
                 for root in roots] +
                ['Purge skipped a document the database refused to delete: id %d' % doc
                 for doc in documents])

    def test_integrity_errors_skip_only_their_record(self):
        """A delete the database refuses never fails the run, and is logged by id.

        A constraint violated by a row that goes with the record (a cascade, a
        foreign key action) makes the delete fail. The record's savepoint is rolled
        back, it counts as failed, and the other records are still purged.
        """
        roots, documents = set(), set()
        for kind in (errors.UniqueViolation, errors.ForeignKeyViolation):
            with self.subTest(kind=kind.__name__), \
                    self.assertLogs(trash_actions.__name__, 'WARNING') as logs:
                root, in_root, alone, healthy, progress = self._purge_with_failing_records(
                    kind, roots, documents)
                self.assertTrue(self._all_exist(root, in_root, alone))
                self.assertFalse(healthy.exists())
                self.assertEqual(progress, {'done': 1, 'remaining': 0})
                # The earlier subtest's records fail again, so each run logs every one.
                self.assertEqual(sorted(record.getMessage() for record in logs.records),
                                 sorted(self._skipped_messages(roots, documents)))

    def test_an_unexpected_error_skips_only_its_record_and_is_logged_by_class(self):
        """Any error of one record, not only the database ones, leaves the run intact (R2b-06).

        The record counts as failed, the others are purged, and the log names the record by
        its id and the error by its class, never by its text (which may hold data).
        """
        roots, documents = set(), set()
        for kind in (ValueError, KeyError, RuntimeError):
            with self.subTest(kind=kind.__name__), \
                    self.assertLogs(trash_actions.__name__, 'WARNING') as logs:
                root, in_root, alone, healthy, progress = self._purge_with_failing_records(
                    kind, roots, documents)
                self.assertTrue(self._all_exist(root, in_root, alone))
                self.assertFalse(healthy.exists())
                self.assertEqual(progress, {'done': 1, 'remaining': 0})
                expected = (
                    ['Purge skipped a folder after an unexpected error: id %d (%s)' % (
                        record, kind.__name__) for record in roots] +
                    ['Purge skipped a document after an unexpected error: id %d (%s)' % (
                        record, kind.__name__) for record in documents])
                self.assertEqual(sorted(record.getMessage() for record in logs.records),
                                 sorted(expected))

    def test_trashed_folders_without_a_company_are_purged_like_any_other(self):
        """A folder shared across companies has no company: its audit rows must be written."""
        shared = self.folders.create({'name': 'Shared root', 'owner_id': self.admin.id,
                                      'company_id': False})
        inside = self._doc(shared, 'shared.txt')
        healthy = self.folders.create({'name': 'Healthy beside shared', 'owner_id': self.admin.id})
        kept = self._doc(healthy, 'healthy-beside.txt')
        self.assertFalse(inside.company_id)
        (shared | healthy).with_user(self.admin).action_trash()
        self._expire(shared, healthy)
        progress = self._cron()
        self.assertTrue(self._none_exist(shared, inside, healthy, kept))
        self.assertEqual(progress, {'done': 4, 'remaining': 0})
        events = self.env['woow.document.log'].sudo().search([
            ('event', 'in', ('upload', 'trash', 'purge')), ('company_id', '=', False)])
        self.assertEqual(sorted(events.mapped('event')),
                         ['purge', 'purge', 'trash', 'upload'])

    def test_items_that_stay_undeletable_for_days_alert_the_administrators(self):
        """After POISON_ALERT_DAYS days past the retention an administrator is told, once a day."""
        manager = self.admin
        self.env.ref('woow_document_management.group_woow_document_manager').write({
            'users': [(4, manager.id)]})
        summary = 'Items in the trash could not be deleted'
        activities = self.env['mail.activity'].sudo().with_context(active_test=False)
        domain = [('res_model', '=', 'res.partner'), ('res_id', '=', manager.partner_id.id),
                  ('user_id', '=', manager.id), ('summary', '=', summary)]

        def failing(name, days):
            """A trashed root that fails to purge, expired ``days`` days ago."""
            root = self.folders.create({'name': name, 'owner_id': self.admin.id})
            root.with_user(self.admin).action_trash()
            root.with_context(active_test=False).sudo().write({
                'trashed_date': fields.Datetime.now() - timedelta(days=days)})
            return root

        def run(*roots):
            purge_tree = trash_actions.WoowDocumentFolderTrash._woow_purge_tree

            def failing_tree(folder, limit=None):
                if folder.id in {root.id for root in roots}:
                    raise ValueError('poison')
                return purge_tree(folder, limit)

            with patch.object(trash_actions.WoowDocumentFolderTrash, '_woow_purge_tree',
                              failing_tree):
                self._cron()

        recent = failing('Recently expired poison', 31)    # retention is 30 days
        with self.assertLogs(trash_actions.__name__, 'WARNING'):
            run(recent)
        self.assertEqual(activities.search_count(domain), 0)
        stuck = failing('Long expired poison', 30 + 3 + 1)
        with self.assertLogs(trash_actions.__name__, 'WARNING'):
            run(recent, stuck)
        found = activities.search(domain)
        self.assertEqual(len(found), 1)
        self.assertEqual(found.user_id, manager)
        self.assertIn('1 items have stayed in the trash for more than 3 days', found.note)
        self.assertNotIn('poison', found.note)
        with self.assertLogs(trash_actions.__name__, 'WARNING'):
            run(recent, stuck)
        self.assertEqual(activities.search_count(domain), 1)   # one activity a day
        self.assertTrue(self._all_exist(recent, stuck))

    def _failing_link(self, folder, ip):
        """A password link on the folder with a counted wrong password of the source."""
        share, _code = self.env['woow.document.share'].with_user(self.admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
            'password': secrets.token_urlsafe(16)})
        self.env['woow.document.log']._log_event(
            'share_unlock_failed', result='denied', channel='share',
            share_id=share.id, ip=ip)
        return share

    def test_purging_a_folder_deletes_its_links_with_their_failure_buckets(self):
        """Links on a purged folder go with it, whatever their failure buckets are.

        Two links of the folder have buckets of one source in one minute. The links
        are deleted by the folder's cascade, which clears ``share_id`` of both
        buckets at once; the buckets stay as audit rows, and the purge neither fails
        nor skips the folder.
        """
        ip = '198.51.100.201'
        folder = self.folders.create({'name': 'Link purge root', 'owner_id': self.admin.id})
        document = self._doc(folder, 'in-link-folder.txt')
        links = self._failing_link(folder, ip) | self._failing_link(folder, ip)
        other = self.folders.create({'name': 'Link purge bystander', 'owner_id': self.admin.id})
        bystander = self._failing_link(other, ip)
        digest = hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, source_key(ip))
        folder.with_user(self.admin).action_trash()
        self._expire(folder)
        with self.assertNoLogs(trash_actions.__name__, 'WARNING'):
            progress = self._cron()
        self.assertEqual(progress, {'done': 2, 'remaining': 0})
        self.assertTrue(self._none_exist(folder, document, links))
        self.assertTrue(self._all_exist(other, bystander))
        self.env.flush_all()
        self.env.invalidate_all()
        rows = self.env['woow.document.log'].sudo().search([
            ('event', '=', 'share_unlock_failed'), ('ip_hash', '=', digest)])
        self.assertEqual({row.link_ref: (row.share_id.id, row.count) for row in rows}, {
            links[0].id: (False, 1), links[1].id: (False, 1), bystander.id: (bystander.id, 1)})


    def _stored(self, folder, name):
        """The stored value of a folder flag, read from the table."""
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT subtree_legal_hold IS TRUE, has_restricted_descendant IS TRUE '
            'FROM woow_document_folder WHERE id = %s', (folder.id,))
        hold, restricted = self.env.cr.fetchone()
        return {'subtree_legal_hold': hold, 'has_restricted_descendant': restricted}[name]

    def _separately_trashed(self, name):
        """Root -> child trashed on its own first -> active leaf; then the root is trashed."""
        root = self.folders.create({'name': '%s root' % name, 'owner_id': self.admin.id})
        child = self.folders.create({'name': '%s child' % name, 'parent_id': root.id,
                                     'owner_id': self.admin.id})
        leaf = self.folders.create({'name': '%s leaf' % name, 'parent_id': child.id,
                                    'owner_id': self.admin.id})
        child.with_user(self.admin).action_trash()
        root.with_user(self.admin).action_trash()
        return root, child, leaf

    def test_flags_below_a_separately_trashed_folder_reach_the_root(self):
        """The stored subtree flags count trashed children in every context.

        The hold is set, and the access changed, in the default context, where
        reading ``child_ids`` drops inactive children.
        """
        root, child, leaf = self._separately_trashed('Flag')
        folders = self.env['woow.document.folder']
        for target in (child, leaf):
            with self.subTest(target=target.name):
                folders.with_user(self.admin).browse(target.id).action_set_legal_hold(True)
                self.assertTrue(self._stored(root, 'subtree_legal_hold'))
                folders.with_user(self.admin).browse(target.id).action_set_legal_hold(False)
                self.assertFalse(self._stored(root, 'subtree_legal_hold'))
                folders.sudo().browse(target.id).write({'inherit_access': False})
                self.assertTrue(self._stored(root, 'has_restricted_descendant'))
                folders.sudo().browse(target.id).write({'inherit_access': True})
                self.assertFalse(self._stored(root, 'has_restricted_descendant'))

    def test_module_update_recomputes_stale_subtree_flags(self):
        """Rows stored before trashed children counted are fixed on each module update."""
        root, child, _leaf = self._separately_trashed('Stale flag')
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE woow_document_folder SET legal_hold = TRUE, subtree_legal_hold = TRUE, '
            'inherit_access = FALSE, has_restricted_descendant = TRUE WHERE id = %s',
            (child.id,))
        self.env.cr.execute(
            'UPDATE woow_document_folder SET subtree_legal_hold = FALSE, '
            'has_restricted_descendant = FALSE WHERE id = %s', (root.id,))
        self.env.invalidate_all()
        self.env['woow.document.folder'].init()
        self.assertTrue(self._stored(root, 'subtree_legal_hold'))
        self.assertTrue(self._stored(root, 'has_restricted_descendant'))
        self.folders.browse(root.id).invalidate_recordset()
        self.assertTrue(self.folders.browse(root.id).subtree_legal_hold)


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

    def _commit_separately_trashed_fixture(self, below=False):
        """An expired root whose child was trashed on its own first, with four documents.

        The child is trashed before the root and both have an old trashed date.
        With ``below`` the documents sit in an active folder under that child.
        Returns (folder ids deepest first, the folder that holds the documents,
        document ids).
        """
        old = fields.Datetime.now() - timedelta(days=400)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {
                'tracking_disable': True, 'mail_create_nolog': True,
                'mail_create_nosubscribe': True})
            folders = env['woow.document.folder']
            root = folders.create({'name': 'Race root %s' % secrets.token_hex(6),
                                   'owner_id': self.admin_id, 'sequence': -1000})
            child = folders.create({'name': 'Race trashed child', 'parent_id': root.id,
                                    'owner_id': self.admin_id})
            chain = [child, root]
            holder = child
            if below:
                holder = folders.create({'name': 'Race active folder', 'parent_id': child.id,
                                         'owner_id': self.admin_id})
                chain.insert(0, holder)
            documents = env['woow.document'].create([{
                'name': 'race-%d.txt' % index, 'folder_id': holder.id,
                'datas': base64.b64encode(b'race fixture %d' % index)} for index in range(4)])
            child.with_user(self.admin_id).action_trash()
            root.with_user(self.admin_id).action_trash()
            (root | child).with_context(active_test=False).write({'trashed_date': old})
            folder_ids = [folder.id for folder in chain]
            self._folders += folder_ids
            self._documents += documents.ids
            return folder_ids, holder.id, documents.ids

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

    def _kept(self, folder_ids, document_ids):
        """(documents left, folders left, purge events) of one fixture."""
        with self.registry.cursor() as cr:
            cr.execute('SELECT count(*) FROM woow_document WHERE id = ANY(%s)', (document_ids,))
            documents = cr.fetchone()[0]
            cr.execute('SELECT count(*) FROM woow_document_folder WHERE id = ANY(%s)',
                       (folder_ids,))
            folders = cr.fetchone()[0]
            cr.execute("SELECT count(*) FROM woow_document_log WHERE event = 'purge' "
                       "AND (document_id = ANY(%s) OR folder_id = ANY(%s))",
                       (document_ids, folder_ids))
            return documents, folders, cr.fetchone()[0]

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


    def _race_separately_trashed(self, below, before_snapshot):
        """T purges with room for two records while H holds the folder with the documents."""
        folder_ids, held, document_ids = self._commit_separately_trashed_fixture(below=below)
        if before_snapshot:
            self._hold('woow.document.folder', held)
        with self.registry.cursor() as cr_t:
            cr_t.execute('SELECT 1')   # T's first statement takes its snapshot.
            if not before_snapshot:
                self._hold('woow.document.folder', held)
            progress, failure = self._purge_batch(cr_t)
        self.assertIsNone(failure, 'the batch failed instead of skipping the held root')
        self.assertEqual(len(progress), 1)
        self.assertEqual(self._kept(folder_ids, document_ids), (4, len(folder_ids), 0))

    def test_hold_on_a_separately_trashed_child_after_the_snapshot_keeps_its_content(self):
        """The child is its own trashed root too; neither purge may delete its documents."""
        self._race_separately_trashed(below=False, before_snapshot=False)

    def test_hold_below_a_separately_trashed_child_after_the_snapshot_keeps_its_content(self):
        """The held folder is active, below a child that was trashed on its own."""
        self._race_separately_trashed(below=True, before_snapshot=False)

    def test_holds_committed_before_the_snapshot_keep_separately_trashed_content(self):
        """Control: the same holds seen in T's snapshot keep the same content."""
        for below in (False, True):
            with self.subTest(below=below):
                self._race_separately_trashed(below=below, before_snapshot=True)

    def test_a_folder_row_changed_below_the_root_after_the_snapshot_skips_the_root(self):
        """The barrier is the lock on every folder row of the subtree, not the stored flags.

        H writes a hold on the deepest folder without the ORM, so no stored flag
        above it changes; the purge must still skip the whole root.
        """
        folder_ids, held, document_ids = self._commit_separately_trashed_fixture(below=True)
        with self.registry.cursor() as cr_t:
            cr_t.execute('SELECT 1')   # T's first statement takes its snapshot.
            with self.registry.cursor() as cr_h:
                cr_h.execute('UPDATE woow_document_folder SET legal_hold = TRUE WHERE id = %s',
                             (held,))
            progress, failure = self._purge_batch(cr_t)
        self.assertIsNone(failure)
        self.assertEqual(len(progress), 1)
        self.assertEqual(self._kept(folder_ids, document_ids), (4, len(folder_ids), 0))

    def test_a_hold_below_a_separately_trashed_child_waits_for_the_batch(self):
        """A hold that starts after T locked the root waits, wherever it is in the subtree."""
        folder_ids, held, _document_ids = self._commit_separately_trashed_fixture(below=True)
        root_id = folder_ids[-1]
        with self.registry.cursor() as cr_t, self.registry.cursor() as cr_h:
            cr_t.execute('SELECT 1')
            api.Environment(cr_t, SUPERUSER_ID, {})['woow.document.folder'].browse(
                root_id)._woow_lock_purge_root()
            cr_h.execute("SET LOCAL lock_timeout = '300ms'")
            folder = api.Environment(cr_h, self.admin_id, {})['woow.document.folder'].browse(held)
            with mute_logger('odoo.sql_db'), self.assertRaises(errors.LockNotAvailable):
                folder.action_set_legal_hold(True)
            cr_h.rollback()
            cr_t.rollback()
        with self.registry.cursor() as cr:
            cr.execute('SELECT legal_hold IS TRUE FROM woow_document_folder WHERE id = %s',
                       (held,))
            self.assertEqual(cr.fetchone(), (False,))
