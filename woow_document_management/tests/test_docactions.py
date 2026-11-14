# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Lock, duplicate and rename: who may do what, and what a lock holds back."""
import base64
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models import docactions
from ..services import locks
from ..models.docactions import DUPLICATE_MAX_PER_CALL
from ..models.upload_policy import WoowUploadRejected
from ..services.copy_names import copy_name

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_PARAM = 'woow_document_management.'


def _b64(data):
    return base64.b64encode(data)


class DocActionsCase(TransactionCase):
    """Folder with an editor, a second editor, a reader, a folder manager and an administrator."""

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = new_test_user(env, login='act_admin', groups=_MANAGER)
        self.alice = new_test_user(env, login='act_alice', groups=_USER)      # edit
        self.bob = new_test_user(env, login='act_bob', groups=_USER)          # edit
        self.carol = new_test_user(env, login='act_carol', groups=_USER)      # read only
        self.dave = new_test_user(env, login='act_dave', groups=_USER)        # manage
        self.uma = new_test_user(env, login='act_uma', groups=_USER)          # upload only
        self.nobody = new_test_user(env, login='act_nobody', groups=_USER)    # no grant
        self.portal = new_test_user(env, login='act_portal', groups='base.group_portal')
        self.folder = env['woow.document.folder'].sudo().create({
            'name': 'Actions folder', 'owner_id': self.admin.id})
        access = env['woow.document.folder.access'].sudo()
        for user, level in ((self.alice, 'edit'), (self.bob, 'edit'), (self.carol, 'read'),
                            (self.dave, 'manage'), (self.uma, 'upload')):
            access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                           'user_id': user.id, 'level': level})
        access.create({'folder_id': self.folder.id, 'subject_type': 'partner',
                       'partner_id': self.portal.partner_id.id, 'level': 'read'})
        self.tag = env['woow.document.tag'].sudo().create({'name': 'Actions tag'})
        self.doc = self._doc('report.txt', b'actions content')
        self.log = env['woow.document.log'].sudo()
        env.flush_all()

    def _doc(self, name, data, folder=None, **vals):
        return self.env['woow.document'].sudo().create(dict({
            'name': name, 'folder_id': (folder or self.folder).id,
            'datas': _b64(data), 'owner_id': self.alice.id}, **vals))

    def _as(self, user, record=None):
        return (record if record is not None else self.doc).with_user(user)

    def _events(self, event, doc=None):
        return self.log.search([('event', '=', event),
                                ('document_id', '=', (doc or self.doc).id)])


