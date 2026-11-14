# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Release 18.0.2, lane ``sharing``: roles on one document and access that ends by itself.

Covers who may change the rows of ``woow.document.share``, the rows and the stored lists that follow
them (in both directions), what a viewer and an editor by share can do, the 30 minute cron that ends
expired folder members and shares, the two sharing dialogs and the upgrade from 18.0.1.
"""
import base64
import importlib.util
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models import share as share_module
from ..models import sharing as sharing_module
from ..services.constants import WOOW_MODELS

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_OPEN_ERRORS = UserError      # AccessError and ValidationError are UserErrors


class _SharingCase(TransactionCase):
    """A folder with an owner (manage level), a document in it, and one account of every kind."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.owner = new_test_user(env, login='shr_owner', groups=_USER)
        cls.manager = new_test_user(env, login='shr_manager', groups=_MANAGER)
        cls.edit_member = new_test_user(env, login='shr_edit_member', groups=_USER)
        cls.upload_owner = new_test_user(env, login='shr_upload_owner', groups=_USER)
        cls.read_member = new_test_user(env, login='shr_read_member', groups=_USER)
        cls.plain = new_test_user(env, login='shr_plain', groups=_USER)
        cls.viewer = new_test_user(env, login='shr_viewer', groups=_USER)
        cls.editor = new_test_user(env, login='shr_editor', groups=_USER)
        cls.target = new_test_user(env, login='shr_target', groups=_USER)
        cls.outsider = new_test_user(env, login='shr_outsider', groups='base.group_user')
        cls.portal = new_test_user(env, login='shr_portal', groups='base.group_portal')
        cls.portal_two = new_test_user(env, login='shr_portal_two', groups='base.group_portal')
        cls.contact = env['res.partner'].sudo().create({'name': 'Share contact without account'})
        folders = env['woow.document.folder'].sudo()
        cls.folder = folders.create({'name': 'Sharing folder', 'owner_id': cls.owner.id})
        cls.other_folder = folders.create({'name': 'Sharing other folder',
                                           'owner_id': cls.owner.id})
        access = env['woow.document.folder.access'].sudo()
        access.create([
            {'folder_id': cls.folder.id, 'subject_type': 'user', 'user_id': cls.edit_member.id,
             'level': 'edit'},
            {'folder_id': cls.folder.id, 'subject_type': 'user', 'user_id': cls.upload_owner.id,
             'level': 'upload'},
            {'folder_id': cls.folder.id, 'subject_type': 'user', 'user_id': cls.read_member.id,
             'level': 'read'},
            {'folder_id': cls.other_folder.id, 'subject_type': 'user', 'user_id': cls.editor.id,
             'level': 'upload'},
            {'folder_id': cls.other_folder.id, 'subject_type': 'user',
             'user_id': cls.edit_member.id, 'level': 'upload'},
        ])
        cls.document = cls._document(cls.folder, 'shared.txt', owner=cls.upload_owner)
        cls.shares = env['woow.document.share']

    @classmethod
    def _document(cls, folder, name, owner=None):
        values = {'name': name, 'folder_id': folder.id, 'datas': base64.b64encode(b'sharing ' + name.encode())}
        if owner:
            values['owner_id'] = owner.id
        return cls.env['woow.document'].sudo().create(values)

    def _row(self, document=None, user=None, partner=None, role='viewer', expiry=None, as_user=None):
        values = {'document_id': (document or self.document).id, 'role': role}
        if user:
            values.update(subject_type='user', user_id=user.id)
        else:
            values.update(subject_type='partner', partner_id=partner.id)
        if expiry:
            values['expiry_date'] = expiry
        return self.shares.with_user(as_user).create(values) if as_user else \
            self.shares.sudo().create(values)

    def _attempt(self, action):
        """Whether ``action`` goes through (in a savepoint, so that a refusal changes nothing)."""
        try:
            with self.env.cr.savepoint():
                action()
        except _OPEN_ERRORS:
            return False
        return True

    def _expire(self, rows):
        """Move the end date of rows into the past, below the check that stops a user from doing it."""
        yesterday = fields.Date.today() - timedelta(days=1)
        self.env.flush_all()
        self.env.cr.execute('UPDATE %s SET expiry_date = %%s WHERE id IN %%s' % rows._table,
                            (yesterday, tuple(rows.ids)))
        self.env.invalidate_all()

    def _can_read(self, user, document=None):
        self.env.flush_all()
        return bool((document or self.document).with_user(user).search_count(
            [('id', '=', (document or self.document).id)]))

    def _can_write(self, user, document=None):
        self.env.flush_all()
        document = (document or self.document).with_user(user)
        return bool(document._filtered_access('write'))


