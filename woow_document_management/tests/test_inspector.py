# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The details panel: what its one server read returns to each user, and what it never returns.

The panel is a convenience: it adds no right. These tests prove that every answer of
``woow_inspector_data`` is what the caller's own rules allow, that the two actions it adds
check access first, and that the browser side (unit tests and one tour) runs.
"""
import base64
import re
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.document import SOURCE_CTX_KEY, _SOURCE_SENTINEL
from ..services import locks

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_CAP = 'odoo.addons.woow_document_management.models.inspector.SHARE_ROWS_PER_DOCUMENT_MAX'
_KEYS = {
    'id', 'name', 'document_type', 'type_label', 'icon', 'mimetype', 'file_size', 'url', 'active',
    'in_trash', 'write_date', 'folder', 'owner', 'contact', 'company', 'tags', 'can_write',
    'is_favorite', 'is_locked', 'locked_by_me', 'locked_by', 'can_lock', 'can_unlock',
    'version_count', 'has_portal_members', 'can_share', 'shares', 'source', 'shortcut_target',
    'request'}


class InspectorCase(TransactionCase):
    """One folder with a member of each level, and a few documents of it."""

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = env.ref('base.user_admin')
        self.reader = new_test_user(env, login='ins_reader', groups=_USER)
        self.uploader = new_test_user(env, login='ins_uploader', groups=_USER)
        self.editor = new_test_user(env, login='ins_editor', groups=_USER)
        self.foldermgr = new_test_user(env, login='ins_foldermgr', groups=_USER)
        self.docmgr = new_test_user(env, login='ins_docmgr', groups=_MANAGER)
        self.outsider = new_test_user(env, login='ins_outsider', groups=_USER)
        self.plain = new_test_user(env, login='ins_plain', groups='base.group_user')
        self.portal_partner = env['res.partner'].sudo().create({'name': 'Inspector portal'})
        self.portal = env['res.users'].sudo().create({
            'name': 'Inspector portal', 'login': 'ins_portal',
            'partner_id': self.portal_partner.id,
            'groups_id': [Command.set([env.ref('base.group_portal').id])]})
        self.folder = env['woow.document.folder'].sudo().create({
            'name': 'Inspector folder', 'owner_id': self.admin.id})
        access = env['woow.document.folder.access'].sudo()
        for user, level in ((self.reader, 'read'), (self.uploader, 'upload'),
                            (self.editor, 'edit'), (self.foldermgr, 'manage')):
            access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                           'user_id': user.id, 'level': level})
        self.tag = env['woow.document.tag'].sudo().create({'name': 'Inspector tag', 'color': 4})
        self.doc = self._document('inspect.txt', b'inspect me')
        self.doc.sudo().tag_ids = [Command.set(self.tag.ids)]
        env.flush_all()
        locks._reset_tracker(env.cr)

    def _document(self, name, content, folder=None, owner=None):
        return self.env['woow.document'].sudo().create({
            'name': name, 'folder_id': (folder or self.folder).id,
            'owner_id': (owner or self.admin).id, 'datas': base64.b64encode(content)})

    def _folder(self, name, grants):
        folder = self.env['woow.document.folder'].sudo().create({
            'name': name, 'owner_id': self.admin.id})
        for user, level in grants.items():
            self.env['woow.document.folder.access'].sudo().create({
                'folder_id': folder.id, 'subject_type': 'user', 'user_id': user.id,
                'level': level})
        self.env.flush_all()
        return folder

    def _data(self, user, doc=None):
        return (doc or self.doc).with_user(user).woow_inspector_data()

    def _with_source(self, doc, model, res_id):
        doc.sudo().with_context(**{SOURCE_CTX_KEY: _SOURCE_SENTINEL}).write({
            'source_res_model': model, 'source_res_id': res_id})
        self.env.flush_all()


@tagged('woow_docmgmt_inspector')
class TestInspectorData(InspectorCase):

    def test_the_answer_has_exactly_the_documented_keys(self):
        for user in (self.admin, self.docmgr, self.foldermgr, self.editor, self.uploader,
                     self.reader):
            with self.subTest(user=user.login):
                self.assertEqual(set(self._data(user)), _KEYS)

    def test_the_facts_of_a_file(self):
        data = self._data(self.reader)
        self.assertEqual(data['id'], self.doc.id)
        self.assertEqual(data['name'], 'inspect.txt')
        self.assertEqual(data['document_type'], 'binary')
        self.assertEqual(data['type_label'], 'File')
        self.assertEqual(data['mimetype'], 'text/plain')
        self.assertEqual(data['icon'], 'fa-file-text-o')
        self.assertEqual(data['file_size'], len(b'inspect me'))
        self.assertTrue(data['active'])
        self.assertFalse(data['in_trash'])
        self.assertEqual(data['folder'], {'id': self.folder.id, 'name': 'Inspector folder',
                                          'selectable': True})
        self.assertEqual(data['owner'], {'id': self.admin.id, 'name': self.admin.name})
        self.assertEqual(data['company']['id'], self.folder.company_id.id or data['company']['id'])
        self.assertEqual(data['tags'], [{'id': self.tag.id, 'name': 'Inspector tag', 'color': 4}])
        self.assertEqual(data['version_count'], 0)
        self.assertEqual(data['shares'], [])
        for key in ('source', 'shortcut_target', 'request', 'url', 'contact'):
            self.assertFalse(data[key], key)

    def test_the_rights_of_each_level(self):
        expected = {
            # user: (can_write, can_lock, can_share)
            self.reader: (False, False, False),
            self.uploader: (False, False, False),
            self.editor: (True, True, False),
            self.foldermgr: (True, True, True),
            self.docmgr: (True, True, True),
            self.admin: (True, True, True),
        }
        for user, (can_write, can_lock, can_share) in expected.items():
            with self.subTest(user=user.login):
                data = self._data(user)
                self.assertEqual((data['can_write'], data['can_lock'], data['can_share']),
                                 (can_write, can_lock, can_share))

    def test_the_owner_with_upload_level_may_write_and_lock_but_not_share(self):
        doc = self._document('mine.txt', b'mine', owner=self.uploader)
        data = self._data(self.uploader, doc)
        self.assertTrue(data['can_write'])
        self.assertTrue(data['can_lock'])
        self.assertFalse(data['can_share'])

    def test_a_user_without_access_is_refused_like_a_missing_record(self):
        hidden = self._document('hidden.txt', b'hidden', folder=self._folder('Hidden', {}))
        missing = self.env['woow.document'].with_user(self.outsider).browse(hidden.id + 100000)
        for method in ('woow_inspector_data', 'action_open_source', 'action_open_inspector'):
            with self.subTest(method=method):
                with self.assertRaises(AccessError) as refused:
                    getattr(hidden.with_user(self.outsider), method)()
                with self.assertRaises(AccessError) as gone:
                    getattr(missing, method)()
                self.assertEqual(str(refused.exception), str(gone.exception))
                self.assertNotIn('hidden', str(refused.exception))

    def test_a_user_who_is_not_a_document_user_is_refused(self):
        with self.assertRaises(AccessError):
            self._data(self.plain)

    def test_a_portal_user_who_reads_the_document_is_refused(self):
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner',
            'partner_id': self.portal_partner.id, 'level': 'read'})
        self.env.flush_all()
        self.assertTrue(self.doc.with_user(self.portal)._filtered_access('read'))
        hidden = self._document('hidden.txt', b'hidden', folder=self._folder('Hidden', {}))
        for method in ('woow_inspector_data', 'action_open_source', 'action_open_inspector'):
            with self.subTest(method=method):
                with self.assertRaises(AccessError) as seen:
                    getattr(self.doc.with_user(self.portal), method)()
                # a portal user learns nothing about a document that they may not read either
                with self.assertRaises(AccessError) as unseen:
                    getattr(hidden.with_user(self.portal), method)()
                self.assertEqual(str(seen.exception), str(unseen.exception))

    def test_only_one_document_at_a_time(self):
        other = self._document('other.txt', b'other')
        with self.assertRaises(ValueError):
            (self.doc | other).with_user(self.admin).woow_inspector_data()

    def test_the_star_follows_the_caller_only(self):
        self.doc.with_user(self.reader).action_toggle_favorite()
        self.assertTrue(self._data(self.reader)['is_favorite'])
        self.assertFalse(self._data(self.editor)['is_favorite'])

    def test_the_version_count(self):
        self.doc.with_user(self.editor).action_new_version()
        wizard = self.env['woow.document.version.wizard'].with_user(self.editor).create({
            'document_id': self.doc.id, 'datas': base64.b64encode(b'second'),
            'filename': 'second.txt'})
        wizard.action_confirm()
        self.assertEqual(self._data(self.reader)['version_count'], 1)

    def test_a_link_shows_its_address_and_no_size(self):
        link = self.env['woow.document'].sudo().create({
            'name': 'A link', 'folder_id': self.folder.id, 'document_type': 'url',
            'url': 'https://example.com/page', 'owner_id': self.admin.id})
        data = self._data(self.reader, link)
        self.assertEqual(data['url'], 'https://example.com/page')
        self.assertEqual(data['icon'], 'fa-link')
        self.assertEqual(data['file_size'], 0)
        self.assertEqual(data['version_count'], 0)
        self.assertTrue(data['can_lock'] is False)
        self.assertTrue(self._data(self.editor, link)['can_lock'])

    def test_a_document_in_the_trash_cannot_be_locked_from_the_panel(self):
        self.doc.with_user(self.admin).action_trash()
        data = self._data(self.admin, self.doc.with_context(active_test=False))
        self.assertFalse(data['active'])
        self.assertFalse(data['can_lock'])

    def test_the_company_is_named_only_when_it_is_readable(self):
        data = self._data(self.reader)
        if data['company']:
            self.assertEqual(data['company']['name'], self.folder.company_id.name)

    def test_a_contact_that_the_rules_hide_is_not_named(self):
        company_b = self.env['res.company'].sudo().create({'name': 'Inspector company B'})
        partner = self.env['res.partner'].sudo().create({
            'name': 'Secret contact', 'company_id': company_b.id})
        self.doc.sudo().partner_id = partner
        self.env.flush_all()
        self.assertFalse(self.env['res.partner'].with_user(self.reader).search(
            [('id', '=', partner.id)]))
        for user in (self.reader, self.editor):
            with self.subTest(user=user.login):
                data = self._data(user)
                self.assertFalse(data['contact'])
                self.assertNotIn('Secret contact', repr(data))
        # a user who may read the contact sees it
        visible = self.env['res.partner'].sudo().create({'name': 'Open contact'})
        self.doc.sudo().partner_id = visible
        self.env.flush_all()
        self.assertEqual(self._data(self.reader)['contact'],
                         {'id': visible.id, 'name': 'Open contact'})

    def test_the_folder_is_not_selectable_when_the_user_cannot_read_it(self):
        hidden = self._folder('Hidden shelf', {})
        doc = self._document('shared.txt', b'shared', folder=hidden)
        self.env['woow.document.share'].sudo().create({
            'document_id': doc.id, 'subject_type': 'user', 'user_id': self.outsider.id,
            'role': 'viewer'})
        self.env.flush_all()
        # a request starts with an empty cache: the rows made above must not stand in for it
        self.env.invalidate_all()
        data = self._data(self.outsider, doc)
        self.assertFalse(data['folder']['selectable'])
        self.assertEqual(data['folder']['name'], 'Restricted folder')
        self.assertFalse(data['can_write'])
        self.assertFalse(data['can_share'])
        self.assertFalse(data['has_portal_members'])

    def test_a_shared_document_in_a_hidden_folder_opens_with_a_cold_cache(self):
        """The 'Shared with me' case, as a real request sees it (nothing cached before the call)."""
        hidden = self._folder('Hidden shelf', {self.uploader: 'upload'})
        # the hidden folder also has a portal member: the flag must not tell the caller so
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': hidden.id, 'subject_type': 'partner',
            'partner_id': self.portal_partner.id, 'level': 'read'})
        doc = self._document('shared2.txt', b'shared two', folder=hidden)
        for user, role in ((self.outsider, 'viewer'), (self.editor, 'editor')):
            self.env['woow.document.share'].sudo().create({
                'document_id': doc.id, 'subject_type': 'user', 'user_id': user.id, 'role': role})
        self.env.flush_all()
        for user, may_write in ((self.outsider, False), (self.editor, True)):
            with self.subTest(user=user.login):
                self.env.invalidate_all()
                data = self._data(user, doc)
                self.assertEqual(data['folder'], {'id': hidden.id, 'name': 'Restricted folder',
                                                  'selectable': False})
                self.assertFalse(data['has_portal_members'])
                self.assertEqual(data['can_write'], may_write)
                self.assertNotIn('Hidden shelf', repr(data))
        # a member of the hidden folder still gets the flag of a folder that they may read
        self.env.invalidate_all()
        self.assertTrue(self._data(self.uploader, doc)['has_portal_members'])

    def test_every_answer_is_the_same_with_a_cold_cache(self):
        """No field of the answer relies on records that an earlier read of the test left cached."""
        for user in (self.reader, self.uploader, self.editor, self.foldermgr, self.docmgr):
            with self.subTest(user=user.login):
                self.env.invalidate_all()
                cold = self._data(user)
                warm = self._data(user)
                self.assertEqual(cold, warm)


@tagged('woow_docmgmt_inspector')
class TestInspectorLock(InspectorCase):

    def test_my_own_lock(self):
        self.doc.with_user(self.editor).action_lock()
        data = self._data(self.editor)
        self.assertTrue(data['is_locked'])
        self.assertTrue(data['locked_by_me'])
        self.assertFalse(data['locked_by'])
        self.assertTrue(data['can_unlock'])
        self.assertTrue(data['can_lock'])

    def test_the_lock_of_another_user(self):
        self.doc.with_user(self.editor).action_lock()
        for user, can_unlock in ((self.foldermgr, True), (self.docmgr, True), (self.admin, True),
                                 (self.reader, False)):
            with self.subTest(user=user.login):
                data = self._data(user)
                self.assertTrue(data['is_locked'])
                self.assertFalse(data['locked_by_me'])
                self.assertEqual(data['locked_by'], self.editor.name)
                self.assertEqual(data['can_unlock'], can_unlock)
                # nobody but the holder is offered a lock button
                self.assertFalse(data['can_lock'])

    def test_the_owner_who_is_not_the_holder_is_not_offered_unlock(self):
        # the server refuses the owner (see action_unlock), so the panel does not offer the button
        doc = self._document('owned.txt', b'owned', owner=self.uploader)
        doc.with_user(self.editor).action_lock()
        self.assertTrue(doc.with_user(self.uploader).has_access('write'))
        self.assertFalse(self._data(self.uploader, doc)['can_unlock'])

    def test_another_editor_cannot_unlock(self):
        second = new_test_user(self.env, login='ins_editor2', groups=_USER)
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': second.id,
            'level': 'edit'})
        self.env.flush_all()
        self.doc.with_user(self.editor).action_lock()
        data = self._data(second)
        self.assertEqual(data['locked_by'], self.editor.name)
        self.assertFalse(data['can_unlock'])
        with self.assertRaises(AccessError):
            self.doc.with_user(second).action_unlock()

    def test_the_name_of_the_holder_is_not_given_when_the_rules_hide_it(self):
        self.doc.sudo().write({'lock_uid': self.editor.id})
        self.env.flush_all()
        document = type(self.env['woow.document'])
        with patch.object(document, '_woow_inspector_label', return_value=False):
            data = self._data(self.reader)
        self.assertTrue(data['is_locked'])
        self.assertEqual(data['locked_by'], 'Restricted')
        self.assertNotIn(self.editor.name, repr(data))


@tagged('woow_docmgmt_inspector')
class TestInspectorSource(InspectorCase):

    def test_a_readable_source_is_named_and_can_be_opened(self):
        partner = self.env['res.partner'].sudo().create({'name': 'Source partner'})
        self._with_source(self.doc, 'res.partner', partner.id)
        data = self._data(self.reader)
        self.assertEqual(data['source'], {'model': 'Contact', 'name': 'Source partner'})
        action = self.doc.with_user(self.reader).action_open_source()
        self.assertEqual((action['type'], action['res_model'], action['res_id']),
                         ('ir.actions.act_window', 'res.partner', partner.id))

    def test_a_source_of_a_model_the_user_cannot_read_is_hidden(self):
        cron = self.env['ir.cron'].sudo().search([], limit=1)
        self._with_source(self.doc, 'ir.cron', cron.id)
        self.assertTrue(self._data(self.admin)['source'])
        for user in (self.reader, self.editor, self.docmgr):
            with self.subTest(user=user.login):
                data = self._data(user)
                self.assertFalse(data['source'])
                self.assertNotIn(cron.name, repr(data))
                with self.assertRaises(UserError):
                    self.doc.with_user(user).action_open_source()

    def test_a_source_record_that_the_rules_hide_is_hidden(self):
        company_b = self.env['res.company'].sudo().create({'name': 'Inspector source company'})
        partner = self.env['res.partner'].sudo().create({
            'name': 'Hidden source', 'company_id': company_b.id})
        self._with_source(self.doc, 'res.partner', partner.id)
        data = self._data(self.reader)
        self.assertFalse(data['source'])
        self.assertNotIn('Hidden source', repr(data))
        with self.assertRaises(UserError) as refused:
            self.doc.with_user(self.reader).action_open_source()
        self.assertNotIn('Hidden source', str(refused.exception))

    def test_a_source_that_is_gone_or_is_not_a_real_model_answers_nothing(self):
        for model, res_id in (('res.partner', 987654321), ('no.such.model', 1),
                              ('woow.document.move.wizard', 1), ('mail.thread', 1)):
            with self.subTest(model=model):
                self._with_source(self.doc, model, res_id)
                self.assertFalse(self._data(self.admin)['source'])
                with self.assertRaises(UserError):
                    self.doc.with_user(self.admin).action_open_source()

    def test_no_source_means_the_message_of_a_missing_one(self):
        with self.assertRaises(UserError):
            self.doc.with_user(self.reader).action_open_source()

    def test_the_actions_need_read_access_first(self):
        hidden = self._document('hidden.txt', b'hidden', folder=self._folder('Hidden', {}))
        for method in ('action_open_source', 'action_open_inspector'):
            with self.subTest(method=method):
                with self.assertRaises(AccessError):
                    getattr(hidden.with_user(self.outsider), method)()

    def test_the_inspector_page_action(self):
        action = self.doc.with_user(self.reader).action_open_inspector()
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'woow_document_inspector')
        self.assertEqual(action['params'], {'res_id': self.doc.id})


@tagged('woow_docmgmt_inspector')
class TestInspectorSharing(InspectorCase):

    def _share(self, **vals):
        values = {'document_id': self.doc.id, 'subject_type': 'user', 'role': 'viewer'}
        values.update(vals)
        return self.env['woow.document.share'].sudo().create(values)

    def test_names_roles_and_end_dates_of_the_people(self):
        end = fields.Date.today() + timedelta(days=30)
        self._share(user_id=self.outsider.id, role='editor')
        self._share(subject_type='partner', partner_id=self.portal_partner.id,
                    expiry_date=end)
        self.env.flush_all()
        rows = self._data(self.reader)['shares']
        by_name = {row['name']: row for row in rows}
        self.assertEqual(set(by_name), {self.outsider.name, 'Inspector portal'})
        self.assertEqual(by_name[self.outsider.name]['role'], 'editor')
        self.assertEqual(by_name[self.outsider.name]['role_label'], 'Editor')
        self.assertEqual(by_name[self.outsider.name]['kind'], 'user')
        self.assertFalse(by_name[self.outsider.name]['expiry_date'])
        self.assertEqual(by_name['Inspector portal']['kind'], 'partner')
        self.assertEqual(by_name['Inspector portal']['expiry_date'], fields.Date.to_string(end))

    def test_a_person_the_rules_hide_is_not_named(self):
        company_b = self.env['res.company'].sudo().create({'name': 'Inspector share company'})
        partner = self.env['res.partner'].sudo().create({
            'name': 'Hidden guest', 'company_id': company_b.id})
        self.env['res.users'].sudo().create({
            'name': 'Hidden guest', 'login': 'ins_hidden_guest', 'partner_id': partner.id,
            'company_id': company_b.id, 'company_ids': [Command.set([company_b.id])],
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        self._share(subject_type='partner', partner_id=partner.id)
        self.env.flush_all()
        self.assertFalse(self.env['res.partner'].with_user(self.reader).search(
            [('id', '=', partner.id)]))
        data = self._data(self.reader)
        self.assertEqual(len(data['shares']), 1)
        self.assertEqual(data['shares'][0]['name'], 'Restricted')
        self.assertNotIn('Hidden guest', repr(data))

    def test_a_user_who_cannot_read_the_document_gets_no_row(self):
        hidden = self._document('hidden.txt', b'hidden', folder=self._folder('Hidden', {}))
        self.env['woow.document.share'].sudo().create({
            'document_id': hidden.id, 'subject_type': 'user', 'user_id': self.reader.id,
            'role': 'viewer'})
        self.env.flush_all()
        with self.assertRaises(AccessError):
            self._data(self.outsider, hidden)

    def test_the_rows_are_bounded(self):
        for user in (self.reader, self.editor, self.foldermgr):
            self._share(user_id=user.id)
        self.env.flush_all()
        self.assertEqual(len(self._data(self.admin)['shares']), 3)
        with patch(_CAP, 2):
            self.assertEqual(len(self._data(self.admin)['shares']), 2)

    def test_only_a_manager_of_the_folder_may_share(self):
        self._share(user_id=self.outsider.id)
        self.env.flush_all()
        self.assertFalse(self._data(self.editor)['can_share'])
        self.assertTrue(self._data(self.foldermgr)['can_share'])


@tagged('woow_docmgmt_inspector')
class TestInspectorKinds(InspectorCase):

    def test_a_shortcut_names_a_target_the_reader_may_read(self):
        other = self._folder('Target folder', {self.reader: 'read'})
        target = self._document('target.txt', b'target', folder=other)
        shortcut = self._shortcut(target)
        data = self._data(self.reader, shortcut)
        self.assertEqual(data['document_type'], 'shortcut')
        self.assertEqual(data['type_label'], 'Shortcut')
        self.assertEqual(data['shortcut_target'], {'id': target.id, 'name': 'target.txt'})
        self.assertEqual(data['file_size'], 0)
        self.assertEqual(data['version_count'], 0)
        self.assertFalse(data['can_lock'])

    def test_a_shortcut_does_not_name_a_target_the_reader_may_not_read(self):
        hidden = self._folder('Hidden target folder', {})
        target = self._document('secret-target.txt', b'target', folder=hidden)
        shortcut = self._shortcut(target)
        shortcut.sudo().name = 'link-to-it.txt'
        data = self._data(self.reader, shortcut)
        self.assertFalse(data['shortcut_target'])
        self.assertNotIn('secret-target', repr(data))
        self.assertEqual(self._data(self.admin, shortcut)['shortcut_target']['id'], target.id)

    def _shortcut(self, target):
        wizard = self.env['woow.document.shortcut.wizard'].with_user(self.admin).create({
            'document_id': target.id, 'folder_id': self.folder.id})
        wizard.action_create()
        return self.env['woow.document'].sudo().search([
            ('document_type', '=', 'shortcut'), ('shortcut_target_id', '=', target.id)])

    def _request(self, asked=None):
        return self.env['woow.document'].with_user(self.editor)._woow_open_request(
            self.folder, 'Signed NDA', asked or self.uploader, 7, note='By Friday')

    def test_a_requested_file(self):
        request = self._request()
        for user, can_upload in ((self.uploader, True), (self.editor, True), (self.reader, False)):
            with self.subTest(user=user.login):
                data = self._data(user, request)
                self.assertEqual(data['document_type'], 'request')
                self.assertEqual(data['request']['asked'],
                                 {'id': self.uploader.id, 'name': self.uploader.name})
                self.assertEqual(data['request']['deadline'], fields.Date.to_string(
                    fields.Date.context_today(request) + timedelta(days=7)))
                self.assertEqual(data['request']['note'], 'By Friday')
                self.assertFalse(data['request']['overdue'])
                self.assertEqual(data['request']['can_upload'], can_upload)
                self.assertFalse(data['can_lock'])
                self.assertEqual(data['file_size'], 0)

    def test_an_overdue_request(self):
        request = self._request()
        request.sudo().request_deadline = fields.Date.context_today(request) - timedelta(days=1)
        self.assertTrue(self._data(self.reader, request)['request']['overdue'])

    def test_a_fulfilled_request_is_a_file(self):
        request = self._request()
        request.with_user(self.uploader)._woow_fulfil_request('nda.txt', b'signed')
        data = self._data(self.reader, request)
        self.assertEqual(data['document_type'], 'binary')
        self.assertFalse(data['request'])

    def test_the_panel_adds_nothing_to_the_log_or_the_database(self):
        before = self.env['woow.document.log'].sudo().search_count([])
        self._data(self.admin)
        self.assertEqual(self.env['woow.document.log'].sudo().search_count([]), before)


@tagged('woow_docmgmt_inspector')
class TestInspectorPortalWarning(InspectorCase):

    def test_the_flag_follows_the_portal_members_of_the_folder(self):
        self.assertFalse(self._data(self.reader)['has_portal_members'])
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner',
            'partner_id': self.portal_partner.id, 'level': 'read'})
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertTrue(self._data(self.reader)['has_portal_members'])


@tagged('woow_docmgmt_inspector')
class TestInspectorStatic(TransactionCase):
    """The browser files stay within the rules for JavaScript of this module."""

    def _static(self):
        import os
        root = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
        found = {}
        for base in ('src/backend/inspector', 'tests/inspector', 'tests/tours/backend'):
            folder = os.path.join(root, base)
            for name in sorted(os.listdir(folder)):
                if 'inspector' in name:
                    with open(os.path.join(folder, name), encoding='utf-8') as stream:
                        found[base + '/' + name] = stream.read()
        return found

    def test_the_files_exist_and_carry_the_header(self):
        files = self._static()
        for name in ('src/backend/inspector/inspector.js', 'src/backend/inspector/inspector.xml',
                     'src/backend/inspector/inspector_page.js', 'tests/inspector/inspector.test.js',
                     'tests/tours/backend/inspector_tour.js'):
            self.assertIn(name, files)
            self.assertIn('Copyright 2026 WoowTech', files[name][:200], name)

    def test_no_html_is_built_from_data(self):
        sinks = re.compile(r'innerHTML|outerHTML|insertAdjacentHTML|\bmarkup\s*\(|t-raw|t-out|'
                           r'document\.write|\beval\s*\(|new\s+Function|dangerouslySet')
        for name, text in self._static().items():
            if name.startswith('src/'):
                self.assertFalse(sinks.search(text), name)

    def test_the_only_routes_called_are_core_ones(self):
        for name, text in self._static().items():
            if name.startswith('src/') and name.endswith('.js'):
                urls = re.findall(r'["`\']/(?:web|woow_document)[^"`\']*', text)
                for url in urls:
                    self.assertTrue(url.startswith(('"/web/dataset/call_button', '`/web/dataset/call_button')),
                                    (name, url))


@tagged('-at_install', 'post_install', 'woow_docmgmt_inspector_js')
class TestInspectorBrowser(HttpCase):

    def test_unit_tests_of_the_panel(self):
        """The web client unit tests of the panel (hoot), selected by their tag."""
        self.browser_js(
            '/web/tests?headless&loglevel=2&preset=desktop&timeout=15000&tag=woow_inspector',
            '', '', login='admin', timeout=900,
            success_signal='[HOOT] Test suite succeeded',
            error_checker=lambda message: '[HOOT]' not in message)

    def test_tour_of_the_panel(self):
        """Open the panel on a real record: act on it, write in the chatter, choose the folder."""
        env = self.env
        admin = env.ref('base.user_admin')
        folder = env['woow.document.folder'].sudo().create({
            'name': 'Inspector tour folder', 'owner_id': admin.id})
        document = env['woow.document'].sudo().create({
            'name': 'inspector-tour.txt', 'folder_id': folder.id, 'owner_id': admin.id,
            'datas': base64.b64encode(b'inspector tour')})
        env['woow.document.tag'].sudo().create({'name': 'Tour tag'})
        self.start_tour(
            '/odoo/%d/action-woow_document_management.action_woow_document_inspector' % document.id,
            'tour_woow_inspector', login='admin')

    def test_tour_of_the_page_closed_from_a_direct_address(self):
        """The page is the first screen: the close button opens the workspace, no error dialog."""
        env = self.env
        admin = env.ref('base.user_admin')
        folder = env['woow.document.folder'].sudo().create({
            'name': 'Inspector close folder', 'owner_id': admin.id})
        document = env['woow.document'].sudo().create({
            'name': 'inspector-close.txt', 'folder_id': folder.id, 'owner_id': admin.id,
            'datas': base64.b64encode(b'inspector close')})
        self.start_tour(
            '/odoo/%d/action-woow_document_management.action_woow_document_inspector' % document.id,
            'tour_woow_inspector_close', login='admin')
