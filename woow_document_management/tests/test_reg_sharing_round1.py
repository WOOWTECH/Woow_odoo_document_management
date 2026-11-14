# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Fix round 1 of release 18.0.2, lane ``fsharing``.

The restore wizard and a document that is not in the trash, the audit of the old share lists, the
expiry cron that must not be blocked by one row, and the locks of people who lost their access.
"""
import logging
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from ..models import sharing as sharing_module
from . import test_sharing

_LOG = 'woow.document.log'


class _Round1Case(test_sharing._SharingCase):

    def _events(self, event, document=None):
        self.env.flush_all()
        return self.env[_LOG].sudo().search([
            ('event', '=', event), ('document_id', '=', (document or self.document).id)],
            order='id')

    def _lock_as(self, user, document=None):
        (document or self.document).with_user(user).action_lock()
        self.env.flush_all()
        self.env.invalidate_all()


@tagged('woow_docmgmt_core')
class TestRestoreWizardOnlyRestores(_Round1Case):

    def _wizard(self, user, documents=(), folders=(), target=None):
        return self.env['woow.document.restore.wizard'].with_user(user).create({
            'document_ids': [Command.set([doc.id for doc in documents])],
            'folder_ids': [Command.set([folder.id for folder in folders])],
            'target_folder_id': (target or self.other_folder).id})

    def test_an_editor_by_share_cannot_move_a_live_document_through_the_wizard(self):
        self._row(user=self.editor, role='editor')
        self.assertTrue(self._can_write(self.editor))
        wizard = self._wizard(self.editor, [self.document])
        with self.assertRaises(AccessError):
            wizard.action_restore_to()
        self.env.invalidate_all()
        self.assertEqual(self.document.folder_id, self.folder)
        self.assertFalse(self._events('restore'))

    def test_a_live_document_is_refused_even_for_someone_who_may_move_it(self):
        wizard = self._wizard(self.edit_member, [self.document])
        with self.assertRaises(AccessError):
            wizard.action_restore_to()
        self.env.invalidate_all()
        self.assertEqual(self.document.folder_id, self.folder)

    def test_a_live_folder_is_refused(self):
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Round one live child', 'parent_id': self.folder.id,
            'owner_id': self.owner.id})
        self.env.flush_all()      # the stored access sets of the new folder, before a user looks for it
        # a member who can see the folder and may move it: the refusal is the wizard's own one
        # (the check of the names in the wizard row refuses what the caller cannot see)
        wizard = self._wizard(self.edit_member, folders=[child], target=self.other_folder)
        with self.assertRaises(AccessError):
            wizard.action_restore_to()
        self.env.invalidate_all()
        self.assertEqual(child.parent_id, self.folder)

    def test_a_trashed_document_is_still_restored_by_the_wizard(self):
        self.document.with_user(self.edit_member).action_trash()
        self.env.invalidate_all()
        self.assertFalse(self.document.active)
        wizard = self._wizard(self.edit_member, [self.document.with_context(active_test=False)],
                              target=self.other_folder)
        wizard.action_restore_to()
        self.env.invalidate_all()
        self.assertTrue(self.document.active)
        self.assertEqual(self.document.folder_id, self.other_folder)
        self.assertEqual(len(self._events('restore')), 1)


@tagged('woow_docmgmt_core')
class TestLegacyListsLeaveAnAuditRow(_Round1Case):

    def _changes(self):
        return self._events('access_change')

    def test_adding_and_removing_a_user_through_the_old_list_is_audited(self):
        before = len(self._changes())
        self.document.with_user(self.owner).write(
            {'shared_user_ids': [Command.link(self.viewer.id)]})
        row = self.shares.sudo().search([('document_id', '=', self.document.id),
                                         ('user_id', '=', self.viewer.id)])
        self.assertEqual(len(row), 1)
        added = self._changes()
        self.assertEqual(len(added), before + 1)
        self.assertEqual(added[-1].detail, {'share_ids': [row.id]})
        self.assertEqual(added[-1].user_id, self.owner)
        self.document.with_user(self.owner).write(
            {'shared_user_ids': [Command.unlink(self.viewer.id)]})
        removed = self._changes()
        self.assertEqual(len(removed), before + 2)
        self.assertEqual(removed[-1].detail, {'share_ids': [row.id]})

    def test_adding_and_removing_a_contact_through_the_old_list_is_audited(self):
        before = len(self._changes())
        partner = self.portal.partner_id
        self.document.with_user(self.owner).write(
            {'shared_partner_ids': [Command.link(partner.id)]})
        row = self.shares.sudo().search([('document_id', '=', self.document.id),
                                         ('partner_id', '=', partner.id)])
        self.assertEqual(len(row), 1)
        self.assertEqual(self._changes()[-1].detail, {'share_ids': [row.id]})
        self.document.with_user(self.owner).write(
            {'shared_partner_ids': [Command.clear()]})
        self.assertEqual(len(self._changes()), before + 2)
        self.assertEqual(self._changes()[-1].detail, {'share_ids': [row.id]})

    def test_a_list_given_at_creation_is_audited(self):
        document = self.env['woow.document'].with_user(self.owner).create({
            'name': 'listed.txt', 'folder_id': self.folder.id,
            'datas': 'bGlzdGVk', 'shared_user_ids': [Command.set([self.viewer.id])]})
        events = self._events('access_change', document)
        self.assertEqual(len(events), 1)
        self.assertEqual(list(events.detail), ['share_ids'])

    def test_a_write_that_changes_nothing_adds_no_row(self):
        self.document.with_user(self.owner).write(
            {'shared_user_ids': [Command.link(self.viewer.id)]})
        before = len(self._changes())
        self.document.with_user(self.owner).write(
            {'shared_user_ids': [Command.link(self.viewer.id)]})
        self.assertEqual(len(self._changes()), before)

    def test_a_row_change_is_still_audited_once(self):
        before = len(self._changes())
        self._row(user=self.viewer, as_user=self.owner)
        self.assertEqual(len(self._changes()), before + 1)


@tagged('woow_docmgmt_core')
class TestExpiryCronIsRobust(_Round1Case):

    def _rows(self, count, prefix, document=None):
        rows = self.shares
        for index in range(count):
            user = test_sharing.new_test_user(
                self.env, login='%s_%d' % (prefix, index), groups=test_sharing._USER)
            rows |= self._row(document=document, user=user,
                              expiry=fields.Date.today() + timedelta(days=1))
        self._expire(rows)
        return rows

    def test_rows_that_cannot_be_removed_do_not_hide_the_rows_behind_them(self):
        rows = self._rows(5, 'rd1_block')
        bad, good = rows[:2], rows[2:]
        original = type(self.shares).unlink

        def refuse(records):
            if records & bad:
                raise UserError('refused')
            return original(records)

        with patch.object(sharing_module, 'ACCESS_EXPIRY_BATCH', 2), \
                patch.object(type(self.shares), 'unlink', refuse), \
                self.assertLogs(sharing_module._logger, 'WARNING') as logged:
            self.env['woow.document'].sudo()._cron_expire_access()
        self.assertEqual(len(bad.exists()), 2)
        self.assertFalse(good.exists())
        # each refused row is logged once, not once per batch
        self.assertEqual(len([line for line in logged.output if 'was not removed' in line]), 2)

    def test_every_expired_row_goes_in_one_run_when_the_budget_allows(self):
        rows = self._rows(5, 'rd1_all')
        progress = []
        with patch.object(sharing_module, 'ACCESS_EXPIRY_BATCH', 2), patch.object(
                type(self.env['ir.cron']), '_notify_progress',
                lambda _self, *, done, remaining: progress.append((done, remaining))):
            self.env['woow.document'].sudo()._cron_expire_access()
        self.assertFalse(rows.exists())
        self.assertEqual(progress[-1], (5, 0))

    def test_a_spent_time_budget_stops_after_a_batch_and_says_more_is_left(self):
        rows = self._rows(5, 'rd1_time')
        progress = []
        with patch.object(sharing_module, 'ACCESS_EXPIRY_BATCH', 2), \
                patch.object(sharing_module, 'ACCESS_EXPIRY_SECONDS', 0), patch.object(
                    type(self.env['ir.cron']), '_notify_progress',
                    lambda _self, *, done, remaining: progress.append((done, remaining))):
            self.env['woow.document'].sudo()._cron_expire_access()
        self.assertEqual(len(rows.exists()), 3)
        self.assertEqual(progress[-1][0], 2)
        self.assertGreater(progress[-1][1], 0)
        self.env['woow.document'].sudo()._cron_expire_access()
        self.assertFalse(rows.exists())

    def test_a_row_that_never_goes_does_not_keep_the_run_going(self):
        rows = self._rows(1, 'rd1_stuck')
        progress = []
        with patch.object(type(self.shares), 'unlink',
                          lambda _records: (_ for _ in ()).throw(UserError('refused'))), \
                self.assertLogs(sharing_module._logger, 'WARNING'), patch.object(
                    type(self.env['ir.cron']), '_notify_progress',
                    lambda _self, *, done, remaining: progress.append((done, remaining))):
            self.env['woow.document'].sudo()._cron_expire_access()
        self.assertTrue(rows.exists())
        self.assertEqual(progress[-1], (0, 0))


@tagged('woow_docmgmt_core')
class TestLocksOfPeopleWhoLostAccess(_Round1Case):

    def _expire_and_run(self, rows):
        self._expire(rows)
        self.env['woow.document'].sudo()._cron_expire_access()
        self.env.invalidate_all()

    def test_the_cron_releases_the_lock_of_an_editor_whose_share_ended(self):
        row = self._row(user=self.editor, role='editor',
                        expiry=fields.Date.today() + timedelta(days=1))
        self._lock_as(self.editor)
        self.assertEqual(self.document.lock_uid, self.editor)
        self._expire_and_run(row)
        self.assertFalse(row.exists())
        self.assertFalse(self.document.lock_uid)
        self.assertFalse(self.document.lock_date)
        events = self._events('unlock')
        self.assertEqual(len(events), 1)
        self.assertEqual(events.detail, {'forced': True})
        self.assertEqual(events.channel, 'cron')
        self.document.with_user(self.upload_owner).write({'name': 'after the lock.txt'})
        self.assertEqual(self.document.name, 'after the lock.txt')

    def test_removing_the_share_row_releases_the_lock(self):
        row = self._row(user=self.editor, role='editor')
        self._lock_as(self.editor)
        row.with_user(self.owner).unlink()
        self.env.invalidate_all()
        self.assertFalse(self.document.lock_uid)
        self.assertEqual(self._events('unlock').detail, {'forced': True})

    def test_emptying_the_old_list_releases_the_lock(self):
        self._row(user=self.editor, role='editor')
        self._lock_as(self.editor)
        self.document.with_user(self.owner).write({'shared_user_ids': [Command.clear()]})
        self.env.invalidate_all()
        self.assertFalse(self.document.lock_uid)

    def test_a_holder_who_can_still_write_keeps_the_lock(self):
        row = self._row(user=self.edit_member, role='editor')
        self._lock_as(self.edit_member)
        row.with_user(self.owner).unlink()
        self.env.invalidate_all()
        # still an editor of the folder
        self.assertEqual(self.document.lock_uid, self.edit_member)
        self.assertFalse(self._events('unlock'))

    def test_the_end_of_a_member_row_releases_the_lock_of_that_member(self):
        line = self.env['woow.document.folder.access'].sudo().search([
            ('folder_id', '=', self.folder.id), ('user_id', '=', self.edit_member.id)])
        self._lock_as(self.edit_member)
        self.assertEqual(self.document.lock_uid, self.edit_member)
        line.with_user(self.owner).unlink()
        self.env.invalidate_all()
        self.assertFalse(self.document.lock_uid)
        self.assertEqual(self._events('unlock').detail, {'forced': True})

    def test_the_member_row_of_a_parent_reaches_the_documents_below(self):
        parent = self.env['woow.document.folder'].sudo().create({
            'name': 'Round one parent', 'owner_id': self.owner.id})
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Round one child', 'parent_id': parent.id, 'owner_id': self.owner.id})
        line = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': parent.id, 'subject_type': 'user', 'user_id': self.target.id,
            'level': 'edit'})
        document = self._document(child, 'below.txt')
        self.env.flush_all()
        self._lock_as(self.target, document)
        self.assertEqual(document.lock_uid, self.target)
        line.sudo().unlink()
        self.env.invalidate_all()
        self.assertFalse(document.lock_uid)

    def test_the_owner_releases_a_lock_of_someone_else_on_the_own_document(self):
        self._lock_as(self.edit_member)
        self.assertEqual(self.document.owner_id, self.upload_owner)
        self.assertTrue(self.document.with_user(self.upload_owner).action_unlock())
        self.assertFalse(self.document.lock_uid)
        self.assertEqual(self._events('unlock').detail, {'forced': True})

    def test_someone_who_is_neither_holder_owner_nor_manager_cannot_release(self):
        self._lock_as(self.edit_member)
        other = self._document(self.folder, 'someone-elses.txt', owner=self.owner)
        self._lock_as(self.edit_member, other)
        with self.assertRaises(AccessError):
            self.document.with_user(self.read_member).action_unlock()
        with self.assertRaises(AccessError):
            other.with_user(self.upload_owner).action_unlock()
        self.assertEqual(self.document.lock_uid, self.edit_member)
