# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The company of a member row is the company of its folder, and a client does not choose it.

The field is read-only in the views, but the ORM stores a value written to it. A member with the
manage grant could so send a row of their folder to another company: the grant kept working, yet
the company rule hid the row from the document administrators of the folder and from its writer.
"""
from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

_USER = 'woow_document_management.group_woow_document_user'


@tagged('woow_docmgmt_core')
class TestMemberRowCompanyFollowsItsFolder(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.company_a = env.company
        self.company_b = env['res.company'].sudo().create({'name': 'Member row company B'})
        self.admin = env.ref('base.user_admin')
        self.manager = new_test_user(env, login='rowco_manager', groups=_USER)
        self.editor = new_test_user(env, login='rowco_editor', groups=_USER)
        self.member = new_test_user(env, login='rowco_member', groups=_USER)
        self.newcomer = new_test_user(env, login='rowco_newcomer', groups=_USER)
        folders = env['woow.document.folder'].sudo()
        self.folder = folders.create({'name': 'Member row folder', 'owner_id': self.admin.id})
        self.second = folders.create({'name': 'Member row second', 'owner_id': self.admin.id})
        access = env['woow.document.folder.access'].sudo()
        for folder in (self.folder, self.second):
            access.create({'folder_id': folder.id, 'subject_type': 'user',
                           'user_id': self.manager.id, 'level': 'manage'})
        access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                       'user_id': self.editor.id, 'level': 'edit'})
        self.line = access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                                   'user_id': self.member.id, 'level': 'read'})
        env.flush_all()

    def _count(self, user):
        return self.env['woow.document.folder.access'].with_user(user).search_count(
            [('id', '=', self.line.id)])

    def test_a_manage_member_cannot_send_a_member_row_to_another_company(self):
        line = self.line.with_user(self.manager)
        for value in (self.company_b.id, self.company_b, (self.company_b.id, 'B'), False):
            with self.subTest(value=value), self.assertRaises(AccessError), self.cr.savepoint():
                line.write({'company_id': value})
        with self.subTest('through the folder'), self.assertRaises(AccessError), \
                self.cr.savepoint():
            self.folder.with_user(self.manager).write({
                'access_ids': [Command.update(self.line.id, {'company_id': self.company_b.id})]})
        self.env.flush_all()
        self.assertEqual(self.line.company_id, self.company_a)
        # the row stays in the list of the document administrators, and of its writer
        self.assertEqual((self._count(self.admin), self._count(self.manager)), (1, 1))

        # Naming the company the row already has changes nothing, whatever else is written,
        # and the row may move to another folder of that company.
        line.write({'company_id': self.company_a.id, 'level': 'upload'})
        self.assertEqual((self.line.company_id, self.line.level), (self.company_a, 'upload'))
        line.write({'company_id': self.company_a.id, 'folder_id': self.second.id})
        self.assertEqual((self.line.folder_id, self.line.company_id),
                         (self.second, self.company_a))
        # A new row has the company of its folder, whatever the call names.
        created = self.env['woow.document.folder.access'].with_user(self.manager).create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.newcomer.id,
            'level': 'read', 'company_id': self.company_b.id})
        self.assertEqual(created.company_id, self.company_a)
        # A member with the edit grant has no right over the rows at all.
        with self.assertRaises(AccessError):
            created.with_user(self.editor).write({'company_id': self.company_b.id})
        # A document administrator who gives the folder to no company takes its rows along.
        self.folder.with_user(self.admin).write({'company_id': False})
        self.env.flush_all()
        self.assertFalse(created.company_id)

    def test_the_company_of_the_target_folder_decides_when_a_row_moves(self):
        """A write that names a folder too is compared with that folder, not with the one it leaves."""
        env = self.env
        companies = [self.company_a.id, self.company_b.id]
        mover = new_test_user(env, login='rowco_mover', groups=_USER, company_id=self.company_a.id,
                              company_ids=[Command.set(companies)])
        folder_b = env['woow.document.folder'].sudo().create({
            'name': 'Member row folder B', 'owner_id': mover.id, 'company_id': self.company_b.id})
        env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': mover.id,
            'level': 'manage'})
        line = env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.newcomer.id,
            'level': 'read'})
        env.flush_all()
        as_mover = line.with_user(mover).with_context(allowed_company_ids=companies)
        # the company of the folder it leaves is not the one that counts
        with self.assertRaises(AccessError), self.cr.savepoint():
            as_mover.write({'company_id': self.company_a.id, 'folder_id': folder_b.id})
        self.assertEqual((line.folder_id, line.company_id), (self.folder, self.company_a))
        # the company of the folder it joins is
        as_mover.write({'company_id': self.company_b.id, 'folder_id': folder_b.id})
        self.assertEqual((line.folder_id, line.company_id), (folder_b, self.company_b))
