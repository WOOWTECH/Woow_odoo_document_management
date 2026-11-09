# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import base64
import math

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from psycopg2 import IntegrityError
from unittest.mock import patch

from ..models import folder_access
from ..services.locks import LockBusy
from ..models.upload_policy import WoowUploadRejected


@tagged('woow_docmgmt_core')
class TestFolderCreation(TransactionCase):

    def test_administrator_has_document_manager_group(self):
        self.assertTrue(self.env.ref('base.user_admin').has_group(
            'woow_document_management.group_woow_document_manager'))

    def test_folder_and_child_can_be_created_with_recursive_effective_fields(self):
        folder_model = self.env['woow.document.folder'].sudo()
        root = folder_model.create({'name': 'Root'})
        child = folder_model.create({'name': 'Child', 'parent_id': root.id})
        self.assertEqual(child.parent_id, root)
        self.assertEqual(child.complete_name, 'Root / Child')

    def test_active_folder_names_are_unique_per_parent_and_company(self):
        folders = self.env['woow.document.folder'].sudo()
        parent = folders.create({'name': 'Parent'})
        first = folders.create({'name': '  Reports  ', 'parent_id': parent.id})
        self.assertEqual(first.name, 'Reports')
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            folders.create({'name': 'reports', 'parent_id': parent.id})
        with self.assertRaises(UserError):
            first.write({'name': '   '})
        other_parent = folders.create({'name': 'Other parent'})
        folders.create({'name': 'Reports', 'parent_id': other_parent.id})
        first.write({'active': False})
        self.env.flush_all()
        folders.create({'name': 'reports', 'parent_id': parent.id})

    def test_subtree_hold_and_restricted_descendant_recompute(self):
        folders = self.env['woow.document.folder'].sudo()
        root = folders.create({'name': 'Root'})
        child = folders.create({'name': 'Child', 'parent_id': root.id})
        leaf = folders.create({'name': 'Leaf', 'parent_id': child.id})
        self.assertFalse(root.subtree_legal_hold)
        self.assertFalse(root.has_restricted_descendant)
        leaf.write({'legal_hold': True})
        self.assertTrue(root.subtree_legal_hold)
        leaf.write({'legal_hold': False})
        self.assertFalse(root.subtree_legal_hold)
        leaf.write({'inherit_access': False})
        self.assertTrue(root.has_restricted_descendant)
        leaf.write({'inherit_access': True})
        self.assertFalse(root.has_restricted_descendant)

    def test_counts_follow_visible_active_records(self):
        folder_model = self.env['woow.document.folder'].sudo()
        root = folder_model.create({'name': 'Count root'})
        child = folder_model.create({'name': 'Count child', 'parent_id': root.id})
        child.write({'inherit_access': False})
        documents = self.env['woow.document'].sudo()
        first = documents.create({'name': 'one.txt', 'folder_id': root.id,
                                  'datas': base64.b64encode(b'one')})
        hidden = documents.create({'name': 'two.txt', 'folder_id': root.id,
                                   'datas': base64.b64encode(b'two')})
        hidden.write({'active': False})
        user = self.env['res.users'].sudo().create({
            'name': 'Count reader', 'login': 'wdoc-count-reader',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_user').id])],
        })
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': root.id, 'subject_type': 'user',
            'user_id': user.id, 'level': 'read',
        })
        self.assertIn(user, root.eff_read_user_ids)
        self.assertEqual(documents.with_user(user).search_count([
            ('folder_id', '=', root.id)]), 1)
        self.assertEqual([(f.id, count) for f, count in
                          documents.with_user(user)._read_group(
                              [('folder_id', 'in', [root.id])],
                              ['folder_id'], ['__count'])], [(root.id, 1)])
        self.assertEqual(root.with_user(user).document_count, 1)
        self.assertEqual(root.with_user(user).subfolder_count, 0)
        self.assertEqual(root.subfolder_count, 1)
        self.assertEqual(child.with_user(user).document_count, 0)
        self.assertTrue(first.exists())

    def test_grants_propagate_and_revoke_across_tree(self):
        folder_model = self.env['woow.document.folder'].sudo()
        access_model = self.env['woow.document.folder.access'].sudo()
        user = self.env['res.users'].sudo().create({
            'name': 'Grant fixture', 'login': 'grant-fixture-user',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_user').id])],
        })
        root = folder_model.create({
            'name': 'Root', 'owner_id': self.env.ref('base.user_admin').id})
        child = folder_model.create({'name': 'Child', 'parent_id': root.id})
        leaf = folder_model.create({'name': 'Leaf', 'parent_id': child.id})
        grant = access_model.create({'folder_id': root.id, 'subject_type': 'user',
                                     'user_id': user.id, 'level': 'read'})
        self.assertIn(user, child.eff_read_user_ids)
        self.assertIn(user, leaf.eff_read_user_ids)
        self.assertNotIn(user, child.eff_upload_user_ids)
        grant.write({'level': 'edit'})
        self.assertIn(user, child.eff_edit_user_ids)
        self.assertNotIn(user, child.eff_manage_user_ids)
        with self.assertRaises(UserError):
            child.write({'inherit_access': False,
                         'access_ids': [Command.clear()]})
        with self.assertRaises(UserError):
            child.write({'inherit_access': False, 'parent_id': root.id})
        self.assertTrue(child.inherit_access)
        child.write({'inherit_access': False})
        self.assertIn(user, child.eff_edit_user_ids)
        self.assertTrue(child.access_ids.filtered(
            lambda line: line.user_id == root.owner_id and line.level == 'manage'))
        grant.write({'level': 'manage'})
        self.assertNotIn(user, child.eff_manage_user_ids)
        child.write({'inherit_access': True})
        self.assertIn(user, child.eff_manage_user_ids)
        child.access_ids.filtered(lambda line: line.user_id == user).unlink()
        grant.unlink()
        self.assertNotIn(user, child.eff_read_user_ids)
        self.assertIn(root.owner_id, child.eff_manage_user_ids)
        grant = access_model.create({'folder_id': root.id, 'subject_type': 'user',
                                     'user_id': user.id, 'level': 'manage'})
        self.assertIn(user, child.eff_manage_user_ids)
        new_root = folder_model.create({'name': 'Other root'})
        child.write({'parent_id': new_root.id})
        self.assertNotIn(user, child.eff_read_user_ids)
        self.assertNotIn(user, leaf.eff_read_user_ids)

    def test_group_grant_and_owner_change_propagate(self):
        folder_model = self.env['woow.document.folder'].sudo()
        manager = self.env.ref('base.user_admin')
        replacement = self.env['res.users'].sudo().create({
            'name': 'Replacement owner', 'login': 'replacement-owner-fixture',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_user').id])],
        })
        root = folder_model.create({'name': 'Root', 'owner_id': manager.id})
        child = folder_model.create({'name': 'Child', 'parent_id': root.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': root.id, 'subject_type': 'group',
            'group_id': self.env.ref('base.group_user').id, 'level': 'edit',
        })
        self.assertIn(self.env.ref('base.group_user'), child.eff_edit_group_ids)
        self.assertNotIn(self.env.ref('base.group_user'), child.eff_manage_group_ids)
        root.write({'owner_id': replacement.id})
        self.assertIn(replacement, child.eff_manage_user_ids)
        self.assertNotIn(manager, child.eff_manage_user_ids)

    def test_portal_grants_never_exceed_upload(self):
        folder_model = self.env['woow.document.folder'].sudo()
        root = folder_model.create({'name': 'Root'})
        partner = self.env['res.partner'].sudo().create({'name': 'Portal fixture'})
        self.env['res.users'].sudo().create({
            'name': 'Portal fixture user', 'login': 'portal-grant-fixture',
            'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': root.id, 'subject_type': 'partner',
            'partner_id': partner.id, 'level': 'upload',
        })
        self.assertIn(partner, root.eff_read_partner_ids)
        self.assertIn(partner, root.eff_upload_partner_ids)
        child = folder_model.create({'name': 'Child', 'parent_id': root.id})
        self.assertIn(partner, child.eff_upload_partner_ids)
        with self.assertRaises(IntegrityError):
            with self.cr.savepoint():
                self.env['woow.document.folder.access'].sudo().create({
                    'folder_id': root.id, 'subject_type': 'partner',
                    'partner_id': partner.id, 'level': 'manage',
                })

    def test_invalid_internal_user_grant_is_rejected(self):
        root = self.env['woow.document.folder'].sudo().create({'name': 'Root'})
        outsider = self.env['res.users'].sudo().create({
            'name': 'Other internal', 'login': 'other-internal-fixture',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(ValidationError):
            self.env['woow.document.folder.access'].sudo().create({
                'folder_id': root.id, 'subject_type': 'user',
                'user_id': outsider.id, 'level': 'read',
            })

    def test_member_limit_counts_after_folder_lock(self):
        root = self.env['woow.document.folder'].sudo().create({'name': 'Root'})
        group = self.env.ref('woow_document_management.group_woow_document_user')
        users = [self.env['res.users'].sudo().create({
            'name': f'Limit user {number}', 'login': f'limit-user-{number}',
            'groups_id': [Command.set([group.id])],
        }) for number in (1, 2)]
        access = self.env['woow.document.folder.access'].sudo()
        with patch.object(folder_access, 'MAX_ACCESS_LINES', 1):
            access.create({'folder_id': root.id, 'subject_type': 'user',
                           'user_id': users[0].id, 'level': 'read'})
            with self.assertRaises(ValidationError):
                with self.cr.savepoint():
                    access.create({'folder_id': root.id, 'subject_type': 'user',
                                   'user_id': users[1].id, 'level': 'read'})
        self.assertEqual(access.search_count([('folder_id', '=', root.id)]), 1)

    def test_member_lock_timeout_happens_before_insert(self):
        root = self.env['woow.document.folder'].sudo().create({'name': 'Root'})
        user = self.env.ref('base.user_admin')
        access = self.env['woow.document.folder.access'].sudo()
        with patch.object(folder_access, 'lock', side_effect=LockBusy('access')):
            with self.assertRaises(LockBusy):
                access.create({'folder_id': root.id, 'subject_type': 'user',
                               'user_id': user.id, 'level': 'read'})
            with self.assertRaises(LockBusy):
                access.with_context(default_folder_id=root.id).create({
                    'subject_type': 'user', 'user_id': user.id, 'level': 'read'})
        self.assertEqual(access.search_count([('folder_id', '=', root.id)]), 0)


@tagged('woow_docmgmt_core')
class TestDocumentCreation(TransactionCase):

    def setUp(self):
        super().setUp()
        self.folder = self.env['woow.document.folder'].sudo().create({'name': 'Upload root'})
        self.document = self.env['woow.document'].sudo()

    def test_file_upload_validates_and_records_audit(self):
        doc = self.document.create({
            'name': '../notes.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'hello document'),
        })
        self.assertEqual(doc.name, 'notes.txt')
        self.assertEqual(doc.mimetype, 'text/plain')
        self.assertTrue(doc.attachment_id)
        self.assertEqual(doc.file_size, len(b'hello document'))
        self.assertEqual(self.env['woow.document.log'].sudo().search_count([
            ('event', '=', 'upload'), ('document_id', '=', doc.id)]), 1)

    def test_batch_rejects_bad_file_without_partial_document(self):
        before = self.document.search_count([])
        with self.assertRaises(WoowUploadRejected):
            self.document.create([
                {'name': 'first.txt', 'folder_id': self.folder.id,
                 'datas': base64.b64encode(b'first')},
                {'name': 'second.svg', 'folder_id': self.folder.id,
                 'datas': base64.b64encode(b'<svg/>')},
            ])
        self.assertEqual(self.document.search_count([]), before)

    def test_member_upload_requires_folder_grant(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Uploader', 'login': 'wdoc-uploader',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_user').id])],
        })
        vals = {'name': 'member.txt', 'folder_id': self.folder.id,
                'datas': base64.b64encode(b'member file')}
        with self.assertRaises(AccessError):
            self.document.with_user(user).create(vals)
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user',
            'user_id': user.id, 'level': 'upload',
        })
        self.env.flush_all()
        doc = self.document.with_user(user).create(vals)
        self.assertEqual(doc.owner_id, user)

    def test_http_url_requires_setting(self):
        with self.assertRaises(UserError):
            self.document.create({'name': 'Link', 'document_type': 'url',
                                  'folder_id': self.folder.id,
                                  'url': 'http://example.com/'})
        doc = self.document.create({'name': 'Link', 'document_type': 'url',
                                    'folder_id': self.folder.id,
                                    'url': 'HTTPS://example.com/a'})
        self.assertEqual(doc.url, 'https://example.com/a')
        self.assertFalse(doc.attachment_id)

    def test_portal_upload_records_partner_without_becoming_follower(self):
        partner = self.env['res.partner'].sudo().create({'name': 'Portal uploader'})
        portal = self.env['res.users'].sudo().create({
            'name': 'Portal upload account', 'login': 'wdoc-portal-upload',
            'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner',
            'partner_id': partner.id, 'level': 'upload',
        })
        self.env.flush_all()
        doc = self.document.with_user(portal).create({
            'name': 'portal.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'portal content'),
        })
        self.assertEqual(doc.uploaded_by_partner_id, partner)
        self.assertFalse(doc.sudo().owner_id)
        self.assertNotIn(partner, doc.sudo().message_partner_ids)

    def test_batch_quota_counts_previous_file_before_next_validation(self):
        quota = self.env['woow.document.quota']
        usage = quota._usage_bytes()
        mib = 1024 * 1024
        capacity_mb = math.ceil((usage + 2 * mib) * 100 / (95 * mib))
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.quota_mb', str(capacity_mb))
        remaining = capacity_mb * mib * 95 // 100 - usage
        size = remaining * 3 // 5
        self.assertGreater(size, 0)
        before = self.document.search_count([])
        with self.assertRaises(WoowUploadRejected) as caught:
            self.document.create([
                {'name': 'one.txt', 'folder_id': self.folder.id,
                 'datas': base64.b64encode(b'a' * size)},
                {'name': 'two.txt', 'folder_id': self.folder.id,
                 'datas': base64.b64encode(b'b' * size)},
            ])
        self.assertEqual(caught.exception.code, 'quota')
        self.assertEqual(self.document.search_count([]), before)

    def test_replacement_validates_bytes_and_records_audit(self):
        doc = self.document.create({
            'name': 'original.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'original'),
        })
        with self.assertRaises(WoowUploadRejected):
            doc.write({'name': 'bad.svg', 'datas': base64.b64encode(b'<svg/>')})
        self.assertEqual(base64.b64decode(doc.datas), b'original')
        doc.write({'datas': base64.b64encode(b'replacement')})
        self.assertEqual(base64.b64decode(doc.datas), b'replacement')
        self.assertEqual(self.env['woow.document.log'].sudo().search_count([
            ('event', '=', 'replace'), ('document_id', '=', doc.id)]), 1)

    def test_member_cannot_move_to_unshared_folder(self):
        doc = self.document.create({
            'name': 'move.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'content'),
        })
        other = self.env['woow.document.folder'].sudo().create({'name': 'Private target'})
        user = self.env['res.users'].sudo().create({
            'name': 'Editor', 'login': 'wdoc-editor',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_user').id])],
        })
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user',
            'user_id': user.id, 'level': 'edit',
        })
        self.env.flush_all()
        with self.assertRaises(AccessError):
            doc.with_user(user).write({'folder_id': other.id})
        self.assertEqual(doc.folder_id, self.folder)

    def test_legal_hold_refuses_permanent_delete(self):
        doc = self.document.create({
            'name': 'held.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'held file'),
        })
        doc.write({'legal_hold': True})
        with self.assertRaises(UserError):
            doc.unlink()
        self.assertTrue(doc.exists())

    def test_restored_date_inherits_latest_ancestor_restore(self):
        root = self.env['woow.document.folder'].sudo().create({'name': 'Restore root'})
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Restore child', 'parent_id': root.id})
        doc = self.document.create({
            'name': 'restored.txt', 'folder_id': child.id,
            'datas': base64.b64encode(b'content'),
        })
        earlier, later = '2026-01-01 00:00:00', '2026-01-02 00:00:00'
        root.write({'restored_date': earlier})
        self.assertEqual(str(doc.effective_restored_at), earlier)
        doc.write({'restored_date': later})
        self.assertEqual(str(doc.effective_restored_at), later)
        root.write({'restored_date': False})
        self.assertEqual(str(doc.effective_restored_at), later)

    def test_source_display_uses_reader_permissions(self):
        partner = self.env['res.partner'].sudo().create({'name': 'Readable source'})
        doc = self.document.create({
            'name': 'source.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'content'),
            'source_res_model': 'res.partner', 'source_res_id': partner.id,
        })
        self.assertEqual(doc.source_display, 'Readable source')
        user = self.env['res.users'].sudo().create({
            'name': 'Source reader', 'login': 'wdoc-source-reader',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_user').id])],
        })
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user',
            'user_id': user.id, 'level': 'read',
        })
        parameter = self.env['ir.config_parameter'].sudo().create({
            'key': 'woow-doc-source-fixture', 'value': 'secret source name'})
        doc.write({'source_res_model': 'ir.config_parameter',
                   'source_res_id': parameter.id})
        self.assertEqual(doc.with_user(user).source_display, 'No access')


@tagged('woow_docmgmt_core')
class TestTagPolicy(TransactionCase):

    def test_tag_creation_obeys_site_policy(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Tag editor', 'login': 'wdoc-tag-editor',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_user').id])],
        })
        tags = self.env['woow.document.tag'].with_user(user)
        with self.assertRaises(AccessError):
            tags.create({'name': 'Not allowed yet'})
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.tag_create_policy', 'users')
        tag = tags.create({'name': 'Allowed tag'})
        self.assertEqual(tag.name, 'Allowed tag')
