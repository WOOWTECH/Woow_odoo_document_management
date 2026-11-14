# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Where the lanes of release 18.0.2 meet: locks, the kinds of document, share users, sharing.

Each lane tests its own feature. These tests prove what only the merged module can: that the
lock of one user holds back the features of the other lanes, that the kinds of document made by
some lanes are refused by the features of the others, and that the global rules which hide those
kinds from share users all stand.
"""
import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..services import locks
from .test_pdftools import raw_pdf

_USER = 'woow_document_management.group_woow_document_user'
_UPLOAD_TYPE = 'mail.mail_activity_data_upload_document'


def _b64(data):
    return base64.b64encode(data)


class _Case(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = env.ref('base.user_admin')
        self.alice = new_test_user(env, login='int_alice', groups=_USER, email='alice@example.test')
        self.bob = new_test_user(env, login='int_bob', groups=_USER, email='bob@example.test')
        self.carol = new_test_user(env, login='int_carol', groups=_USER)
        self.portal = new_test_user(env, login='int_portal', groups='base.group_portal')
        folders = env['woow.document.folder'].sudo()
        self.folder = folders.create({'name': 'Integration folder', 'owner_id': self.admin.id})
        self.other = folders.create({'name': 'Integration other', 'owner_id': self.admin.id})
        for folder in (self.folder, self.other):
            for user in (self.alice, self.bob):
                self._grant(folder, user, 'edit')
        self._grant(self.folder, self.carol, 'read')
        env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner',
            'partner_id': self.portal.partner_id.id, 'level': 'read'})
        self.model = env['woow.document']
        self.doc = self._doc('plain.txt', b'plain content')
        env.flush_all()
        # the folders above took locks of a higher rank than the quota lock in this transaction
        locks._reset_tracker(env.cr)

    def _grant(self, folder, user, level):
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user', 'user_id': user.id, 'level': level})

    def _doc(self, name, data, folder=None, **vals):
        return self.model.sudo().create(dict({
            'name': name, 'folder_id': (folder or self.folder).id, 'datas': _b64(data),
            'owner_id': self.alice.id}, **vals))

    def _pdf(self, name='int.pdf'):
        return self._doc(name, raw_pdf([101, 102, 103]))

    def _request(self):
        return self.model.with_user(self.alice)._woow_open_request(
            self.folder, 'Signed contract', self.bob, 7)

    def _shortcut(self, target=None):
        target = target or self.doc
        wizard = self.env['woow.document.shortcut.wizard'].with_user(self.alice).create({
            'document_id': target.id, 'folder_id': self.other.id})
        wizard.action_create()
        return self.model.sudo().search([('document_type', '=', 'shortcut'),
                                         ('shortcut_target_id', '=', target.id)])


@tagged('woow_docmgmt_integration')
class TestLockHoldsBackTheOtherLanes(_Case):
    """Another user's lock stops what changes the locked document and nothing else."""

    def setUp(self):
        super().setUp()
        self.doc.with_user(self.alice).action_lock()

    def _version_wizard(self, user, doc):
        return self.env['woow.document.version.wizard'].with_user(user).create({
            'document_id': doc.id, 'datas': _b64(b'next content'), 'filename': 'next.txt'})

    def test_the_lock_fields_exist_in_the_merged_module(self):
        for name in ('lock_uid', 'lock_date', 'is_locked'):
            self.assertIn(name, self.model._fields)
        self.assertEqual(self.doc.lock_uid, self.alice)

    def test_a_new_version_is_refused_for_another_user_and_allowed_for_the_holder(self):
        with self.assertRaises(UserError):
            self._version_wizard(self.bob, self.doc).action_confirm()
        self.assertEqual(self.doc.sudo().version_count, 0)
        self.assertEqual(base64.b64decode(self.doc.datas), b'plain content')
        self._version_wizard(self.alice, self.doc).action_confirm()
        self.assertEqual(self.doc.sudo().version_count, 1)

    def test_a_restore_is_refused_for_another_user(self):
        self._version_wizard(self.alice, self.doc).action_confirm()
        version = self.env['woow.document.version'].sudo().search([('document_id', '=', self.doc.id)])
        with self.assertRaises(UserError):
            version.with_user(self.bob).action_restore()
        self.assertEqual(base64.b64decode(self.doc.datas), b'next content')

    def test_a_rename_is_refused_for_another_user(self):
        wizard = self.env['woow.document.rename.wizard'].with_user(self.bob).create({
            'document_id': self.doc.id, 'name': 'taken.txt'})
        with self.assertRaises(UserError):
            wizard.action_rename()
        self.assertEqual(self.doc.name, 'plain.txt')

    def test_a_requested_file_upload_is_refused_while_another_user_holds_the_lock(self):
        request = self._request()
        request.with_user(self.alice).action_lock()
        wizard = self.env['woow.document.request.fulfil.wizard'].with_user(self.bob).create({
            'document_id': request.id, 'file': _b64(b'contract text'), 'filename': 'contract.txt'})
        with self.assertRaises(UserError):
            wizard.action_fulfil()
        self.assertEqual(request.document_type, 'request')
        self.assertFalse(request.sudo().datas)
        request.with_user(self.alice).action_unlock()
        wizard.action_fulfil()
        self.assertEqual(request.document_type, 'binary')

    def test_the_holder_of_a_lock_on_a_request_may_fulfil_it(self):
        request = self._request()
        request.with_user(self.bob).action_lock()
        self.model.with_user(self.bob).browse(request.id)._woow_fulfil_request(
            'contract.txt', b'contract text')
        self.assertEqual(request.document_type, 'binary')

    def test_the_trash_option_of_a_pdf_tool_is_refused_for_the_locked_source(self):
        source = self._pdf()
        source.with_user(self.alice).action_lock()
        wizard = self.env['woow.document.pdf.split.wizard'].with_user(self.bob).create({
            'document_id': source.id, 'ranges': '1-2', 'trash_original': True})
        with self.assertRaises(UserError):
            wizard.action_split()
        self.assertTrue(source.active)
        count = self.model.sudo().search_count([('folder_id', '=', self.folder.id)])
        merge = self.env['woow.document.pdf.merge.wizard'].with_user(self.bob).create({
            'line_ids': [(0, 0, {'document_id': source.id, 'sequence': 10}),
                         (0, 0, {'document_id': self._pdf('two.pdf').id, 'sequence': 20})],
            'folder_id': self.other.id, 'name': 'merged.pdf', 'trash_originals': True})
        with self.assertRaises(UserError):
            merge.action_merge()
        self.assertTrue(source.active)
        self.assertEqual(self.model.sudo().search_count([('folder_id', '=', self.folder.id)]),
                         count + 1)   # only the second source made above

    def test_reading_the_locked_file_is_not_held_back(self):
        source = self._pdf()
        source.with_user(self.alice).action_lock()
        action = self.env['woow.document.pdf.split.wizard'].with_user(self.bob).create({
            'document_id': source.id, 'ranges': '1-2'}).action_split()
        made = self.model.sudo().search(action['params']['next']['domain'])
        self.assertTrue(made)
        self.assertFalse(made.mapped('lock_uid'))    # a lock is never copied to what is made
        copy = self.doc.with_user(self.bob).action_duplicate()
        self.assertFalse(self.model.sudo().browse(copy['res_id']).lock_uid)
        self.assertEqual(self.doc.lock_uid, self.alice)

    def test_a_shortcut_to_a_locked_file_can_be_made_and_is_not_locked(self):
        shortcut = self._shortcut()
        self.assertEqual(len(shortcut), 1)
        self.assertFalse(shortcut.lock_uid)
        self.assertEqual(self.doc.lock_uid, self.alice)
        # the target is unchanged by the shortcut, and still held back for the other user
        with self.assertRaises(UserError):
            self.doc.with_user(self.bob).write({'name': 'other.txt'})