@tagged('woow_docmgmt_core')
class TestShareRows(_SharingCase):

    def test_the_model_is_one_of_the_module_models(self):
        self.assertIn('woow.document.share', WOOW_MODELS)

    def test_who_may_change_rows_is_who_may_change_the_lists(self):
        """Parity with 18.0.1: a caller may create, change and delete a row exactly when the same
        caller may add a person to ``shared_user_ids`` of that document."""
        actors = [('owner', self.owner, True), ('manager', self.manager, True),
                  ('edit member', self.edit_member, False),
                  ('upload owner', self.upload_owner, False),
                  ('read member', self.read_member, False), ('plain', self.plain, False),
                  ('viewer by share', self.viewer, False), ('editor by share', self.editor, False),
                  ('outsider', self.outsider, False), ('portal', self.portal, False)]
        self._row(user=self.viewer)
        self._row(user=self.editor, role='editor')
        for label, actor, expected in actors:
            with self.subTest(actor=label):
                fresh = new_test_user(self.env, login='shr_fresh_%s' % label.replace(' ', '_'),
                                      groups=_USER)
                other = new_test_user(self.env, login='shr_fresh2_%s' % label.replace(' ', '_'),
                                      groups=_USER)
                via_list = self._attempt(lambda: self.document.with_user(actor).write(
                    {'shared_user_ids': [Command.link(fresh.id)]}))
                via_row = self._attempt(lambda: self.shares.with_user(actor).create({
                    'document_id': self.document.id, 'subject_type': 'user',
                    'user_id': other.id}))
                self.assertEqual(via_list, expected, 'list')
                self.assertEqual(via_row, expected, 'row')
        # change and delete: the same people
        row = self._row(user=self.target)
        for label, actor, expected in actors:
            with self.subTest(actor=label, what='write and unlink'):
                changed = self._attempt(lambda: row.with_user(actor).write({'role': 'editor'}))
                removed = self._attempt(lambda: self.shares.browse(row.id).with_user(actor).unlink())
                self.assertEqual((changed, removed), (expected, expected))
                if expected and removed:
                    row = self._row(user=self.target)

    def test_a_share_gives_no_management_right(self):
        self._row(user=self.editor, role='editor')
        self._row(user=self.viewer)
        for user in (self.editor, self.viewer):
            with self.subTest(user=user.login):
                self.assertFalse(self.folder.with_user(user)._woow_has_rank(4))
                with self.assertRaises(AccessError):
                    self.shares.with_user(user).create({
                        'document_id': self.document.id, 'subject_type': 'user',
                        'user_id': self.target.id})
                with self.assertRaises(AccessError):
                    self.document.with_user(user).write({'shared_user_ids': [Command.clear()]})
                with self.assertRaises(AccessError):
                    self.document.with_user(user).write({'shared_partner_ids': [
                        Command.link(self.portal.partner_id.id)]})
                with self.assertRaises(AccessError):
                    self.document.with_user(user).write({'legal_hold': True})

    def test_the_people_a_row_may_name(self):
        for label, values in (
                ('a user who is not a document user',
                 {'subject_type': 'user', 'user_id': self.outsider.id}),
                ('a contact without an account',
                 {'subject_type': 'partner', 'partner_id': self.contact.id}),
                ('a portal contact as editor',
                 {'subject_type': 'partner', 'partner_id': self.portal.partner_id.id,
                  'role': 'editor'}),
                ('a user and a contact at once',
                 {'subject_type': 'user', 'user_id': self.target.id,
                  'partner_id': self.portal.partner_id.id}),
                ('nobody', {'subject_type': 'user'})):
            with self.subTest(label), self.assertRaises(_OPEN_ERRORS), self.env.cr.savepoint():
                self.shares.with_user(self.owner).create({'document_id': self.document.id, **values})
        # a portal contact whose account is archived is not one with an active account
        self.portal_two.sudo().active = False
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._row(partner=self.portal_two.partner_id, as_user=self.owner)
        row = self._row(partner=self.portal.partner_id, as_user=self.owner)
        self.assertEqual(row.role, 'viewer')
        with self.assertRaises(_OPEN_ERRORS), self.env.cr.savepoint():
            row.with_user(self.owner).write({'role': 'editor'})

    def test_a_person_has_one_row_and_a_document_has_a_limit(self):
        self._row(user=self.target, as_user=self.owner)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._row(user=self.target, as_user=self.owner)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.shares.with_user(self.owner).create([
                {'document_id': self.document.id, 'subject_type': 'user', 'user_id': self.viewer.id},
                {'document_id': self.document.id, 'subject_type': 'user',
                 'user_id': self.viewer.id}])
        other = self._row(user=self.viewer, as_user=self.owner)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            other.with_user(self.owner).write({'user_id': self.target.id})
        with patch.object(share_module, 'SHARE_ROWS_PER_DOCUMENT_MAX', 2):
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self._row(user=self.editor, as_user=self.owner)
            # the mirror of a direct change of the lists has the same limit
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.document.sudo().write({'shared_user_ids': [Command.link(self.editor.id)]})

    def test_the_server_columns_and_the_document_are_not_the_callers_to_set(self):
        row = self._row(user=self.target, as_user=self.owner)
        with self.assertRaises(AccessError):
            row.with_user(self.owner).write({'company_id': False})
        with self.assertRaises(AccessError):
            row.with_user(self.owner).write({'folder_id': self.other_folder.id})
        with self.assertRaises(UserError):
            row.with_user(self.owner).write({'document_id': self._document(
                self.folder, 'second.txt').id})
        with self.assertRaises(AccessError):
            self.shares.with_user(self.owner).create({
                'document_id': self.document.id, 'subject_type': 'user',
                'user_id': self.viewer.id, 'folder_id': self.other_folder.id})
        # a value that is not an id is refused, as in the other guards of the module
        for bad in ([self.target.id], 'x', 0, -4, 1.5, {'id': 1}):
            with self.subTest(bad=bad), self.assertRaises(_OPEN_ERRORS), self.env.cr.savepoint():
                self.shares.with_user(self.owner).create({
                    'document_id': self.document.id, 'subject_type': 'user', 'user_id': bad})

    def test_a_context_flag_from_a_client_is_not_the_server_marker(self):
        for flag in (True, 1, 'woow_share_sync', object):
            with self.subTest(flag=flag):
                with self.assertRaises(AccessError), self.env.cr.savepoint():
                    self.shares.with_user(self.plain).with_context(woow_share_sync=flag).create({
                        'document_id': self.document.id, 'subject_type': 'user',
                        'user_id': self.target.id})
                with self.assertRaises(ValidationError), self.env.cr.savepoint():
                    self.shares.with_user(self.owner).with_context(woow_share_sync=flag).create({
                        'document_id': self.document.id, 'subject_type': 'user',
                        'user_id': self.outsider.id})

    def test_a_stored_default_does_not_choose_the_role_or_the_end_date(self):
        defaults = self.env['ir.default'].with_user(self.owner)
        defaults.set('woow.document.share', 'role', 'editor', user_id=True)
        defaults.set('woow.document.share', 'expiry_date',
                     str(fields.Date.today() + timedelta(days=9)), user_id=True)
        row = self.shares.with_user(self.owner).create({
            'document_id': self.document.id, 'subject_type': 'user', 'user_id': self.target.id})
        self.assertEqual((row.role, row.expiry_date), ('viewer', False))
        self.assertNotIn(self.target, self.document.shared_editor_user_ids)

    def test_rows_cannot_be_reached_by_portal_users(self):
        self._row(partner=self.portal.partner_id)
        self.assertFalse(self.shares.with_user(self.portal).check_access_rights(
            'read', raise_exception=False))
        for call in (lambda: self.shares.with_user(self.portal).search([]),
                     lambda: self.document.with_user(self.portal).share_ids,
                     lambda: self.document.with_user(self.portal).search_count(
                         [('share_ids.partner_id', '=', self.portal.partner_id.id)]),
                     lambda: self.document.with_user(self.portal).shared_editor_user_ids,
                     lambda: self.document.with_user(self.portal).search_count(
                         [('shared_editor_user_ids', 'in', [self.editor.id])])):
            with self.assertRaises(AccessError):
                call()
        # the contact still reads the document that was shared with it
        self.assertTrue(self._can_read(self.portal))

    def test_rows_follow_the_document_that_the_caller_reads(self):
        row = self._row(user=self.viewer)
        self.assertEqual(self.shares.with_user(self.read_member).search([]), row)
        self.assertEqual(self.shares.with_user(self.viewer).search([]), row)
        self.assertFalse(self.shares.with_user(self.plain).search([]))
        self.assertEqual(self.shares.with_user(self.outsider).check_access_rights(
            'read', raise_exception=False), False)

    def test_rows_belong_to_the_company_of_their_document(self):
        row = self._row(user=self.viewer)
        self.assertEqual(row.company_id, self.document.company_id)
        self.assertEqual(row.folder_id, self.folder)
        company_b = self.env['res.company'].sudo().create({'name': 'Sharing company B'})
        folder_b = self.env['woow.document.folder'].sudo().create({
            'name': 'Sharing folder B', 'company_id': company_b.id, 'owner_id': False})
        document_b = self._document(folder_b, 'company-b.txt')
        row_b = self._row(document=document_b, user=self.viewer)
        self.assertEqual(row_b.company_id, company_b)
        manager = new_test_user(self.env, login='shr_company_a_manager', groups=_MANAGER)
        self.assertIn(row, self.shares.with_user(manager).search([]))
        self.assertNotIn(row_b, self.shares.with_user(manager).search([]))
        # the document moves, and its rows follow
        self.document.sudo().write({'folder_id': self.other_folder.id})
        self.assertEqual(row.folder_id, self.other_folder)


