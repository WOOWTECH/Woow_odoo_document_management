# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""A wizard row is its creator's alone (R-W2..R-W6).

Core has no owner rule for transient models. The actions of these wizards act on the
stored row with the rights of the user who runs them, so another document user must not
read or change a row that, say, an administrator is about to confirm.
"""
import base64

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

_USER_GROUP = 'woow_document_management.group_woow_document_user'


@tagged('woow_docmgmt_core')
class TestWizardRowsBelongToTheirCreator(TransactionCase):

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.other = new_test_user(self.env, login='wizard_scope_other', groups=_USER_GROUP)
        folders = self.env['woow.document.folder'].sudo()
        self.folder = folders.create({'name': 'Wizard scope source', 'owner_id': self.admin.id})
        self.target = folders.create({'name': 'Wizard scope target', 'owner_id': self.admin.id})
        for folder in (self.folder, self.target):
            self.env['woow.document.folder.access'].sudo().create({
                'folder_id': folder.id, 'subject_type': 'user',
                'user_id': self.other.id, 'level': 'upload'})
        self.document = self.env['woow.document'].sudo().create({
            'name': 'wizard-scope.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'wizard scope')})
        # Rows an administrator could be about to confirm, and the smallest row that an
        # ordinary document user may create for each wizard.
        self.admin_rows = {
            'woow.document.move.wizard': {
                'document_ids': [(6, 0, [self.document.id])], 'target_folder_id': self.target.id},
            'woow.document.restore.wizard': {
                'document_ids': [(6, 0, [self.document.id])], 'target_folder_id': self.target.id},
            'woow.document.purge.wizard': {
                'document_ids': [(6, 0, [self.document.id])], 'confirm': True},
            'woow.document.add.attachment.wizard': {'folder_id': self.folder.id},
            'woow.document.notify.wizard': {'folder_id': self.folder.id, 'all_members': True},
        }
        self.own_rows = {
            'woow.document.move.wizard': {'target_folder_id': self.target.id},
            'woow.document.restore.wizard': {},
            'woow.document.purge.wizard': {},
            'woow.document.add.attachment.wizard': {'folder_id': self.folder.id},
            'woow.document.notify.wizard': {'folder_id': self.folder.id},
        }

    def test_another_document_user_cannot_find_read_or_change_the_row(self):
        for model, values in self.admin_rows.items():
            with self.subTest(model=model):
                row = self.env[model].with_user(self.admin).create(values)
                self.env.flush_all()
                found = self.env[model].with_user(self.other)
                self.assertEqual(found.search_count([('id', '=', row.id)]), 0)
                self.assertFalse(found.search([('id', '=', row.id)]))
                self.assertEqual(found.search_read([('id', '=', row.id)], ['id']), [])
                with self.assertRaises(AccessError):
                    found.browse(row.id).read(['id'])
                with self.assertRaises(AccessError):
                    found.browse(row.id).write({next(iter(values)): False})
                # The creator's own row is untouched and still theirs.
                self.assertEqual(row.with_user(self.admin).read(['id'])[0]['id'], row.id)

    def test_a_document_user_keeps_their_own_row_and_no_one_else_sees_it(self):
        for model, values in self.own_rows.items():
            with self.subTest(model=model):
                own = self.env[model].with_user(self.other).create(values)
                self.env.flush_all()
                mine = self.env[model].with_user(self.other)
                self.assertEqual(mine.search_count([('id', '=', own.id)]), 1)
                self.assertEqual(own.read(['id'])[0]['id'], own.id)
                # A document administrator does not see it either: the rule has no
                # exception for the administrator group.
                theirs = self.env[model].with_user(self.admin)
                self.assertEqual(theirs.search_count([('id', '=', own.id)]), 0)
                with self.assertRaises(AccessError):
                    theirs.browse(own.id).read(['id'])