@tagged('woow_docmgmt_integration')
class TestKindsOfDocumentAreRefusedByOtherFeatures(_Case):
    """A shortcut and a requested file have no content: duplicating, versions and PDF tools refuse."""

    def setUp(self):
        super().setUp()
        self.shortcut = self._shortcut()
        self.request = self._request()

    def test_duplicate_refuses_both(self):
        for record in (self.shortcut, self.request):
            with self.assertRaises(UserError):
                record.with_user(self.alice).action_duplicate()

    def test_a_new_version_is_refused_for_both(self):
        for record in (self.shortcut, self.request):
            with self.assertRaises(UserError):
                record.with_user(self.alice).action_new_version()
            wizard = self.env['woow.document.version.wizard'].with_user(self.alice).create({
                'document_id': record.id, 'datas': _b64(b'next'), 'filename': 'next.txt'})
            with self.assertRaises(UserError):
                wizard.action_confirm()
            self.assertFalse(self.env['woow.document.version'].sudo().search(
                [('document_id', '=', record.id)]))

    def test_the_pdf_tools_refuse_both(self):
        pdf = self._pdf()
        for record in (self.shortcut, self.request):
            with self.assertRaises(UserError):
                self.env['woow.document.pdf.split.wizard'].with_user(self.alice).create({
                    'document_id': record.id, 'ranges': '1'}).action_split()
            with self.assertRaises(UserError):
                self.env['woow.document.pdf.merge.wizard'].with_user(self.alice).create({
                    'line_ids': [(0, 0, {'document_id': record.id, 'sequence': 10}),
                                 (0, 0, {'document_id': pdf.id, 'sequence': 20})],
                    'folder_id': self.other.id, 'name': 'merged.pdf'}).action_merge()

    def test_a_shortcut_cannot_point_to_a_requested_file(self):
        with self.assertRaises(UserError):
            self.request.with_user(self.alice).action_open_shortcut_wizard()
        with self.assertRaises(UserError):
            self.env['woow.document.shortcut.wizard'].with_user(self.alice).create({
                'document_id': self.request.id, 'folder_id': self.other.id}).action_create()
        with self.assertRaises(UserError):
            self.model.sudo().create({
                'name': 'bad', 'folder_id': self.other.id, 'document_type': 'shortcut',
                'shortcut_target_id': self.request.id})
        # nor to another shortcut
        with self.assertRaises(UserError):
            self.shortcut.with_user(self.alice).action_open_shortcut_wizard()

    def test_both_count_zero_bytes_and_have_no_file(self):
        for record in (self.shortcut, self.request):
            self.assertFalse(record.sudo().attachment_id)
            self.assertFalse(record.sudo().file_size)
            self.assertFalse(record.sudo().datas)

    def test_a_fulfilled_request_becomes_an_ordinary_file_that_can_be_versioned(self):
        self.model.with_user(self.bob).browse(self.request.id)._woow_fulfil_request(
            'contract.txt', b'contract text')
        self.assertEqual(self.request.document_type, 'binary')
        wizard = self.env['woow.document.version.wizard'].with_user(self.bob).create({
            'document_id': self.request.id, 'datas': _b64(b'second'), 'filename': 'second.txt'})
        wizard.action_confirm()
        self.assertEqual(self.request.sudo().version_count, 1)


