# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

from odoo import Command
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_core')
class TestFolderNameFolding(TransactionCase):

    def test_hidden_parent_name_is_not_exposed(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Path reader', 'login': 'wdoc-path-reader',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_user').id])],
        })
        folders = self.env['woow.document.folder'].sudo()
        root = folders.create({'name': 'Hidden ancestor'})
        child = folders.create({'name': 'Visible child', 'parent_id': root.id,
                                'inherit_access': False})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': child.id, 'subject_type': 'user',
            'user_id': user.id, 'level': 'read',
        })
        self.env.flush_all()
        self.assertEqual(root.with_user(user).display_name, 'Restricted folder')
        self.assertEqual(child.with_user(user).display_name, '… / Visible child')
        self.assertEqual(child.with_user(user)._woow_breadcrumb(),
                         [(None, '…'), (child.id, 'Visible child')])

    def test_portal_navigation_skips_hidden_intermediate_folder(self):
        partner = self.env['res.partner'].sudo().create({'name': 'Navigation contact'})
        portal = self.env['res.users'].sudo().create({
            'name': 'Navigation user', 'login': 'wdoc-navigation',
            'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        folders = self.env['woow.document.folder'].sudo()
        root = folders.create({'name': 'Portal root'})
        hidden = folders.create({'name': 'Private intermediate', 'parent_id': root.id,
                                 'inherit_access': False})
        leaf = folders.create({'name': 'Portal leaf', 'parent_id': hidden.id,
                               'inherit_access': False})
        for folder in (root, leaf):
            self.env['woow.document.folder.access'].sudo().create({
                'folder_id': folder.id, 'subject_type': 'partner',
                'partner_id': partner.id, 'level': 'read',
            })
        self.env.flush_all()
        model = folders.with_user(portal)
        self.assertEqual(model._woow_portal_roots().ids, [root.id])
        self.assertEqual(root.with_user(portal)._woow_portal_children().ids, [leaf.id])
        self.assertEqual(leaf.with_user(portal)._woow_breadcrumb(), [
            (root.id, 'Portal root'), (None, '…'), (leaf.id, 'Portal leaf')])