@tagged('woow_docmgmt_core')
class TestLockRights(DocActionsCase):

    def test_an_editor_locks_and_the_lock_is_recorded(self):
        self.assertFalse(self.doc.is_locked)
        self.assertTrue(self._as(self.alice).action_lock())
        self.assertEqual(self.doc.lock_uid, self.alice)
        self.assertTrue(self.doc.lock_date)
        self.assertTrue(self.doc.is_locked)
        self.assertEqual(len(self._events('lock')), 1)
        self.assertEqual(self._events('lock').user_id, self.alice)

    def test_locking_again_by_the_holder_changes_nothing(self):
        self._as(self.alice).action_lock()
        first = self.doc.lock_date
        self._as(self.alice).action_lock()
        self.assertEqual(self.doc.lock_date, first)
        self.assertEqual(len(self._events('lock')), 1)

    def test_a_reader_an_uploader_and_a_stranger_cannot_lock(self):
        for user in (self.carol, self.nobody):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self._as(user).action_lock()
        # an upload-only member owns none of this document, so it is not writable for them
        with self.assertRaises(AccessError):
            self._as(self.uma).action_lock()
        self.assertFalse(self.doc.lock_uid)
        self.assertFalse(self._events('lock'))

    def test_the_owner_with_upload_rights_may_lock_own_document(self):
        own = self._doc('own.txt', b'own', owner_id=self.uma.id)
        self._as(self.uma, own).action_lock()
        self.assertEqual(own.lock_uid, self.uma)

    def test_a_portal_user_cannot_lock_or_unlock(self):
        with self.assertRaises(AccessError):
            self._as(self.portal).action_lock()
        self._as(self.alice).action_lock()
        with self.assertRaises(AccessError):
            self._as(self.portal).action_unlock()
        self.assertEqual(self.doc.lock_uid, self.alice)

    def test_a_locked_document_cannot_be_locked_by_another_user(self):
        self._as(self.alice).action_lock()
        with self.assertRaises(UserError):
            self._as(self.bob).action_lock()
        with self.assertRaises(UserError):
            self._as(self.admin).action_lock()
        self.assertEqual(self.doc.lock_uid, self.alice)

    def test_a_batch_with_one_locked_document_is_refused_whole(self):
        other = self._doc('other.txt', b'other')
        self._as(self.alice).action_lock()
        with self.assertRaises(UserError):
            (self.doc | other).with_user(self.bob).action_lock()
        self.assertFalse(other.lock_uid)

    def test_a_trashed_document_cannot_be_locked(self):
        self._as(self.alice).action_trash()
        with self.assertRaises(AccessError):
            self.doc.with_user(self.alice).with_context(active_test=False).action_lock()

    def test_who_may_unlock(self):
        for user, allowed in ((self.alice, True), (self.dave, True), (self.admin, True),
                              (self.bob, False), (self.carol, False), (self.nobody, False)):
            with self.subTest(user=user.login):
                self._as(self.alice).action_lock()
                if allowed:
                    self.assertTrue(self._as(user).action_unlock())
                    self.assertFalse(self.doc.lock_uid)
                    self.assertFalse(self.doc.lock_date)
                else:
                    with self.assertRaises(AccessError):
                        self._as(user).action_unlock()
                    self.assertEqual(self.doc.lock_uid, self.alice)
                    self._as(self.alice).action_unlock()

    def test_unlocking_is_logged_and_marks_a_release_by_someone_else(self):
        self._as(self.alice).action_lock()
        self._as(self.alice).action_unlock()
        self._as(self.alice).action_lock()
        self._as(self.dave).action_unlock()
        events = self._events('unlock').sorted('id')
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].detail, {'forced': False})
        self.assertEqual(events[1].detail, {'forced': True})
        self.assertEqual(events[1].user_id, self.dave)

    def test_unlocking_a_free_document_is_a_quiet_no_op(self):
        self.assertTrue(self._as(self.bob).action_unlock())
        self.assertFalse(self._events('unlock'))

    def test_a_manager_of_another_folder_cannot_unlock_here(self):
        elsewhere = self.env['woow.document.folder'].sudo().create({
            'name': 'Elsewhere', 'owner_id': self.admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': elsewhere.id, 'subject_type': 'user', 'user_id': self.nobody.id,
            'level': 'manage'})
        self._as(self.alice).action_lock()
        with self.assertRaises(AccessError):
            self._as(self.nobody).action_unlock()

    def test_a_document_of_another_company_is_out_of_reach(self):
        company = self.env['res.company'].sudo().create({'name': 'Actions other company'})
        outsider = new_test_user(
            self.env, login='act_outsider', groups=_USER, company_id=company.id,
            company_ids=[Command.set([company.id])])
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': outsider.id,
            'level': 'manage'})
        for method in ('action_lock', 'action_unlock', 'action_duplicate'):
            with self.subTest(method=method), self.assertRaises(AccessError):
                getattr(self._as(outsider), method)()
        self.assertFalse(self.doc.lock_uid)

    def test_lock_batches_are_bounded(self):
        docs = self.doc | self._doc('two.txt', b'two') | self._doc('three.txt', b'three')
        with patch.object(docactions, 'LOCK_MAX_PER_CALL', 2):
            with self.assertRaises(UserError):
                docs.with_user(self.admin).action_lock()
            with self.assertRaises(UserError):
                docs.with_user(self.admin).action_unlock()
        self.assertFalse(docs.lock_uid)
        docs.with_user(self.admin).action_lock()
        self.assertEqual(docs.lock_uid, self.admin)