@tagged('woow_docmgmt_integration')
class TestShareUsersNeverSeeTheNewKinds(_Case):
    """Both global record rules stand; a share user sees the plain file and neither kind."""

    def test_both_global_rules_exist_and_are_global(self):
        for xmlid in ('rule_woow_document_shortcut_share_users', 'rule_woow_document_request_share_users'):
            rule = self.env.ref('woow_document_management.' + xmlid)
            self.assertFalse(rule.groups, xmlid)
            self.assertEqual(rule.model_id.model, 'woow.document')
            self.assertTrue(rule.perm_read and rule.perm_write and rule.perm_create and rule.perm_unlink)

    def test_a_portal_user_with_folder_access_sees_only_the_plain_file(self):
        shortcut = self._shortcut()
        request = self._request()
        self.env.flush_all()
        portal_model = self.model.with_user(self.portal)
        visible = portal_model.search([('folder_id', '=', self.folder.id)])
        self.assertEqual(visible, self.doc)
        self.assertFalse(portal_model.search([('id', 'in', (request | self.doc).ids),
                                              ('document_type', '=', 'request')]))
        # an internal reader with the same folder access sees the request but never the shortcut's
        # target through it; internal users are not held back by the global rules
        internal = self.model.with_user(self.carol).search([('folder_id', '=', self.folder.id)])
        self.assertIn(request, internal)
        self.assertIn(self.doc, internal)
        self.assertEqual(shortcut.folder_id, self.other)


