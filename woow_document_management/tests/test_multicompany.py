# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Folders of different companies stay apart (US-23)."""
import base64

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'


@tagged('woow_docmgmt_core')
class TestFolderCompanyChanges(TransactionCase):
    """Only a document administrator moves a folder to another company, or to none."""

    def setUp(self):
        super().setUp()
        self.company_a = self.env.company
        self.company_b = self.env['res.company'].sudo().create({'name': 'Folder company B'})
        self.admin = self.env.ref('base.user_admin')
        # The manage grant of the owner is all a plain member has over the subtree.
        self.member = new_test_user(self.env, login='mc_member', groups=_USER)
        self.admin_b = new_test_user(
            self.env, login='mc_admin_b', groups=_MANAGER, company_id=self.company_b.id,
            company_ids=[Command.set([self.company_b.id])])
        folders = self.env['woow.document.folder'].sudo()
        self.root = folders.create({'name': 'MC root', 'owner_id': self.member.id})
        self.child = folders.create({'name': 'MC child', 'parent_id': self.root.id,
                                     'owner_id': self.member.id})
        self.document = self.env['woow.document'].sudo().create({
            'name': 'mc.txt', 'folder_id': self.child.id,
            'datas': base64.b64encode(b'company A only')})
        self.env.flush_all()

    def _seen_by_company_b(self):
        """Folders and documents the administrator of company B can find."""
        return (self.env['woow.document.folder'].with_user(self.admin_b).search_count(
                    [('id', 'in', [self.root.id, self.child.id])]),
                self.env['woow.document'].with_user(self.admin_b).search_count(
                    [('id', '=', self.document.id)]))

    def test_shared_company_false_manager_only(self):
        """T-MC-05: a plain manage member cannot empty the company, folder by folder either."""
        self.assertEqual(self._seen_by_company_b(), (0, 0))
        for folder in (self.root, self.child):
            with self.subTest(folder=folder.name), self.assertRaises(AccessError):
                folder.with_user(self.member).write({'company_id': False})
        self.assertEqual((self.root | self.child).company_id, self.company_a)
        self.assertEqual(self.document.company_id, self.company_a)
        self.assertEqual(self._seen_by_company_b(), (0, 0))
        # A document administrator may share a folder across companies (US-23 AC4).
        self.root.with_user(self.admin).write({'company_id': False})
        self.assertFalse(self.root.company_id)
        self.assertEqual(self.env['woow.document.folder'].with_user(self.admin_b).search_count(
            [('id', '=', self.root.id)]), 1)

    def test_a_manage_member_cannot_hand_a_folder_to_another_company(self):
        for value in (self.company_b.id, self.company_b, (self.company_b.id, 'B'), 'x'):
            with self.subTest(value=value), self.assertRaises(AccessError):
                self.root.with_user(self.member).write({'company_id': value})
        self.assertEqual(self.root.company_id, self.company_a)
        self.assertEqual(self._seen_by_company_b(), (0, 0))
        # Naming the company the folder already has is no change.
        self.root.with_user(self.member).write({
            'company_id': self.company_a.id, 'description': '<p>kept in company A</p>'})
        self.assertEqual(self.root.company_id, self.company_a)
        self.assertIn('kept in company A', self.root.description)

    def test_a_manage_member_cannot_create_a_folder_without_a_company(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.allow_root_create', 'True')
        folders = self.env['woow.document.folder']
        for value in (False, None):
            with self.subTest(value=value), self.assertRaises(AccessError):
                folders.with_user(self.member).create({'name': 'Open to all', 'company_id': value})
        with self.assertRaises(AccessError):
            folders.with_user(self.member).create({
                'name': 'Open child', 'parent_id': self.root.id, 'company_id': False})
        own = folders.with_user(self.member).create({'name': 'Company folder of the member'})
        self.assertEqual(own.company_id, self.company_a)
        shared = folders.with_user(self.admin).create({'name': 'Shared by the administrator',
                                                       'company_id': False})
        self.assertFalse(shared.company_id)

    def test_company_value_shapes(self):
        from ..models.document_folder import _company_value
        company = self.company_b
        for value, expected in ((False, False), (None, False), (company.id, company.id),
                                (company, company.id), (company.browse(), False),
                                ((company.id, 'B'), company.id), ([], False),
                                (company | self.company_a, None), ('x', None), (1.5, None),
                                ({'id': 1}, None)):
            with self.subTest(value=value):
                self.assertEqual(_company_value(value), expected)