@tagged('woow_docmgmt_core')
class TestLockFieldsAreServerOnly(DocActionsCase):

    def test_an_rpc_write_to_a_lock_field_is_refused(self):
        for user in (self.alice, self.dave, self.admin):
            for vals in ({'lock_uid': user.id}, {'lock_date': '2026-01-01 00:00:00'},
                         {'is_locked': True}, {'lock_uid': False},
                         {'woow_locked_by_other': True}, {'name': 'x.txt', 'lock_uid': user.id}):
                with self.subTest(user=user.login, vals=sorted(vals)), \
                        self.assertRaises(AccessError):
                    self._as(user).write(dict(vals))
        self.assertFalse(self.doc.lock_uid)
        self.assertEqual(self.doc.name, 'report.txt')

    def test_a_forged_lock_cannot_be_cleared_by_write_either(self):
        self._as(self.alice).action_lock()
        with self.assertRaises(AccessError):
            self._as(self.bob).write({'lock_uid': False})
        with self.assertRaises(AccessError):
            self._as(self.alice).write({'lock_uid': False, 'lock_date': False})
        self.assertEqual(self.doc.lock_uid, self.alice)

    def test_create_refuses_the_lock_fields(self):
        for vals in ({'lock_uid': self.alice.id}, {'lock_date': '2026-01-01 00:00:00'},
                     {'is_locked': True}):
            with self.subTest(vals=sorted(vals)), self.assertRaises(AccessError):
                self.env['woow.document'].with_user(self.alice).create(dict(
                    vals, name='forged.txt', folder_id=self.folder.id, datas=_b64(b'plain text content')))

    def test_a_stored_default_cannot_lock_a_new_document(self):
        self.env['ir.default'].with_user(self.alice).set(
            'woow.document', 'lock_uid', self.bob.id, user_id=True)
        doc = self.env['woow.document'].with_user(self.alice).create({
            'name': 'fresh.txt', 'folder_id': self.folder.id, 'datas': _b64(b'plain text content')})
        self.assertFalse(doc.sudo().lock_uid)

    def test_a_copy_of_the_record_does_not_carry_the_lock(self):
        self.assertFalse(self.env['woow.document']._fields['lock_uid'].copy)
        self.assertFalse(self.env['woow.document']._fields['lock_date'].copy)

    def test_the_lock_fields_are_not_readable_by_portal_users(self):
        self._as(self.alice).action_lock()
        portal_doc = self.doc.with_user(self.portal)
        self.assertTrue(portal_doc.name)
        for name in ('lock_uid', 'lock_date', 'is_locked', 'woow_locked_by_other'):
            with self.subTest(field=name), self.assertRaises(AccessError):
                portal_doc[name]
            with self.subTest(search=name), self.assertRaises(AccessError):
                self.env['woow.document'].with_user(self.portal).search([(name, '!=', False)])

    def test_the_lock_fields_can_be_searched_by_users(self):
        self._as(self.alice).action_lock()
        docs = self.env['woow.document'].with_user(self.bob)
        self.assertIn(self.doc, docs.search([('is_locked', '=', True)]))
        self.assertNotIn(self.doc, docs.search([('is_locked', '=', False)]))
        self.assertIn(self.doc, docs.search([('lock_uid', '=', self.alice.id)]))
        self.assertTrue(self._as(self.bob).woow_locked_by_other)
        self.assertFalse(self._as(self.alice).woow_locked_by_other)