@tagged('woow_docmgmt_core')
class TestListsFollowRows(_SharingCase):

    def lists(self, document=None):
        document = (document or self.document).sudo()
        self.env.flush_all()
        return (sorted(document.shared_user_ids.ids), sorted(document.shared_partner_ids.ids),
                sorted(document.shared_editor_user_ids.ids))

    def test_rows_build_the_three_lists(self):
        viewer = self._row(user=self.viewer)
        editor = self._row(user=self.editor, role='editor')
        contact = self._row(partner=self.portal.partner_id)
        self.assertEqual(self.lists(), (sorted([self.viewer.id, self.editor.id]),
                                        [self.portal.partner_id.id], [self.editor.id]))
        editor.sudo().write({'role': 'viewer'})
        self.assertEqual(self.lists()[2], [])
        self.assertIn(self.editor.id, self.lists()[0])
        viewer.sudo().write({'role': 'editor'})
        self.assertEqual(self.lists()[2], [self.viewer.id])
        viewer.sudo().write({'user_id': self.target.id})
        self.assertEqual(self.lists()[0], sorted([self.target.id, self.editor.id]))
        self.assertEqual(self.lists()[2], [self.target.id])
        contact.sudo().unlink()
        self.assertEqual(self.lists()[1], [])
        (viewer | editor).sudo().unlink()
        self.assertEqual(self.lists(), ([], [], []))

    def test_the_lists_are_built_from_every_row_and_not_the_rows_the_caller_reads(self):
        """A manager of the folder who cannot read every row (the document is archived) still keeps
        the other people in the lists."""
        self._row(user=self.viewer)
        self._row(user=self.editor, role='editor')
        self.document.sudo().write({'active': False})
        self.env.flush_all()
        self.shares.with_user(self.owner).create({
            'document_id': self.document.id, 'subject_type': 'user', 'user_id': self.target.id})
        self.assertEqual(self.lists()[0], sorted([self.viewer.id, self.editor.id, self.target.id]))
        self.assertEqual(self.lists()[2], [self.editor.id])

    def test_a_direct_change_of_the_lists_is_mirrored_into_rows(self):
        document = self.document.sudo()
        document.write({'shared_user_ids': [Command.set([self.viewer.id, self.editor.id])],
                        'shared_partner_ids': [Command.set([self.portal.partner_id.id])]})
        rows = self.shares.sudo().search([('document_id', '=', document.id)])
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(rows.mapped('role')), {'viewer'})
        self.assertFalse(any(rows.mapped('expiry_date')))
        self.assertEqual(self.lists(), (sorted([self.viewer.id, self.editor.id]),
                                        [self.portal.partner_id.id], []))
        # a person who stays keeps the role and the end date; a person who leaves loses the row
        editor_row = rows.filtered(lambda row: row.user_id == self.editor)
        end = fields.Date.today() + timedelta(days=5)
        editor_row.write({'role': 'editor', 'expiry_date': end})
        document.write({'shared_user_ids': [Command.set([self.editor.id, self.target.id])]})
        self.assertEqual(editor_row.exists().role, 'editor')
        self.assertEqual(editor_row.expiry_date, end)
        users = self.shares.sudo().search([('document_id', '=', document.id),
                                           ('subject_type', '=', 'user')])
        self.assertEqual(users.user_id, self.editor | self.target)
        self.assertEqual(self.lists(), (sorted([self.editor.id, self.target.id]),
                                        [self.portal.partner_id.id], [self.editor.id]))
        # an editor who leaves the list leaves the editor list as well
        document.write({'shared_user_ids': [Command.unlink(self.editor.id)]})
        self.assertEqual(self.lists(), ([self.target.id], [self.portal.partner_id.id], []))
        document.write({'shared_partner_ids': [Command.clear()], 'shared_user_ids': [Command.clear()]})
        self.assertFalse(self.shares.sudo().search([('document_id', '=', document.id)]))
        self.assertEqual(self.lists(), ([], [], []))

    def _stored_users(self):
        """The users of ``shared_user_ids`` as the database holds them, archived ones included."""
        self.env.flush_all()
        self.env.cr.execute('SELECT user_id FROM woow_document_shared_user_rel '
                            'WHERE document_id = %s', (self.document.id,))
        return {row[0] for row in self.env.cr.fetchall()}

    def test_an_archived_user_keeps_the_entry_and_the_row(self):
        """The lists hold archived users too; a later change of the lists keeps them, and the end of
        a row takes them out."""
        self.document.sudo().write({
            'shared_user_ids': [Command.set([self.viewer.id, self.target.id])]})
        self.target.sudo().active = False
        self.document.sudo().write({'shared_user_ids': [Command.link(self.editor.id)]})
        rows = self.shares.sudo().search([('document_id', '=', self.document.id)])
        self.assertEqual(set(rows.user_id.ids), {self.viewer.id, self.target.id, self.editor.id})
        self.assertEqual(self._stored_users(), {self.viewer.id, self.target.id, self.editor.id})
        # the row of the archived user ends: the entry goes too, so it does not come back with the user
        rows.filtered(lambda row: row.user_id == self.target).unlink()
        self.assertEqual(self._stored_users(), {self.viewer.id, self.editor.id})
        self.target.sudo().active = True
        self.assertFalse(self._can_read(self.target))

    def test_the_mirror_works_for_the_people_who_may_change_the_lists(self):
        self.document.with_user(self.owner).write({
            'shared_user_ids': [Command.link(self.viewer.id)],
            'shared_partner_ids': [Command.link(self.portal.partner_id.id)]})
        rows = self.shares.sudo().search([('document_id', '=', self.document.id)])
        self.assertEqual(sorted(rows.mapped('subject_type')), ['partner', 'user'])
        self.assertTrue(self._can_read(self.viewer))
        self.assertTrue(self._can_read(self.portal))
        # the lists of 18.0.1 took any user or contact: they still do, as viewers
        public = new_test_user(self.env, login='shr_public', groups='base.group_public')
        self.document.sudo().write({'shared_partner_ids': [Command.link(public.partner_id.id)],
                                    'shared_user_ids': [Command.link(self.outsider.id)]})
        self.assertIn(public.partner_id, self.shares.sudo().search(
            [('document_id', '=', self.document.id)]).partner_id)
        self.assertIn(self.outsider, self.shares.sudo().search(
            [('document_id', '=', self.document.id)]).user_id)

    def test_a_document_made_with_lists_has_the_rows(self):
        document = self.env['woow.document'].sudo().create({
            'name': 'with-lists.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'with lists'), 'shared_user_ids': [Command.set([self.viewer.id])],
            'shared_partner_ids': [Command.set([self.portal.partner_id.id])]})
        rows = self.shares.sudo().search([('document_id', '=', document.id)])
        self.assertEqual((rows.user_id, rows.partner_id), (self.viewer, self.portal.partner_id))
        plain = self.env['woow.document'].sudo().create({
            'name': 'no-lists.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'no lists')})
        self.assertFalse(self.shares.sudo().search([('document_id', '=', plain.id)]))

    def test_the_rows_of_the_dialog_are_a_normal_write_of_the_document(self):
        document = self.document.with_user(self.owner)
        document.write({'share_ids': [Command.create({
            'subject_type': 'user', 'user_id': self.viewer.id, 'role': 'editor'})]})
        self.assertEqual(self.lists()[2], [self.viewer.id])
        row = self.document.sudo().share_ids
        document.write({'share_ids': [Command.update(row.id, {'role': 'viewer'})]})
        self.assertEqual(self.lists()[2], [])
        document.write({'share_ids': [Command.delete(row.id)]})
        self.assertEqual(self.lists(), ([], [], []))
        # the same write is refused to someone who may not change the shares
        with self.assertRaises(AccessError):
            self.document.with_user(self.edit_member).write({'share_ids': [Command.create({
                'subject_type': 'user', 'user_id': self.viewer.id})]})
        self.assertEqual(self.lists(), ([], [], []))

    def test_every_change_of_a_row_is_one_audit_row_with_ids_only(self):
        log = self.env['woow.document.log'].sudo()
        before = log.search_count([('event', '=', 'access_change'),
                                   ('document_id', '=', self.document.id)])
        row = self._row(user=self.target, as_user=self.owner)
        row.with_user(self.owner).write({'role': 'editor'})
        row.with_user(self.owner).unlink()
        entries = log.search([('event', '=', 'access_change'),
                              ('document_id', '=', self.document.id)], order='id')
        self.assertEqual(len(entries) - before, 3)
        for entry in entries[before:]:
            self.assertEqual(list(entry.detail), ['share_ids'])
            self.assertEqual(entry.detail['share_ids'], [row.id])
            self.assertEqual(entry.channel, 'rpc')


@tagged('woow_docmgmt_core')
class TestEditorAndViewerByShare(_SharingCase):

    def setUp(self):
        super().setUp()
        self.row_viewer = self._row(user=self.viewer)
        self.row_editor = self._row(user=self.editor, role='editor')
        self.tag = self.env['woow.document.tag'].sudo().create({'name': 'Share tag'})

    def test_a_viewer_reads_and_does_not_write(self):
        self.assertTrue(self._can_read(self.viewer))
        self.assertFalse(self._can_write(self.viewer))
        for vals in ({'name': 'viewer.txt'}, {'description': '<p>x</p>'},
                     {'datas': base64.b64encode(b'viewer content')},
                     {'tag_ids': [Command.set([self.tag.id])]}):
            with self.subTest(vals=list(vals)), self.assertRaises(AccessError):
                self.document.with_user(self.viewer).write(vals)
        with self.assertRaises(AccessError):
            self.document.with_user(self.viewer).action_trash()

    def test_an_editor_by_share_changes_the_document(self):
        self.assertTrue(self._can_write(self.editor))
        document = self.document.with_user(self.editor)
        document.write({'name': 'renamed.txt'})
        self.assertEqual(self.document.name, 'renamed.txt')
        document.write({'datas': base64.b64encode(b'new content')})
        self.assertEqual(base64.b64decode(self.document.datas), b'new content')
        document.write({'tag_ids': [Command.set([self.tag.id])], 'description': '<p>note</p>'})
        self.assertEqual(self.document.tag_ids, self.tag)
        document.action_trash()
        self.assertFalse(self.document.active)

    def test_an_editor_by_share_cannot_use_the_document_beyond_its_content(self):
        document = self.document.with_user(self.editor)
        self.assertTrue(self.other_folder.with_user(self.editor)._woow_has_rank(2))
        with self.assertRaises(AccessError):
            document.write({'owner_id': self.editor.id})
        with self.assertRaises(AccessError):
            document.write({'folder_id': self.other_folder.id})
        with self.assertRaises(AccessError):
            document.write({'shared_editor_user_ids': [Command.link(self.target.id)]})
        with self.assertRaises(AccessError):
            document.write({'legal_hold': True})
        with self.assertRaises(AccessError):
            document.action_set_legal_hold()
        with self.assertRaises(AccessError):
            self.folder.with_user(self.editor).write({'name': 'taken over'})
        with self.assertRaises(AccessError):
            self.shares.with_user(self.editor).browse(self.row_viewer.id).write({'role': 'editor'})
        with self.assertRaises(AccessError):
            self.shares.with_user(self.editor).browse(self.row_editor.id).unlink()
        self.assertEqual((self.document.folder_id, self.document.owner_id),
                         (self.folder, self.upload_owner))
        # the move wizard follows the same rule
        wizard = self.env['woow.document.move.wizard'].with_user(self.editor).create({
            'document_ids': [Command.set([self.document.id])],
            'target_folder_id': self.other_folder.id})
        with self.assertRaises(AccessError):
            wizard.action_move()
        self.assertEqual(self.document.folder_id, self.folder)

    def test_a_member_who_could_write_before_still_moves_the_document(self):
        """The share rule only adds a way to write; it takes nothing from the rules of 18.0.1."""
        self._row(user=self.edit_member, role='editor')
        self.document.with_user(self.edit_member).write({'folder_id': self.other_folder.id})
        self.assertEqual(self.document.folder_id, self.other_folder)
        self.document.with_user(self.owner).write({'folder_id': self.folder.id})

    def test_an_editor_by_share_is_not_given_the_owner_or_a_wider_folder_right(self):
        self.assertEqual(self.document.with_user(self.editor).shared_editor_user_ids,
                         self.editor)
        self.assertEqual(self.document.with_user(self.editor).woow_user_can_write, True)
        self.assertEqual(self.document.with_user(self.editor).woow_user_can_share, False)
        self.assertFalse(self.folder.with_user(self.editor)._woow_has_rank(1))
        self.assertFalse(self.env['woow.document.folder'].with_user(self.editor).search(
            [('id', '=', self.folder.id)]))
        other = self._document(self.folder, 'second-shared.txt')
        self.assertFalse(self._can_read(self.editor, other))
        self.assertFalse(self._can_write(self.editor, other))

    def test_the_editor_rule_does_not_reach_the_trash(self):
        self.document.sudo().write({'active': False})
        self.assertFalse(self.document.with_user(self.editor).with_context(
            active_test=False)._filtered_access('write'))
        self.document.sudo().write({'active': True})
        self.assertTrue(self._can_write(self.editor))
        self.folder.with_user(self.owner).action_trash()
        self.assertFalse(self.document.with_user(self.editor).with_context(
            active_test=False)._filtered_access('write'))
        self.assertFalse(self._can_read(self.editor))
        with self.assertRaises(AccessError):
            self.document.with_user(self.editor).write({'name': 'in a trashed folder.txt'})

    def test_rows_that_are_removed_take_the_rights_with_them(self):
        self.row_editor.with_user(self.owner).unlink()
        self.assertFalse(self._can_write(self.editor))
        self.assertFalse(self._can_read(self.editor))
        self.row_viewer.with_user(self.owner).unlink()
        self.assertFalse(self._can_read(self.viewer))

    def test_a_mirrored_list_change_that_removes_an_editor_removes_the_write_right(self):
        self.document.with_user(self.owner).write({
            'shared_user_ids': [Command.unlink(self.editor.id)]})
        self.assertFalse(self._can_write(self.editor))
        self.assertTrue(self._can_read(self.viewer))

    def test_the_rule_and_the_matrix_name_the_editor_list(self):
        rule = self.env.ref('woow_document_management.rule_woow_document_user_write')
        self.assertIn('shared_editor_user_ids', rule.domain_force)
        self.assertIn("('active', '=', True)", rule.domain_force)
        self.assertIn("('folder_in_trash', '=', False)", rule.domain_force)
        read = self.env.ref('woow_document_management.rule_woow_document_user_read')
        self.assertIn('shared_user_ids', read.domain_force)
        self.assertNotIn('shared_editor_user_ids', read.domain_force)

    def test_chatter_readers_follow_the_rows(self):
        """A person who is shared with is notified of the document; an expired one is not."""
        from ..models.mail_thread_guard import _readers_only
        users = self.viewer | self.editor | self.target
        self.document.sudo().message_subscribe(partner_ids=users.partner_id.ids)
        self.env.flush_all()
        subtype = self.env.ref('mail.mt_comment').id

        def notified():
            data = self.env['mail.followers']._get_recipient_data(
                self.document.sudo(), 'comment', subtype)[self.document.id]
            kept = _readers_only(
                self.document.with_user(self.env.ref('base.user_admin')),
                [data[user.partner_id.id] for user in users], self.env['mail.message'],
                {'partner_ids': []})
            return {item['id'] for item in kept}

        self.assertEqual(notified(), {self.viewer.partner_id.id, self.editor.partner_id.id})
        self.row_editor.with_user(self.owner).unlink()
        self.assertEqual(notified(), {self.viewer.partner_id.id})


@tagged('woow_docmgmt_core')
class TestEndDates(_SharingCase):

    def test_the_end_date_is_not_in_the_past(self):
        today = fields.Date.today()
        for days, ok in ((-1, False), (0, True), (3, True)):
            with self.subTest(days=days):
                values = {'document_id': self.document.id, 'subject_type': 'user',
                          'user_id': new_test_user(self.env, login='shr_end_%s' % (days + 1),
                                                   groups=_USER).id,
                          'expiry_date': today + timedelta(days=days)}
                with self.env.cr.savepoint():
                    if ok:
                        self.shares.with_user(self.owner).create(values)
                    else:
                        with self.assertRaises(ValidationError):
                            self.shares.with_user(self.owner).create(values)
        row = self._row(user=self.target)
        with self.assertRaises(ValidationError):
            row.with_user(self.owner).write({'expiry_date': today - timedelta(days=1)})
        row.with_user(self.owner).write({'expiry_date': today + timedelta(days=2)})
        row.with_user(self.owner).write({'expiry_date': False})
        line = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.target.id,
            'level': 'read'})
        with self.assertRaises(ValidationError):
            line.with_user(self.owner).write({'expiry_date': today - timedelta(days=1)})
        with self.assertRaises(ValidationError):
            self.env['woow.document.folder.access'].with_user(self.owner).create({
                'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.viewer.id,
                'level': 'read', 'expiry_date': today - timedelta(days=1)})
        line.with_user(self.owner).write({'expiry_date': today})
        self.assertEqual(line.expiry_date, today)

    def test_a_stored_default_gives_no_end_date_to_a_member(self):
        self.env['ir.default'].with_user(self.owner).set(
            'woow.document.folder.access', 'expiry_date',
            str(fields.Date.today() + timedelta(days=3)), user_id=True)
        line = self.env['woow.document.folder.access'].with_user(self.owner).create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.viewer.id,
            'level': 'read'})
        self.assertFalse(line.expiry_date)

    def test_the_cron_ends_an_expired_folder_member(self):
        line = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.viewer.id,
            'level': 'edit', 'expiry_date': fields.Date.today() + timedelta(days=1)})
        keeper = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.target.id,
            'level': 'read', 'expiry_date': fields.Date.today()})
        self.env.flush_all()
        self.assertTrue(self.folder.with_user(self.viewer)._woow_has_rank(3))
        self.assertTrue(self._can_read(self.viewer))
        self._expire(line)
        # still there until the cron runs (the stored lists are not time dependent)
        self.assertTrue(self.folder.with_user(self.viewer)._woow_has_rank(3))
        self.env['woow.document'].sudo()._cron_expire_access()
        self.assertFalse(line.exists())
        self.assertTrue(keeper.exists())
        self.assertNotIn(self.viewer, self.folder.eff_edit_user_ids)
        self.assertNotIn(self.viewer, self.folder.eff_read_user_ids)
        self.assertFalse(self.folder.with_user(self.viewer)._woow_has_rank(1))
        self.assertFalse(self._can_read(self.viewer))
        self.assertTrue(self.folder.with_user(self.target)._woow_has_rank(1))

    def test_the_cron_ends_an_expired_share_of_a_user(self):
        row = self._row(user=self.viewer, expiry=fields.Date.today() + timedelta(days=2))
        keeper = self._row(user=self.target, expiry=fields.Date.today())
        self._expire(row)
        self.assertTrue(self._can_read(self.viewer))
        self.env['woow.document'].sudo()._cron_expire_access()
        self.assertFalse(row.exists())
        self.assertTrue(keeper.exists())
        self.assertFalse(self._can_read(self.viewer))
        self.assertNotIn(self.viewer, self.document.shared_user_ids)
        self.assertTrue(self._can_read(self.target))

    def test_the_cron_ends_an_expired_share_of_an_editor(self):
        row = self._row(user=self.editor, role='editor',
                        expiry=fields.Date.today() + timedelta(days=2))
        self.assertTrue(self._can_write(self.editor))
        self._expire(row)
        self.env['woow.document'].sudo()._cron_expire_access()
        self.assertFalse(row.exists())
        self.assertNotIn(self.editor, self.document.shared_editor_user_ids)
        self.assertNotIn(self.editor, self.document.shared_user_ids)
        self.assertFalse(self._can_write(self.editor))
        self.assertFalse(self._can_read(self.editor))
        with self.assertRaises(AccessError):
            self.document.with_user(self.editor).write({'name': 'late edit.txt'})

    def test_the_cron_ends_the_access_of_a_portal_contact(self):
        row = self._row(partner=self.portal.partner_id,
                        expiry=fields.Date.today() + timedelta(days=2))
        line = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.other_folder.id, 'subject_type': 'partner',
            'partner_id': self.portal_two.partner_id.id, 'level': 'read',
            'expiry_date': fields.Date.today() + timedelta(days=2)})
        self.env.flush_all()
        documents = self.env['woow.document']
        self.assertEqual(documents.with_user(self.portal)._woow_portal_direct_documents(),
                         self.document)
        in_other = self._document(self.other_folder, 'portal-folder.txt')
        self.assertTrue(self._can_read(self.portal_two, in_other))
        self._expire(row)
        self._expire(line)
        self.env['woow.document'].sudo()._cron_expire_access()
        self.assertFalse(row.exists() or line.exists())
        self.assertFalse(documents.with_user(self.portal)._woow_portal_direct_documents())
        self.assertFalse(self._can_read(self.portal))
        self.assertFalse(self._can_read(self.portal_two, in_other))
        self.assertNotIn(self.portal.partner_id, self.document.shared_partner_ids)

    def test_the_cron_is_bounded_logs_ids_only_and_is_for_the_server(self):
        rows = self.env['woow.document.share']
        for index in range(3):
            user = new_test_user(self.env, login='shr_batch_%d' % index, groups=_USER)
            rows |= self._row(user=user, expiry=fields.Date.today() + timedelta(days=1))
        lines = self.env['woow.document.folder.access']
        for index in range(3):
            user = new_test_user(self.env, login='shr_batch_line_%d' % index, groups=_USER)
            lines |= self.env['woow.document.folder.access'].sudo().create({
                'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': user.id,
                'level': 'read', 'expiry_date': fields.Date.today() + timedelta(days=1)})
        self._expire(rows)
        self._expire(lines)
        with self.assertRaises(AccessError):
            self.env['woow.document'].with_user(self.owner)._cron_expire_access()
        progress = []
        cron = type(self.env['ir.cron'])
        with patch.object(sharing_module, 'ACCESS_EXPIRY_BATCH', 2), patch.object(
                cron, '_notify_progress',
                lambda _self, *, done, remaining: progress.append((done, remaining))):
            self.env['woow.document'].sudo()._cron_expire_access()
            self.assertEqual(len(rows.exists()), 1)
            self.assertEqual(len(lines.exists()), 1)
            self.assertEqual(progress[-1][0], 4)
            self.assertGreater(progress[-1][1], 0)
            self.env['woow.document'].sudo()._cron_expire_access()
            self.assertFalse(rows.exists() or lines.exists())
            self.assertEqual(progress[-1], (2, 0))
            self.env['woow.document'].sudo()._cron_expire_access()
            self.assertEqual(progress[-1], (0, 0))
        entries = self.env['woow.document.log'].sudo().search([('event', '=', 'access_expire')])
        self.assertEqual(len(entries), 6)
        for entry in entries:
            self.assertEqual(entry.channel, 'cron')
            self.assertEqual(len(entry.detail), 1)
            self.assertTrue(set(entry.detail) <= {'member_id', 'share_id'})
            self.assertTrue(all(type(value) is int for value in entry.detail.values()))
        self.assertEqual(len(entries.filtered('document_id')), 3)
        self.assertEqual(len(entries.filtered('folder_id')), 6)

    def test_a_row_that_cannot_be_removed_does_not_stop_the_others(self):
        bad = self._row(user=self.viewer, expiry=fields.Date.today() + timedelta(days=1))
        good = self._row(user=self.target, expiry=fields.Date.today() + timedelta(days=1))
        self._expire(bad | good)
        original = type(self.shares).unlink

        def refuse(records):
            if bad in records:
                raise UserError('refused')
            return original(records)

        with patch.object(type(self.shares), 'unlink', refuse):
            self.env['woow.document'].sudo()._cron_expire_access()
        self.assertTrue(bad.exists())
        self.assertFalse(good.exists())

    def test_the_cron_is_registered_every_thirty_minutes(self):
        cron = self.env.ref('woow_document_management.ir_cron_woow_document_expire_access')
        self.assertEqual((cron.interval_number, cron.interval_type), (30, 'minutes'))
        self.assertEqual(cron.user_id, self.env.ref('base.user_root'))
        self.assertIn('_cron_expire_access', cron.code)