@tagged('woow_docmgmt_integration')
class TestWorkspaceSectionsFollowTheSharingLists(_Case):
    """The "shared with me" section of the workspace reads the lists that the share rows keep."""

    def _section(self, user, value='shared'):
        return self.model.with_user(user).search([('woow_section', '=', value)])

    def test_a_row_shares_the_document_and_deleting_it_stops_the_sharing(self):
        outsider = new_test_user(self.env, login='int_outsider', groups=_USER)
        self.assertNotIn(self.doc, self._section(outsider))
        row = self.env['woow.document.share'].with_user(self.admin).create({
            'document_id': self.doc.id, 'subject_type': 'user', 'user_id': outsider.id,
            'role': 'viewer'})
        self.assertIn(outsider, self.doc.sudo().shared_user_ids)
        self.assertNotIn(outsider, self.doc.sudo().shared_editor_user_ids)
        self.assertIn(self.doc, self._section(outsider))
        row.with_user(self.admin).write({'role': 'editor'})
        self.assertIn(outsider, self.doc.sudo().shared_editor_user_ids)
        self.assertIn(self.doc, self._section(outsider))
        self.assertNotIn(self.doc, self._section(outsider, 'mine'))
        row.with_user(self.admin).unlink()
        self.assertNotIn(outsider, self.doc.sudo().shared_user_ids)
        self.assertNotIn(self.doc, self._section(outsider))

    def test_the_old_direct_list_is_mirrored_into_rows_and_the_section_sees_it(self):
        outsider = new_test_user(self.env, login='int_outsider2', groups=_USER)
        self.doc.with_user(self.admin).write({'shared_user_ids': [(4, outsider.id)]})
        rows = self.env['woow.document.share'].sudo().search([('document_id', '=', self.doc.id)])
        self.assertEqual(rows.user_id, outsider)
        self.assertEqual(rows.role, 'viewer')
        self.assertIn(self.doc, self._section(outsider))


@tagged('woow_docmgmt_integration')
class TestAContentReplacementByWriteKeepsAVersionAndRespectsTheLock(_Case):
    """A direct write of the content keeps a version, after the lock and the share checks."""

    def _replace(self, user, doc, data=b'replaced content'):
        return doc.with_user(user).write({'datas': _b64(data)})

    def test_a_replacement_by_write_keeps_the_old_content_as_a_version(self):
        self._replace(self.alice, self.doc)
        self.assertEqual(base64.b64decode(self.doc.datas), b'replaced content')
        versions = self.env['woow.document.version'].sudo().search([('document_id', '=', self.doc.id)])
        self.assertEqual(len(versions), 1)
        self.assertEqual(base64.b64decode(versions.datas), b'plain content')

    def test_another_users_lock_refuses_a_replacement_by_write_and_keeps_everything(self):
        self.doc.with_user(self.alice).action_lock()
        with self.assertRaises(UserError):
            self._replace(self.bob, self.doc)
        self.assertEqual(base64.b64decode(self.doc.datas), b'plain content')
        self.assertFalse(self.env['woow.document.version'].sudo().search(
            [('document_id', '=', self.doc.id)]))
        self._replace(self.alice, self.doc)       # the holder may
        self.assertEqual(self.doc.sudo().version_count, 1)

    def test_an_editor_by_share_replaces_the_content_and_a_version_is_kept(self):
        outsider = new_test_user(self.env, login='int_share_editor', groups=_USER)
        self.env['woow.document.share'].with_user(self.admin).create({
            'document_id': self.doc.id, 'subject_type': 'user', 'user_id': outsider.id,
            'role': 'editor'})
        self._replace(outsider, self.doc)
        self.assertEqual(base64.b64decode(self.doc.sudo().datas), b'replaced content')
        self.assertEqual(self.doc.sudo().version_count, 1)
        # the editor by share still cannot read, change or remove the history
        versions = self.env['woow.document.version'].with_user(outsider)
        self.assertFalse(versions.search([('document_id', '=', self.doc.id)]))