@tagged('woow_docmgmt_core')
class TestWhatALockHoldsBack(DocActionsCase):

    def setUp(self):
        super().setUp()
        self.link = self.env['woow.document'].sudo().create({
            'name': 'a link', 'folder_id': self.folder.id, 'document_type': 'url',
            'url': 'https://example.com/a', 'owner_id': self.alice.id})
        self.other_folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Other actions folder', 'owner_id': self.admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.other_folder.id, 'subject_type': 'user', 'user_id': self.bob.id,
            'level': 'edit'})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.other_folder.id, 'subject_type': 'user', 'user_id': self.alice.id,
            'level': 'edit'})
        self.contact = self.env['res.partner'].sudo().create({'name': 'Lock contact'})
        self._as(self.alice).action_lock()
        self._as(self.alice, self.link).action_lock()

    def _blocked(self, user, vals, record=None):
        record = self.doc if record is None else record
        with self.assertRaises(UserError) as caught:
            record.with_user(user).write(dict(vals))
        self.assertNotIsInstance(caught.exception, AccessError)
        self.assertIn('locked', str(caught.exception))
        return caught.exception

    def test_every_protected_field_is_refused_to_a_second_user(self):
        cases = {
            'name': {'name': 'renamed.txt'},
            'content': {'datas': _b64(b'new content')},
            'folder': {'folder_id': self.other_folder.id},
            'tags': {'tag_ids': [Command.link(self.tag.id)]},
            'description': {'description': '<p>text</p>'},
            'owner': {'owner_id': self.bob.id},
            'contact': {'partner_id': self.contact.id},
            'properties': {'properties': {}},
            'archived': {'active': False},
            'type': {'document_type': 'url'},
        }
        for label, vals in cases.items():
            for user in (self.bob, self.dave, self.admin):
                with self.subTest(field=label, user=user.login):
                    self._blocked(user, vals)
        for user in (self.bob, self.admin):
            with self.subTest(field='link', user=user.login):
                self._blocked(user, {'url': 'https://example.com/b'}, self.link)
        self.assertEqual(self.doc.name, 'report.txt')
        self.assertEqual(self.doc.folder_id, self.folder)

    def test_the_message_names_no_holder(self):
        error = self._blocked(self.bob, {'name': 'x.txt'})
        self.assertNotIn(self.alice.name, str(error))
        self.assertNotIn(self.alice.login, str(error))

    def test_a_mixed_write_is_refused_whole(self):
        self._blocked(self.bob, {'name': 'x.txt', 'tag_ids': [Command.link(self.tag.id)]})
        self.assertFalse(self.doc.tag_ids)

    def test_the_holder_changes_the_document_freely(self):
        self._as(self.alice).write({'name': 'mine.txt', 'description': '<p>d</p>',
                                    'tag_ids': [Command.link(self.tag.id)]})
        self._as(self.alice).write({'folder_id': self.other_folder.id})
        self.assertEqual(self.doc.name, 'mine.txt')
        self.assertEqual(self.doc.folder_id, self.other_folder)
        self.assertEqual(self.doc.lock_uid, self.alice)

    def test_a_released_document_is_free_again(self):
        self._as(self.alice).action_unlock()
        self._as(self.bob).write({'name': 'free.txt'})
        self.assertEqual(self.doc.name, 'free.txt')

    def test_a_user_who_cannot_write_gets_the_ordinary_answer(self):
        for user in (self.carol, self.nobody):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self._as(user).write({'name': 'x.txt'})

    def test_other_changes_are_not_held_back(self):
        self._as(self.bob).message_post(body='A note', message_type='comment')
        self._as(self.bob).message_subscribe(partner_ids=[self.bob.partner_id.id])
        self.env['mail.activity'].with_user(self.bob).create({
            'res_model_id': self.env['ir.model']._get_id('woow.document'),
            'res_id': self.doc.id, 'summary': 'Look at this',
            'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
            'user_id': self.bob.id})
        self._as(self.dave).write({'shared_user_ids': [Command.link(self.carol.id)]})
        self.assertIn(self.carol, self.doc.shared_user_ids)
        self.assertEqual(self.doc.lock_uid, self.alice)

    def test_the_folder_form_cannot_change_a_locked_document_either(self):
        with self.assertRaises(UserError):
            self.folder.with_user(self.dave).write({
                'document_ids': [Command.update(self.doc.id, {'name': 'viafolder.txt'})]})
        self.assertEqual(self.doc.name, 'report.txt')

    def test_the_move_wizard_is_refused_for_a_second_user(self):
        wizard = self.env['woow.document.move.wizard'].with_user(self.bob).create({
            'document_ids': [Command.set(self.doc.ids)],
            'target_folder_id': self.other_folder.id})
        with self.assertRaises(UserError):
            wizard.action_move()
        self.assertEqual(self.doc.folder_id, self.folder)
        mine = self.env['woow.document.move.wizard'].with_user(self.alice).create({
            'document_ids': [Command.set(self.doc.ids)],
            'target_folder_id': self.other_folder.id})
        mine.action_move()
        self.assertEqual(self.doc.folder_id, self.other_folder)

    def test_a_batch_move_with_one_locked_document_moves_nothing(self):
        free = self._doc('free.txt', b'free')
        wizard = self.env['woow.document.move.wizard'].with_user(self.bob).create({
            'document_ids': [Command.set((free | self.doc).ids)],
            'target_folder_id': self.other_folder.id})
        with self.assertRaises(UserError):
            wizard.action_move()
        self.assertEqual(free.folder_id, self.folder)

    def test_trashing_is_refused_for_a_second_user_and_allowed_for_the_holder(self):
        for user in (self.bob, self.dave, self.admin):
            with self.subTest(user=user.login), self.assertRaises(UserError):
                self._as(user).action_trash()
        self.assertTrue(self.doc.active)
        self.assertTrue(self._as(self.alice).action_trash())
        self.assertFalse(self.doc.active)

    def test_restoring_is_refused_for_a_second_user(self):
        self._as(self.alice).action_trash()
        for user in (self.dave, self.admin):
            with self.subTest(user=user.login), self.assertRaises(UserError):
                self.doc.with_user(user).with_context(active_test=False).action_restore()
        self.assertFalse(self.doc.active)
        self.doc.with_user(self.alice).with_context(active_test=False).action_restore()
        self.assertTrue(self.doc.active)

    def test_the_restore_wizard_does_not_move_a_locked_document(self):
        self._as(self.alice).action_trash()
        wizard = self.env['woow.document.restore.wizard'].with_user(self.admin).create({
            'document_ids': [Command.set(self.doc.ids)],
            'target_folder_id': self.other_folder.id})
        with self.assertRaises(UserError):
            wizard.action_restore_to()
        self.assertEqual(self.doc.folder_id, self.folder)
        self.assertFalse(self.doc.active)
        self._as(self.alice).action_unlock()
        wizard.action_restore_to()
        self.assertTrue(self.doc.active)
        self.assertEqual(self.doc.folder_id, self.other_folder)

    def test_a_single_locked_document_cannot_be_purged_by_another_user(self):
        self._as(self.alice).action_trash()
        wizard = self.env['woow.document.purge.wizard'].with_user(self.admin).create({
            'document_ids': [Command.set(self.doc.ids)], 'confirm': True})
        with self.assertRaises(UserError):
            wizard.action_confirm_purge()
        with self.assertRaises(UserError):
            self.doc.with_user(self.admin).with_context(active_test=False).unlink()
        self.assertEqual(self.doc.with_user(self.admin).with_context(
            active_test=False)._woow_purge(), (0, 1))
        self.assertTrue(self.doc.exists())
        self._as(self.alice).action_unlock()
        wizard.action_confirm_purge()
        self.assertFalse(self.doc.exists())

    def test_the_superuser_and_crons_are_not_held_back(self):
        self.doc.sudo().write({'name': 'cron.txt'})
        self.assertEqual(self.doc.name, 'cron.txt')
        self.assertTrue(self.doc.sudo().action_trash())
        self.assertEqual(self.doc.sudo().with_context(active_test=False)._woow_purge(), (1, 0))
        self.assertFalse(self.doc.exists())