@tagged('woow_docmgmt_core')
class TestSharingDialogs(_SharingCase):

    def test_the_document_dialog_is_for_those_who_may_share(self):
        action = self.document.with_user(self.owner).action_open_share_dialog()
        self.assertEqual((action['res_model'], action['res_id'], action['target']),
                         ('woow.document', self.document.id, 'new'))
        view = self.env.ref('woow_document_management.view_woow_document_share_dialog')
        self.assertEqual(action['views'], [(view.id, 'form')])
        for user in (self.edit_member, self.viewer, self.plain, self.portal, self.outsider):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.document.with_user(user).action_open_share_dialog()
        self.assertTrue(self.document.with_user(self.owner).woow_user_can_share)
        self.assertFalse(self.document.with_user(self.edit_member).woow_user_can_share)
        self.assertTrue(self.document.with_user(self.manager).woow_user_can_share)

    def test_the_folder_dialog_is_for_those_who_manage_the_folder(self):
        action = self.folder.with_user(self.owner).action_open_share_dialog()
        self.assertEqual((action['res_model'], action['res_id'], action['target']),
                         ('woow.document.folder', self.folder.id, 'new'))
        for user in (self.edit_member, self.read_member, self.plain, self.portal):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.folder.with_user(user).action_open_share_dialog()

    def test_views_are_valid_and_show_the_columns(self):
        document = self.env['woow.document'].with_user(self.owner)
        form = document.get_view(self.env.ref(
            'woow_document_management.view_woow_document_document_form_sharing').id, 'form')
        self.assertIn('share_ids', form['arch'])
        self.assertIn('woow_shared_with', form['arch'])
        self.assertIn('action_open_share_dialog', form['arch'])
        dialog = document.get_view(self.env.ref(
            'woow_document_management.view_woow_document_share_dialog').id, 'form')
        self.assertIn('share_ids', dialog['arch'])
        folders = self.env['woow.document.folder'].with_user(self.owner)
        folder_form = folders.get_view(self.env.ref(
            'woow_document_management.view_woow_document_folder_form').id, 'form')
        self.assertIn('expiry_date', folder_form['models']['woow.document.folder.access'])
        dialog = folders.get_view(self.env.ref(
            'woow_document_management.view_woow_folder_share_dialog').id, 'form')
        self.assertIn('woow_all_users_level', dialog['arch'])
        self.assertIn('expiry_date', dialog['models']['woow.document.folder.access'])
        self.assertIn('expiry_date', document.get_view(self.env.ref(
            'woow_document_management.view_woow_document_share_dialog').id, 'form')[
                'models']['woow.document.share'])

    def test_the_document_dialog_saves_rows_as_a_normal_write(self):
        view = 'woow_document_management.view_woow_document_share_dialog'
        with Form(self.document.with_user(self.owner), view=view) as form:
            with form.share_ids.new() as line:
                line.subject_type = 'user'
                line.user_id = self.target
                line.role = 'editor'
                line.expiry_date = fields.Date.today() + timedelta(days=4)
        row = self.document.sudo().share_ids
        self.assertEqual((row.user_id, row.role), (self.target, 'editor'))
        self.assertEqual(self.document.sudo().shared_editor_user_ids, self.target)
        with Form(self.document.with_user(self.owner), view=view) as form:
            form.share_ids.remove(index=0)
        self.assertFalse(self.document.sudo().share_ids)

    def test_the_folder_dialog_sets_what_all_document_users_may_do(self):
        group = self.env.ref(_USER)
        access = self.env['woow.document.folder.access'].sudo()
        folder = self.folder.with_user(self.owner)
        self.assertEqual(folder.woow_all_users_level, 'none')
        for level in ('read', 'upload', 'edit'):
            with self.subTest(level=level):
                folder.write({'woow_all_users_level': level})
                lines = access.search([('folder_id', '=', self.folder.id),
                                       ('group_id', '=', group.id)])
                self.assertEqual(lines.mapped('level'), [level])
                self.assertEqual(folder.woow_all_users_level, level)
        self.assertTrue(self._can_read(self.plain))
        self.assertTrue(self._can_write(self.plain))
        folder.write({'woow_all_users_level': 'none'})
        self.assertFalse(access.search([('folder_id', '=', self.folder.id),
                                        ('group_id', '=', group.id)]))
        self.assertFalse(self._can_read(self.plain))
        with self.assertRaises(UserError):
            folder.write({'woow_all_users_level': 'manage'})
        view = 'woow_document_management.view_woow_folder_share_dialog'
        with Form(self.folder.with_user(self.owner), view=view) as form:
            form.woow_all_users_level = 'read'
        self.assertTrue(self._can_read(self.plain))

    def test_the_all_document_users_choice_is_for_managers_of_the_folder(self):
        for user in (self.edit_member, self.read_member, self.plain, self.viewer):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.folder.with_user(user).write({'woow_all_users_level': 'edit'})
        self.assertEqual(self.folder.with_user(self.owner).woow_all_users_level, 'none')
        self.folder.with_user(self.manager).write({'woow_all_users_level': 'read'})
        self.assertEqual(self.folder.with_user(self.owner).woow_all_users_level, 'read')

    def test_the_all_document_users_choice_keeps_the_ceiling_of_the_parent(self):
        """A folder that does not inherit its access holds the new row to the parent's grants."""
        child = self.env['woow.document.folder'].with_user(self.owner).create({
            'name': 'Sharing child', 'parent_id': self.folder.id, 'inherit_access': False})
        with self.assertRaises(ValidationError):
            child.write({'woow_all_users_level': 'edit'})
        self.assertEqual(child.woow_all_users_level, 'none')


