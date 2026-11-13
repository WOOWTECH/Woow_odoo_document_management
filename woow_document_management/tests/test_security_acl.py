# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_core')
class TestDocumentPermissions(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        user_group = cls.env.ref('woow_document_management.group_woow_document_user')
        cls.user = cls.env['res.users'].sudo().create({
            'name': 'Document member fixture', 'login': 'wdoc-member-acl',
            'groups_id': [Command.set([user_group.id])],
        })
        cls.outsider = cls.env['res.users'].sudo().create({
            'name': 'Unassigned fixture', 'login': 'wdoc-outsider-acl',
            'groups_id': [Command.set([cls.env.ref('base.group_user').id])],
        })
        cls.root = cls.env['woow.document.folder'].sudo().create({'name': 'Access root'})

    def test_group_membership_alone_does_not_grant_folder_access(self):
        self.assertEqual(self.env['woow.document.folder'].with_user(self.user).search_count(
            [('id', '=', self.root.id)]), 0)
        self.assertFalse(self.root.with_user(self.user)._woow_has_rank(1))
        self.assertFalse(self.env['woow.document.folder'].with_user(
            self.outsider).check_access_rights('read', raise_exception=False))

    def test_member_cannot_move_grant_to_unmanaged_folder(self):
        """A source-folder manager cannot import their grant into a hidden target."""
        target = self.env['woow.document.folder'].sudo().create({
            'name': 'Unmanaged target',
        })
        grant = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.root.id, 'subject_type': 'user',
            'user_id': self.user.id, 'level': 'manage',
        })
        # Give the caller read access to the target, so a hidden-record
        # AccessError cannot mask a missing target management check.
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': target.id, 'subject_type': 'group',
            'group_id': self.env.ref('base.group_user').id, 'level': 'read',
        })
        self.env.flush_all()
        self.assertTrue(self.root.with_user(self.user)._woow_has_rank(4))
        self.assertFalse(target.with_user(self.user)._woow_has_rank(4))
        self.assertTrue(target.with_user(self.user)._woow_has_rank(1))
        with self.assertRaises(AccessError):
            grant.with_user(self.user).write({'folder_id': target.id})
        self.assertEqual(grant.folder_id.id, self.root.id)
        with self.assertRaises(AccessError):
            self.env['woow.document.folder.access'].with_user(self.user).create({
                'folder_id': target.id, 'subject_type': 'user',
                'user_id': self.user.id, 'level': 'manage',
            })

    def test_effective_grant_controls_rank_and_record_rule(self):
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.root.id, 'subject_type': 'user',
            'user_id': self.user.id, 'level': 'edit',
        })
        self.env.flush_all()
        self.assertIn(self.user, self.root.eff_edit_user_ids)
        folder = self.root.with_user(self.user)
        self.assertEqual(folder.search_count([('id', '=', folder.id)]), 1)
        self.assertEqual(folder.woow_user_rank, 3)
        self.assertTrue(folder._woow_has_rank(3))
        self.assertFalse(folder._woow_has_rank(4))
        folder.write({'name': 'Edited by member'})
        self.assertEqual(self.root.name, 'Edited by member')
        with self.assertRaises(AccessError):
            folder._woow_require_rank(4)
        with self.assertRaises(AccessError):
            folder.write({'owner_id': self.user.id})
        with self.assertRaises(AccessError):
            folder.write({'eff_manage_user_ids': [Command.link(self.user.id)]})

    def test_child_creation_keeps_access_within_parent(self):
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.root.id, 'subject_type': 'user',
            'user_id': self.user.id, 'level': 'edit',
        })
        self.env.flush_all()
        folders = self.env['woow.document.folder'].with_user(self.user)
        child = folders.create({'name': 'Editor child', 'parent_id': self.root.id})
        self.env.flush_all()
        self.assertEqual(child.owner_id, self.root.owner_id)
        self.assertTrue(child._woow_has_rank(3))
        self.assertFalse(child._woow_has_rank(4))
        with self.assertRaises(AccessError):
            folders.create({'name': 'Chosen owner', 'parent_id': self.root.id,
                            'owner_id': self.user.id})
        with self.assertRaises(AccessError):
            folders.create({'name': 'Isolated child', 'parent_id': self.root.id,
                            'inherit_access': False})
        with self.assertRaises(AccessError):
            self.env['woow.document.folder.access'].with_user(self.user).create({
                'folder_id': child.id, 'subject_type': 'user',
                'user_id': self.user.id, 'level': 'manage',
            })

    def test_root_policy_and_field_groups(self):
        folder = self.env['woow.document.folder'].with_user(self.user)
        with self.assertRaises(AccessError):
            folder.create({'name': 'Unapproved root'})
        with self.assertRaises(AccessError):
            folder.create({'name': 'Protected field', 'legal_hold': True})
        self.assertFalse(self.env['woow.document.folder'].sudo().with_context(
            default_legal_hold=True).create({'name': 'Filtered default'}).legal_hold)

    def test_root_policy_covers_moves(self):
        """Moving a folder to the top level is creating a root folder (M-24)."""
        params = self.env['ir.config_parameter'].sudo()
        folders = self.env['woow.document.folder'].sudo()
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.root.id, 'subject_type': 'user',
            'user_id': self.user.id, 'level': 'manage'})
        moved = folders.create({'name': 'Moved up', 'parent_id': self.root.id})
        folders.create({'name': 'Moved up'})   # a root with the same name already exists
        self.env.flush_all()
        self.assertTrue(moved.with_user(self.user)._woow_has_rank(4))
        # Refused with the same answer whether or not the name is taken at the top level.
        for name in ('Moved up', 'Moved elsewhere'):
            with self.subTest(name=name), self.assertRaises(AccessError):
                moved.with_user(self.user).write({'parent_id': False, 'name': name})
        self.assertEqual(moved.parent_id, self.root)
        # A folder that is already at the top level is not moved by a write that keeps it there.
        self.root.with_user(self.user).write({'parent_id': False})
        # The site setting opens it, as it does for creating a root folder.
        params.set_param('woow_document_management.allow_root_create', 'True')
        moved.with_user(self.user).write({'parent_id': False, 'name': 'Moved elsewhere'})
        self.assertFalse(moved.parent_id)
        # Document administrators and the superuser never need the setting.
        params.set_param('woow_document_management.allow_root_create', 'False')
        admin = self.env.ref('base.user_admin')
        by_admin = folders.create({'name': 'Moved by admin', 'parent_id': self.root.id})
        by_admin.with_user(admin).write({'parent_id': False})
        self.assertFalse(by_admin.parent_id)
        by_su = folders.create({'name': 'Moved by su', 'parent_id': self.root.id})
        by_su.write({'parent_id': False})
        self.assertFalse(by_su.parent_id)

    def test_portal_partner_grant_is_exact_and_write_is_only_a_probe(self):
        partner = self.env['res.partner'].sudo().create({'name': 'Portal document contact'})
        other = self.env['res.partner'].sudo().create({'name': 'Other portal contact'})
        portal_group = self.env.ref('base.group_portal')
        users = self.env['res.users'].sudo()
        portal = users.create({'name': 'Portal document user', 'login': 'wdoc-portal-acl',
                               'partner_id': partner.id,
                               'groups_id': [Command.set([portal_group.id])]})
        other_portal = users.create({'name': 'Other portal user', 'login': 'wdoc-portal-other',
                                     'partner_id': other.id,
                                     'groups_id': [Command.set([portal_group.id])]})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.root.id, 'subject_type': 'partner',
            'partner_id': partner.id, 'level': 'upload',
        })
        self.env.flush_all()
        mine = self.root.with_user(portal)
        self.assertEqual(mine.search_count([('id', '=', mine.id)]), 1)
        self.assertTrue(mine._woow_portal_can_upload())
        self.assertFalse(mine._woow_has_rank(1))
        with self.assertRaises(AccessError):
            mine.write({'name': 'Forbidden portal rename'})
        theirs = self.root.with_user(other_portal)
        self.assertEqual(theirs.search_count([('id', '=', theirs.id)]), 0)
        self.assertFalse(theirs._woow_portal_can_upload())

    def test_effective_access_summary_respects_role_and_hidden_ancestors(self):
        """Managers see members; read-only and portal users cannot inspect them."""
        manager_group = self.env.ref('woow_document_management.group_woow_document_manager')
        manager = self.env['res.users'].sudo().create({
            'name': 'Document administrator fixture', 'login': 'wdoc-manager-summary',
            'groups_id': [Command.set([manager_group.id])],
        })
        portal = self.env['res.users'].sudo().create({
            'name': 'Portal summary fixture', 'login': 'wdoc-portal-summary',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Visible child', 'parent_id': self.root.id,
        })
        access = self.env['woow.document.folder.access'].sudo()
        access.create({'folder_id': self.root.id, 'subject_type': 'user',
                       'user_id': self.user.id, 'level': 'read'})
        access.create({'folder_id': self.root.id, 'subject_type': 'partner',
                       'partner_id': portal.partner_id.id, 'level': 'read'})
        self.env.flush_all()

        manager_lines = child.with_user(manager).effective_access_lines
        self.assertTrue(any(line.get('principal_id') == self.user.id and
                            line.get('subject_type') == 'user' for line in manager_lines))
        self.assertTrue(any(line.get('source_folder_name') == self.root.name
                            for line in manager_lines))
        self.assertFalse(child.with_user(self.user).effective_access_lines)

        portal_folder = child.with_user(portal)
        self.assertNotIn('effective_access_lines', portal_folder.fields_get())
        with self.assertRaises(AccessError):
            portal_folder.read(['effective_access_lines'])

        # The child can be managed without making its ancestor readable.
        access.create({'folder_id': child.id, 'subject_type': 'user',
                       'user_id': self.user.id, 'level': 'manage'})
        access.search([('folder_id', '=', self.root.id),
                       ('user_id', '=', self.user.id)]).unlink()
        self.env.flush_all()
        self.assertEqual(self.root.with_user(self.user).search_count(
            [('id', '=', self.root.id)]), 0)
        lines = child.with_user(self.user).effective_access_lines
        self.assertTrue(any(line.get('source') == 'inherited' for line in lines))
        self.assertFalse(any(self.root.name in str(line) for line in lines))
        self.assertFalse(any(line.get('principal_id') == portal.partner_id.id and
                             line.get('subject_type') == 'partner' for line in lines))