@tagged('woow_docmgmt_core')
class TestFolderOperationsIgnoreDocumentLocks(DocActionsCase):

    def setUp(self):
        super().setUp()
        self.child = self.env['woow.document.folder'].sudo().create({
            'name': 'Locked child', 'parent_id': self.folder.id, 'owner_id': self.admin.id})
        self.locked = self._doc('inside.txt', b'inside', folder=self.child)
        self.locked.with_user(self.alice).action_lock()

    def test_a_folder_is_trashed_restored_and_moved_around_its_locked_documents(self):
        target = self.env['woow.document.folder'].sudo().create({
            'name': 'Move target', 'owner_id': self.admin.id})
        self.child.with_user(self.admin).write({'parent_id': target.id})
        self.assertEqual(self.child.parent_id, target)
        wizard = self.env['woow.document.move.wizard'].with_user(self.admin).create({
            'folder_ids': [Command.set(self.child.ids)], 'target_folder_id': self.folder.id})
        wizard.action_move()
        self.assertEqual(self.child.parent_id, self.folder)
        self.assertTrue(self.child.with_user(self.admin).action_trash())
        self.assertTrue(self.locked.folder_in_trash)
        self.assertTrue(self.child.with_user(self.admin).with_context(
            active_test=False).action_restore())
        self.assertFalse(self.locked.folder_in_trash)
        self.assertEqual(self.locked.lock_uid, self.alice)

    def test_a_trashed_folder_is_deleted_with_its_locked_documents(self):
        self.child.with_user(self.admin).action_trash()
        self.assertEqual(self.child.with_user(self.admin).with_context(
            active_test=False)._woow_purge(), (1, 0))
        self.assertFalse(self.locked.exists())
        self.assertFalse(self.child.exists())

    def test_the_folder_purge_marker_cannot_be_sent_by_a_client(self):
        self.locked.with_user(self.alice).action_trash()
        client = self.locked.with_user(self.admin).with_context(
            active_test=False, woow_folder_purge=True)
        with self.assertRaises(UserError):
            client.unlink()
        with self.assertRaises(UserError):
            self.locked.with_user(self.bob).with_context(
                woow_folder_purge='1', active_test=False).write({'name': 'x.txt'})