@tagged('woow_docmgmt_core')
class TestUpgradeFrom18_0_1(_SharingCase):

    def _migrate(self):
        path = Path(__file__).resolve().parents[1] / 'migrations' / '18.0.2.0.0' / 'post-sharing.py'
        spec = importlib.util.spec_from_file_location('woow_post_sharing', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.env.flush_all()
        module.migrate(self.env.cr, '18.0.1.1.0')
        self.env.invalidate_all()

    def _old_state(self):
        """Lists that release 18.0.1 filled and no rows: the database as the old code left it."""
        document = self.document.sudo()
        document.write({'shared_user_ids': [Command.set([self.viewer.id, self.editor.id])],
                        'shared_partner_ids': [Command.set([self.portal.partner_id.id])]})
        other = self._document(self.other_folder, 'old-state.txt')
        other.sudo().write({'shared_user_ids': [Command.set([self.viewer.id])]})
        self.env.flush_all()
        self.env.cr.execute('DELETE FROM woow_document_share')
        self.env.invalidate_all()
        return other

    def test_every_entry_of_the_lists_gets_a_viewer_row_without_an_end_date(self):
        other = self._old_state()
        self.assertFalse(self.shares.sudo().search([]))
        self._migrate()
        rows = self.shares.sudo().search([('document_id', 'in', (self.document | other).ids)])
        self.assertEqual(len(rows), 4)
        self.assertEqual(set(rows.mapped('role')), {'viewer'})
        self.assertFalse(any(rows.mapped('expiry_date')))
        mine = rows.filtered(lambda row: row.document_id == self.document)
        self.assertEqual(mine.user_id, self.viewer | self.editor)
        self.assertEqual(mine.partner_id, self.portal.partner_id)
        self.assertEqual({(row.company_id.id, row.folder_id.id) for row in rows},
                         {(row.document_id.company_id.id, row.document_id.folder_id.id)
                          for row in rows})
        self.assertEqual(rows.folder_id, self.folder | self.other_folder)
        self.assertEqual(sorted(self.document.sudo().shared_user_ids.ids),
                         sorted([self.viewer.id, self.editor.id]))
        self.assertFalse(self.document.sudo().shared_editor_user_ids)
        self.assertTrue(self._can_read(self.viewer))
        self.assertTrue(self._can_read(self.portal))

    def test_it_is_safe_to_run_again_and_keeps_what_exists(self):
        self._old_state()
        self._migrate()
        count = self.shares.sudo().search_count([])
        row = self.shares.sudo().search([('user_id', '=', self.editor.id)])
        row.write({'role': 'editor', 'expiry_date': fields.Date.today() + timedelta(days=3)})
        self._migrate()
        self.assertEqual(self.shares.sudo().search_count([]), count)
        self.assertEqual(row.role, 'editor')
        self.assertTrue(row.expiry_date)

    def test_nothing_is_done_on_a_fresh_install(self):
        self._old_state()
        path = Path(__file__).resolve().parents[1] / 'migrations' / '18.0.2.0.0' / 'post-sharing.py'
        spec = importlib.util.spec_from_file_location('woow_post_sharing_fresh', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, None)
        self.assertFalse(self.shares.sudo().search([]))