@tagged('woow_docmgmt_core')
class TestDuplicate(DocActionsCase):

    def setUp(self):
        super().setUp()
        self.doc.sudo().write({
            'tag_ids': [Command.link(self.tag.id)], 'description': '<p>Describe me</p>',
            'partner_id': self.portal.partner_id.id})
        self.env.flush_all()

    def test_the_copy_is_a_new_file_owned_by_the_caller(self):
        action = self._as(self.bob).action_duplicate()
        copy = self.env['woow.document'].browse(action['res_id'])
        self.assertEqual(action['res_model'], 'woow.document')
        self.assertNotEqual(copy, self.doc)
        self.assertEqual(copy.name, 'report (copy).txt')
        self.assertEqual(copy.folder_id, self.folder)
        self.assertEqual(copy.owner_id, self.bob)
        self.assertEqual(copy.tag_ids, self.tag)
        self.assertIn('Describe me', copy.description)
        self.assertEqual(base64.b64decode(copy.datas), b'actions content')
        self.assertEqual(copy.document_type, 'binary')
        self.assertEqual(copy.mimetype, self.doc.mimetype)
        self.assertEqual(len(self._events('duplicate', copy)), 1)
        self.assertEqual(self._events('duplicate', copy).detail, {'source_id': self.doc.id})
        self.assertEqual(self._events('duplicate', copy).user_id, self.bob)
        # the source is untouched
        self.assertEqual(self.doc.name, 'report.txt')
        self.assertEqual(self.doc.owner_id, self.alice)

    def test_what_belongs_to_the_source_is_not_copied(self):
        self.doc.sudo().write({'shared_user_ids': [Command.link(self.carol.id)],
                               'legal_hold': True})
        self.doc.with_user(self.alice).message_post(body='Source chatter', message_type='comment')
        self.doc.with_user(self.alice).action_lock()
        copy = self.env['woow.document'].browse(
            self._as(self.bob).action_duplicate()['res_id'])
        self.assertFalse(copy.sudo().shared_user_ids)
        self.assertFalse(copy.sudo().shared_partner_ids)
        self.assertFalse(copy.legal_hold)
        self.assertFalse(copy.lock_uid)
        self.assertFalse(copy.lock_date)
        self.assertFalse(copy.partner_id)
        self.assertNotIn('Source chatter', ''.join(copy.message_ids.mapped('body')))
        self.assertTrue(self.doc.lock_uid)
        self.assertTrue(self.doc.legal_hold)

    def test_a_link_is_copied_with_its_address(self):
        link = self.env['woow.document'].sudo().create({
            'name': 'Docs', 'folder_id': self.folder.id, 'document_type': 'url',
            'url': 'https://example.com/docs'})
        copy = self.env['woow.document'].browse(
            self._as(self.bob, link).action_duplicate()['res_id'])
        self.assertEqual(copy.document_type, 'url')
        self.assertEqual(copy.url, 'https://example.com/docs')
        self.assertEqual(copy.name, 'Docs (copy)')
        self.assertFalse(copy.datas)

    def test_a_file_without_an_extension_gets_the_suffix_at_the_end(self):
        self.assertEqual(copy_name('notes', '(copy)'), 'notes (copy)')
        self.assertEqual(copy_name('a.b.pdf', '(copy)'), 'a.b (copy).pdf')
        self.assertEqual(copy_name('.hidden', '(copy)'), '.hidden (copy)')
        self.assertEqual(copy_name('x' * 300 + '.txt', '(copy)')[-11:], ' (copy).txt')
        self.assertLessEqual(len(copy_name('x' * 300 + '.txt', '(copy)')), 200)
        self.assertEqual(copy_name('Report', '(copy)', keep_extension=False), 'Report (copy)')

    def test_rights_of_the_caller(self):
        count = self.env['woow.document'].sudo().search_count([])
        # a reader may see the file but not create in its folder; a stranger does not see it
        for user in (self.carol, self.nobody, self.portal):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self._as(user).action_duplicate()
        self.assertEqual(self.env['woow.document'].sudo().search_count([]), count)
        # an upload-only member may read the file and create in the folder: the copy is theirs
        copy = self.env['woow.document'].browse(self._as(self.uma).action_duplicate()['res_id'])
        self.assertEqual(copy.owner_id, self.uma)
        self.assertEqual(self.env['woow.document'].sudo().search_count([]), count + 1)

    def test_a_trashed_document_cannot_be_duplicated(self):
        self.doc.with_user(self.alice).action_trash()
        with self.assertRaises(AccessError):
            self.doc.with_user(self.alice).with_context(active_test=False).action_duplicate()

    def test_a_document_locked_by_another_user_can_still_be_copied(self):
        self._as(self.alice).action_lock()
        copy = self.env['woow.document'].browse(self._as(self.bob).action_duplicate()['res_id'])
        self.assertFalse(copy.lock_uid)

    def test_a_batch_makes_one_copy_each_and_is_bounded(self):
        docs = self.doc | self._doc('second.txt', b'second content')
        action = docs.with_user(self.bob).action_duplicate()
        copies = self.env['woow.document'].search(action['domain'])
        self.assertEqual(len(copies), 2)
        self.assertEqual(sorted(copies.mapped('name')),
                         ['report (copy).txt', 'second (copy).txt'])
        self.assertEqual(action['view_mode'], 'list,form')
        many = self.env['woow.document']
        for index in range(DUPLICATE_MAX_PER_CALL + 1):
            many |= self._doc('batch%02d.txt' % index, b'batch %d' % index)
        before = self.env['woow.document'].sudo().search_count([])
        with self.assertRaises(UserError):
            many.with_user(self.bob).action_duplicate()
        self.assertEqual(self.env['woow.document'].sudo().search_count([]), before)
        exactly = many[:DUPLICATE_MAX_PER_CALL]
        exactly.with_user(self.bob).action_duplicate()
        self.assertEqual(self.env['woow.document'].sudo().search_count([]),
                         before + DUPLICATE_MAX_PER_CALL)

    def test_an_empty_selection_is_refused(self):
        with self.assertRaises(UserError):
            self.env['woow.document'].with_user(self.bob).action_duplicate()

    def test_a_copy_follows_the_quota_and_a_refused_batch_leaves_nothing(self):
        big = self._doc('big.txt', b'q' * (1536 * 1024))
        self.env['ir.config_parameter'].sudo().set_param(_PARAM + 'quota_mb', '1')
        # the fixtures took the member-row locks of this transaction; a request starts without them
        locks._reset_tracker(self.env.cr)
        before = self.env['woow.document'].sudo().search_count([])
        with self.assertRaises(WoowUploadRejected) as caught:
            (self.doc | big).with_user(self.bob).action_duplicate()
        self.assertEqual(caught.exception.code, 'quota')
        self.assertEqual(self.env['woow.document'].sudo().search_count([]), before)

    def test_a_copy_follows_the_upload_policy(self):
        big = self._doc('big.txt', b'p' * (1536 * 1024))
        self.env['ir.config_parameter'].sudo().set_param(_PARAM + 'internal_max_upload_mb', '1')
        with self.assertRaises(WoowUploadRejected) as caught:
            self._as(self.bob, big).action_duplicate()
        self.assertEqual(caught.exception.code, 'size')

    def test_a_document_without_content_is_refused_cleanly(self):
        attachment = self.doc.sudo().attachment_id
        self.env.cr.execute('UPDATE woow_document SET attachment_id = NULL WHERE id = %s',
                            (self.doc.id,))
        attachment.sudo().unlink()
        self.env.invalidate_all()
        self.assertFalse(self.doc.sudo().datas)
        with self.assertRaises(UserError):
            self._as(self.bob).action_duplicate()


@tagged('woow_docmgmt_core')
class TestRename(DocActionsCase):

    def _wizard(self, user, **vals):
        return self.env['woow.document.rename.wizard'].with_user(user).create(vals)

    def test_an_editor_renames_a_document_with_a_normal_write(self):
        action = self._as(self.alice).action_open_rename_wizard()
        self.assertEqual(action['res_model'], 'woow.document.rename.wizard')
        self.assertEqual(action['context'], {'default_document_id': self.doc.id})
        wizard = self._wizard(self.alice, document_id=self.doc.id, name='  renamed.txt ')
        self.assertEqual(wizard.action_rename()['type'], 'ir.actions.act_window_close')
        self.assertEqual(self.doc.name, 'renamed.txt')

    def test_the_form_is_prefilled_with_the_current_name(self):
        wizard = self.env['woow.document.rename.wizard'].with_user(self.alice).with_context(
            default_document_id=self.doc.id)
        self.assertEqual(wizard.default_get(['name'])['name'], 'report.txt')
        folder = self.env['woow.document.rename.wizard'].with_user(self.alice).with_context(
            default_folder_id=self.folder.id)
        self.assertEqual(folder.default_get(['name'])['name'], 'Actions folder')

    def test_the_prefill_does_not_tell_the_name_of_a_hidden_document(self):
        wizard = self.env['woow.document.rename.wizard'].with_user(self.nobody).with_context(
            default_document_id=self.doc.id)
        self.assertFalse(wizard.default_get(['name']).get('name'))
        wizard = self.env['woow.document.rename.wizard'].with_user(self.nobody).with_context(
            default_folder_id=self.folder.id)
        self.assertFalse(wizard.default_get(['name']).get('name'))

    def test_a_reader_and_a_stranger_cannot_rename(self):
        for user in (self.carol, self.nobody):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self._as(user).action_open_rename_wizard()
                wizard = self._wizard(user, document_id=self.doc.id, name='hijack.txt')
                with self.assertRaises(AccessError):
                    wizard.action_rename()
        self.assertEqual(self.doc.name, 'report.txt')

    def test_a_locked_document_is_not_renamed_by_a_second_user(self):
        self._as(self.alice).action_lock()
        wizard = self._wizard(self.bob, document_id=self.doc.id, name='other.txt')
        with self.assertRaises(UserError):
            wizard.action_rename()
        self._wizard(self.alice, document_id=self.doc.id, name='holder.txt').action_rename()
        self.assertEqual(self.doc.name, 'holder.txt')

    def test_the_name_rules_of_the_document_apply(self):
        for name, code in (('report.exe', 'extension'), ('report.pdf', 'mime')):
            with self.subTest(name=name):
                wizard = self._wizard(self.alice, document_id=self.doc.id, name=name)
                with self.assertRaises(WoowUploadRejected) as caught:
                    wizard.action_rename()
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.doc.name, 'report.txt')
        self._wizard(self.alice, document_id=self.doc.id, name='a/../b.txt').action_rename()
        self.assertEqual(self.doc.name, 'b.txt')

    def test_an_empty_name_is_refused(self):
        wizard = self._wizard(self.alice, document_id=self.doc.id, name='   ')
        with self.assertRaises(UserError):
            wizard.action_rename()
        self.assertEqual(self.doc.name, 'report.txt')

    def test_exactly_one_target_is_needed(self):
        for vals in ({}, {'document_id': self.doc.id, 'folder_id': self.folder.id}):
            with self.subTest(vals=sorted(vals)):
                wizard = self._wizard(self.alice, name='x', **vals)
                with self.assertRaises(UserError):
                    wizard.action_rename()
        self.assertEqual(self.doc.name, 'report.txt')
        self.assertEqual(self.folder.name, 'Actions folder')

    def test_a_trashed_document_cannot_be_renamed(self):
        self._as(self.alice).action_trash()
        wizard = self._wizard(self.alice, document_id=self.doc.id, name='gone.txt')
        with self.assertRaises(AccessError):
            wizard.action_rename()

    def test_a_folder_is_renamed_by_an_editor_and_by_nobody_less(self):
        action = self.folder.with_user(self.alice).action_open_rename_wizard()
        self.assertEqual(action['context'], {'default_folder_id': self.folder.id})
        self._wizard(self.alice, folder_id=self.folder.id, name='Renamed folder').action_rename()
        self.assertEqual(self.folder.name, 'Renamed folder')
        for user in (self.uma, self.carol, self.nobody):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self.folder.with_user(user).action_open_rename_wizard()
                with self.assertRaises(AccessError):
                    self._wizard(user, folder_id=self.folder.id, name='Hijacked').action_rename()
        self.assertEqual(self.folder.name, 'Renamed folder')

    def test_a_folder_name_that_is_taken_is_refused(self):
        self.env['woow.document.folder'].sudo().create({
            'name': 'Taken', 'owner_id': self.admin.id})
        wizard = self._wizard(self.admin, folder_id=self.folder.id, name='Taken')
        with self.assertRaises(UserError):
            wizard.action_rename()
        self.assertEqual(self.folder.name, 'Actions folder')

    def test_portal_users_cannot_use_the_wizard(self):
        with self.assertRaises(AccessError):
            self._wizard(self.portal, document_id=self.doc.id, name='x.txt')
        with self.assertRaises(AccessError):
            self._as(self.portal).action_open_rename_wizard()


@tagged('woow_docmgmt_backend')
class TestDocActionsViews(DocActionsCase):

    def test_the_form_shows_the_buttons_and_the_lock_state(self):
        arch = self.env['woow.document'].with_user(self.alice).get_view(view_type='form')['arch']
        for name in ('action_lock', 'action_unlock', 'action_duplicate',
                     'action_open_rename_wizard', 'lock_uid', 'is_locked', 'woow_locked_by_other'):
            self.assertIn(name, arch)

    def test_the_list_has_a_lock_indicator_and_the_search_a_filter(self):
        arch = self.env['woow.document'].with_user(self.alice).get_view(view_type='list')['arch']
        self.assertIn('is_locked', arch)
        search = self.env['woow.document'].with_user(self.alice).get_view(
            view_type='search')['arch']
        self.assertIn('woow_locked', search)

    def test_the_folder_form_offers_rename(self):
        arch = self.env['woow.document.folder'].with_user(self.alice).get_view(
            view_type='form')['arch']
        self.assertIn('action_open_rename_wizard', arch)

    def test_the_rename_form_loads(self):
        arch = self.env['woow.document.rename.wizard'].with_user(self.alice).get_view(
            view_type='form')['arch']
        self.assertIn('action_rename', arch)
