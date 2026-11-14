# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Earlier versions of a file: who may keep, restore, delete and download them."""
import base64
import io
import secrets
import threading
import time
import zipfile
from unittest.mock import patch

from psycopg2 import errors

from odoo import Command, SUPERUSER_ID, api
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from ..hooks import uninstall_hook
from ..models.upload_policy import WoowUploadRejected
from ..services import locks
from ..services.constants import MAX_VERSIONS_PER_DOCUMENT, WOOW_MODELS

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_CAP = 'odoo.addons.woow_document_management.models.versions.MAX_VERSIONS_PER_DOCUMENT'


class _Fixture:
    """One folder with a member of each level, a document of it, and the people around it."""

    def _fixture(self, suffix='v'):
        env = self.env
        self.admin = env.ref('base.user_admin')
        self.reader = new_test_user(env, login='wver-reader-' + suffix, groups=_USER)
        self.uploader = new_test_user(env, login='wver-uploader-' + suffix, groups=_USER)
        self.editor = new_test_user(env, login='wver-editor-' + suffix, groups=_USER)
        self.foldermgr = new_test_user(env, login='wver-foldermgr-' + suffix, groups=_USER)
        self.docmgr = new_test_user(env, login='wver-docmgr-' + suffix, groups=_MANAGER)
        self.outsider = new_test_user(env, login='wver-outsider-' + suffix, groups=_USER)
        self.plain = new_test_user(env, login='wver-plain-' + suffix, groups='base.group_user')
        self.portal_partner = env['res.partner'].sudo().create({'name': 'Versions portal ' + suffix})
        self.portal = env['res.users'].sudo().create({
            'name': 'Versions portal ' + suffix, 'login': 'wver-portal-' + suffix,
            'partner_id': self.portal_partner.id,
            'groups_id': [Command.set([env.ref('base.group_portal').id])]})
        self.folder = env['woow.document.folder'].sudo().create({
            'name': 'Versions folder ' + suffix, 'owner_id': self.admin.id})
        access = env['woow.document.folder.access'].sudo()
        for user, level in ((self.reader, 'read'), (self.uploader, 'upload'),
                            (self.editor, 'edit'), (self.foldermgr, 'manage')):
            access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                           'user_id': user.id, 'level': level})
        access.create({'folder_id': self.folder.id, 'subject_type': 'partner',
                       'partner_id': self.portal_partner.id, 'level': 'read'})
        self.doc = self._document('note.txt', b'first content')
        env.flush_all()

    def _document(self, name, content, folder=None, owner=None):
        return self.env['woow.document'].sudo().create({
            'name': name, 'folder_id': (folder or self.folder).id,
            'owner_id': (owner or self.admin).id, 'datas': base64.b64encode(content)})

    def _upload(self, user, doc, content, filename='next.txt'):
        """What the form does: the wizard of ``user`` with one uploaded file, then its button."""
        wizard = self.env['woow.document.version.wizard'].with_user(user).create({
            'document_id': doc.id, 'datas': base64.b64encode(content), 'filename': filename})
        return wizard.action_confirm()

    def _content(self, doc):
        return base64.b64decode(doc.sudo().datas)

    def _versions(self, doc):
        return self.env['woow.document.version'].sudo().search(
            [('document_id', '=', doc.id)], order='number')

    def _events(self, event, doc):
        return self.env['woow.document.log'].sudo().search(
            [('event', '=', event), ('document_id', '=', doc.id)], order='id')

    def _server_version(self, doc, number, content, name=None):
        """A version row as the server paths make it (the superuser is not stopped by the guard)."""
        return self.env['woow.document.version'].sudo().create({
            'document_id': doc.id, 'number': number, 'name': name or doc.name,
            'mimetype': 'text/plain', 'datas': base64.b64encode(content),
            'file_size': len(content)})


@tagged('woow_docmgmt_core')
class TestNewVersion(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()

    def test_the_model_is_one_of_the_modules_and_has_no_public_surface(self):
        self.assertIn('woow.document.version', WOOW_MODELS)
        version = self.env['woow.document.version']
        self.assertTrue(version._fields['datas'].attachment)
        self.assertEqual(version._woow_own_default_fields, frozenset(
            name for name in version._fields if name not in
            ('id', 'display_name', 'create_uid', 'create_date', 'write_uid', 'write_date')
            and not name.startswith('__')))

    def test_an_upload_keeps_the_replaced_content_as_version_one(self):
        before_checksum = self.doc.checksum
        self._upload(self.editor, self.doc, b'second content')
        self.assertEqual(self._content(self.doc), b'second content')
        versions = self._versions(self.doc)
        self.assertEqual(versions.mapped('number'), [1])
        first = versions
        self.assertEqual(base64.b64decode(first.datas), b'first content')
        self.assertEqual((first.name, first.mimetype, first.checksum, first.file_size),
                         ('note.txt', 'text/plain', before_checksum, len(b'first content')))
        self.assertEqual(first.create_uid, self.editor)
        self.assertEqual(first.company_id, self.doc.company_id)
        self._upload(self.editor, self.doc, b'third content')
        self.assertEqual(self._versions(self.doc).mapped('number'), [1, 2])
        self.assertEqual(base64.b64decode(self._versions(self.doc)[1].datas), b'second content')
        self.assertEqual(self.doc.sudo().version_count, 2)
        events = self._events('version_add', self.doc)
        self.assertEqual([event.detail['number'] for event in events], [1, 2])
        self.assertEqual(events[0].detail['bytes'], len(b'second content'))
        self.assertEqual(events[0].user_id, self.editor)
        # the name of the log row holds ids and counts only
        self.assertTrue(all(type(value) is int for event in events
                            for value in event.detail.values()))

    def test_who_may_add_a_version(self):
        owned = self._document('owned.txt', b'mine', owner=self.uploader)
        allowed = ((self.editor, self.doc), (self.foldermgr, self.doc),
                   (self.docmgr, self.doc), (self.uploader, owned))
        for index, (user, doc) in enumerate(allowed):
            with self.subTest(user=user.login):
                self._upload(user, doc, b'by %d' % index)
                self.assertEqual(self._content(doc), b'by %d' % index)
        refused = ((self.reader, self.doc), (self.uploader, self.doc), (self.outsider, self.doc),
                   (self.plain, self.doc), (self.portal, self.doc))
        for user, doc in refused:
            with self.subTest(refused=user.login):
                before = (self._content(doc), len(self._versions(doc)))
                with self.assertRaises(AccessError):
                    self._upload(user, doc, b'not allowed')
                self.assertEqual((self._content(doc), len(self._versions(doc))), before)

    def test_the_button_that_opens_the_form_checks_the_same(self):
        action = self.doc.with_user(self.editor).action_new_version()
        self.assertEqual(action['res_model'], 'woow.document.version.wizard')
        self.assertEqual(action['context'], {'default_document_id': self.doc.id})
        for user in (self.reader, self.uploader, self.outsider, self.plain, self.portal):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.doc.with_user(user).action_new_version()

    def test_the_form_has_the_button_and_the_tab(self):
        views = self.env['woow.document'].with_user(self.editor).get_views([(False, 'form')])
        arch = views['views']['form']['arch']
        self.assertIn('name="action_new_version"', arch)
        self.assertIn('name="version_ids"', arch)
        self.assertIn('woow_user_can_write', arch)
        sub = views['models']['woow.document']['fields']['version_ids']
        self.assertEqual(sub['relation'], 'woow.document.version')
        listing = self.env['woow.document.version'].with_user(self.editor).get_views(
            [(self.env.ref('woow_document_management.view_woow_document_version_list').id,
              'list')])['views']['list']['arch']
        for action in ('action_download', 'action_restore', 'action_delete'):
            self.assertIn('name="%s"' % action, listing)
        wizard = self.env['woow.document.version.wizard'].with_user(self.editor).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertIn('name="action_confirm"', wizard)

    def test_no_version_for_a_link_a_trashed_file_or_a_file_that_another_user_locked(self):
        link = self.env['woow.document'].sudo().create({
            'name': 'link', 'folder_id': self.folder.id, 'document_type': 'url',
            'url': 'https://example.com/page', 'owner_id': self.admin.id})
        with self.assertRaises(UserError):
            self._upload(self.editor, link, b'content')
        trashed = self._document('trashed.txt', b'gone')
        trashed.with_user(self.editor).action_trash()
        with self.assertRaises(AccessError):
            self._upload(self.editor, trashed, b'late')
        model = type(self.env['woow.document'])
        with patch.object(model, '_woow_lock_holder', lambda doc: self.reader):
            with self.assertRaises(UserError):
                self._upload(self.editor, self.doc, b'locked out')
            self.assertEqual(self._content(self.doc), b'first content')
        with patch.object(model, '_woow_lock_holder', lambda doc: self.editor):
            self._upload(self.editor, self.doc, b'my own lock')   # the holder is not stopped
        self.assertEqual(self._content(self.doc), b'my own lock')

    def test_the_new_file_passes_the_upload_policy_and_nothing_changes_when_it_does_not(self):
        for name, content in (('run.exe', b'MZ binary'), ('bad.txt', b'text\x00with nul'),
                              ('..', b'text')):
            with self.subTest(name=name), self.assertRaises(WoowUploadRejected):
                self._upload(self.editor, self.doc, content, name)
        with self.assertRaises(WoowUploadRejected):
            self._upload(self.editor, self.doc, b'', 'empty.txt')
        self.assertEqual((self._content(self.doc), len(self._versions(self.doc))),
                         (b'first content', 0))
        self.assertFalse(self._events('version_add', self.doc))

    def test_the_new_file_passes_the_quota_and_a_restore_does_not_need_it(self):
        locks._reset_tracker(self.env.cr)
        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '1')
        with self.assertRaises(WoowUploadRejected) as caught:
            self._upload(self.editor, self.doc, b'a' * (1024 * 1024))
        self.assertEqual(caught.exception.code, 'quota')
        self.assertEqual((self._content(self.doc), len(self._versions(self.doc))),
                         (b'first content', 0))
        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '0')
        self._upload(self.editor, self.doc, b'second content')
        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '1')
        version = self._versions(self.doc)
        with patch.object(type(self.env['woow.document.quota']), '_usage_bytes',
                          return_value=1024 * 1024):
            version.with_user(self.editor).action_restore()     # full site: nothing is added
        self.assertEqual(self._content(self.doc), b'first content')

    def test_the_bytes_of_versions_count_towards_the_usage_once_per_content(self):
        quota = self.env['woow.document.quota']

        class Own:
            """The usage query reads committed data on a cursor of its own: read this transaction."""
            def __enter__(inner):
                return self.env.cr

            def __exit__(inner, *exc):
                return False

        with patch.object(self.env.registry, 'cursor', return_value=Own()):
            before = quota._usage_bytes()
            self._upload(self.editor, self.doc, b'second content!')
            after_upload = quota._usage_bytes()
            # the replaced content is still stored (as the version): only the new bytes are added
            self.assertEqual(after_upload - before, len(b'second content!'))
            self._versions(self.doc).with_user(self.editor).action_restore()
            # the same two contents are held again: nothing more is stored
            self.assertEqual(quota._usage_bytes(), after_upload)
            first, second = self._versions(self.doc)
            first.with_user(self.editor).action_delete()
            # the current content holds the same file: nothing is freed
            self.assertEqual(quota._usage_bytes(), after_upload)
            second.with_user(self.editor).action_delete()
            self.assertEqual(quota._usage_bytes(), after_upload - len(b'second content!'))

    def test_the_extension_of_the_name_follows_the_content_and_a_restore_brings_it_back(self):
        self._upload(self.editor, self.doc, b'a,b\n1,2\n', 'table.csv')
        self.assertEqual((self.doc.name, self.doc.mimetype), ('note.csv', 'text/csv'))
        version = self._versions(self.doc)
        self.assertEqual((version.name, version.mimetype), ('note.txt', 'text/plain'))
        version.with_user(self.editor).action_restore()
        self.assertEqual((self.doc.name, self.doc.mimetype), ('note.txt', 'text/plain'))
        self.assertEqual(self._content(self.doc), b'first content')
        # the same extension keeps the name, whatever the uploaded file is called
        self._upload(self.editor, self.doc, b'more', 'other-name.TXT')
        self.assertEqual(self.doc.name, 'note.txt')

    def test_only_the_newest_versions_are_kept_and_the_removal_is_logged(self):
        with patch(_CAP, 3):
            for index in range(5):
                self._upload(self.editor, self.doc, b'content %d' % index)
        versions = self._versions(self.doc)
        self.assertEqual(versions.mapped('number'), [3, 4, 5])
        self.assertEqual([base64.b64decode(v.datas) for v in versions],
                         [b'content 1', b'content 2', b'content 3'])
        self.assertEqual(self._content(self.doc), b'content 4')
        removed = self._events('version_delete', self.doc)
        self.assertEqual(sorted(event.detail['number'] for event in removed), [1, 2])
        # the files of the removed versions are gone with their rows
        rows = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document.version'), ('res_field', '=', 'datas'),
            ('res_id', 'in', versions.ids)])
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.env['woow.document.version'].sudo().search_count(
            [('document_id', '=', self.doc.id)]), 3)

    def test_the_real_limit_is_the_constant_of_the_release(self):
        self.assertEqual(MAX_VERSIONS_PER_DOCUMENT, 20)

    def test_a_document_under_legal_hold_keeps_every_version(self):
        with patch(_CAP, 2):
            self._upload(self.editor, self.doc, b'content 1')
            self._upload(self.editor, self.doc, b'content 2')
            self.doc.with_user(self.docmgr).action_set_legal_hold(True)
            before = (self._content(self.doc), self._versions(self.doc).ids)
            with self.assertRaises(UserError):
                self._upload(self.editor, self.doc, b'content 3')
            with self.assertRaises(UserError):
                self._upload(self.docmgr, self.doc, b'content 3')
            version = self._versions(self.doc)[0]
            with self.assertRaises(UserError):
                version.with_user(self.editor).action_restore()
            self.assertEqual((self._content(self.doc), self._versions(self.doc).ids), before)
            self.doc.with_user(self.docmgr).action_set_legal_hold(False)
            self._upload(self.editor, self.doc, b'content 3')
        self.assertEqual(self._content(self.doc), b'content 3')

    def test_under_legal_hold_a_version_fits_while_the_limit_is_not_reached(self):
        self.doc.with_user(self.docmgr).action_set_legal_hold(True)
        self._upload(self.editor, self.doc, b'content 1')
        self.assertEqual(self._versions(self.doc).mapped('number'), [1])

    def test_the_number_of_a_version_is_unique_for_its_document(self):
        self._server_version(self.doc, 1, b'one')
        with mute_logger('odoo.sql_db'), self.assertRaises(errors.UniqueViolation), \
                self.cr.savepoint():
            self._server_version(self.doc, 1, b'again')
        with mute_logger('odoo.sql_db'), self.assertRaises(errors.CheckViolation), \
                self.cr.savepoint():
            self._server_version(self.doc, 0, b'zero')


    def test_a_number_is_never_given_twice_after_the_newest_version_is_deleted(self):
        self._upload(self.editor, self.doc, b'content 1')
        self._upload(self.editor, self.doc, b'content 2')
        self.assertEqual(self._versions(self.doc).mapped('number'), [1, 2])
        self._versions(self.doc)[-1].with_user(self.editor).action_delete()
        self._upload(self.editor, self.doc, b'content 3')
        self.assertEqual(self._versions(self.doc).mapped('number'), [1, 3])
        self.assertEqual(sorted(event.detail['number']
                                for event in self._events('version_add', self.doc)), [1, 2, 3])
        self.assertEqual(self.doc.sudo().version_last_number, 3)

    def test_the_counter_of_numbers_is_set_by_the_server_only(self):
        for user in (self.editor, self.docmgr):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.doc.with_user(user).write({'version_last_number': 0})
            with self.subTest(user=user.login, create=True), self.assertRaises(AccessError):
                self.env['woow.document'].with_user(user).create({
                    'name': 'counter.txt', 'folder_id': self.folder.id,
                    'datas': base64.b64encode(b'x'), 'version_last_number': 7})
        with self.assertRaises(AccessError):
            self.doc.with_user(self.portal).read(['version_last_number'])

    def test_a_non_administrator_is_not_told_about_a_legal_hold(self):
        with patch(_CAP, 1):
            self._upload(self.editor, self.doc, b'content 1')
            self.doc.with_user(self.docmgr).action_set_legal_hold(True)
            hold = 'legal hold'
            with self.assertRaises(UserError) as editor:
                self._upload(self.editor, self.doc, b'content 2')
            with self.assertRaises(UserError) as manager:
                self._upload(self.docmgr, self.doc, b'content 2')
        self.assertNotIn(hold, str(editor.exception).lower())
        self.assertIn(hold, str(manager.exception).lower())
        # the editor cannot read the hold, and the message does not give it away either
        with self.assertRaises(AccessError):
            self.doc.with_user(self.editor).read(['legal_hold'])

    def test_the_new_file_is_judged_when_it_arrives_in_the_wizard(self):
        """Nothing is parked in the table of the wizard: a file that the policy refuses, or
        a document the caller may not change (hidden, missing or read-only), is refused at once."""
        wizard = self.env['woow.document.version.wizard']
        for name, content in (('run.exe', b'MZ binary'), ('bad.txt', b'text\x00with nul')):
            with self.subTest(name=name), self.assertRaises(WoowUploadRejected):
                wizard.with_user(self.editor).create({
                    'document_id': self.doc.id, 'datas': base64.b64encode(content),
                    'filename': name})
        with self.assertRaises(WoowUploadRejected):
            wizard.with_user(self.editor).create({
                'document_id': self.doc.id, 'datas': 'not base64 !', 'filename': 'a.txt'})
        row = wizard.with_user(self.editor).create({
            'document_id': self.doc.id, 'datas': base64.b64encode(b'fine'), 'filename': 'a.txt'})
        with self.assertRaises(WoowUploadRejected):
            row.write({'datas': base64.b64encode(b'MZ binary'), 'filename': 'run.exe'})
        row.write({'filename': 'b.txt'})
        self.assertEqual(wizard.sudo().search_count([]), 1)

    def test_a_hidden_a_missing_and_a_read_only_document_get_the_same_answer_in_the_wizard(self):
        other_folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Versions hidden folder', 'owner_id': self.admin.id})
        hidden = self._document('hidden.txt', b'hidden', folder=other_folder)
        self.env.flush_all()
        missing = self.env['woow.document'].sudo().search([], order='id desc', limit=1).id + 1000
        answers = set()
        for user, document_id in ((self.editor, hidden.id), (self.editor, missing),
                                  (self.reader, self.doc.id), (self.uploader, self.doc.id),
                                  (self.reader, hidden.id), (self.reader, missing)):
            with self.subTest(user=user.login, document=document_id):
                with self.assertRaises(AccessError) as caught:
                    self.env['woow.document.version.wizard'].with_user(user).create({
                        'document_id': document_id, 'datas': base64.b64encode(b'x'),
                        'filename': 'x.txt'})
                answers.add(str(caught.exception))
        self.assertEqual(len(answers), 1)
        self.assertFalse(self.env['woow.document.version.wizard'].sudo().search_count([]))
        row = self.env['woow.document.version.wizard'].with_user(self.editor).create({
            'document_id': self.doc.id, 'datas': base64.b64encode(b'fine'), 'filename': 'a.txt'})
        with self.assertRaises(AccessError):
            row.write({'document_id': hidden.id})
        with self.assertRaises(AccessError):
            row.write({'document_id': missing})

    def test_a_file_that_has_content_is_replaced_through_the_button_not_the_field(self):
        arch = self.env['woow.document'].with_user(self.editor).get_views(
            [(False, 'form')])['views']['form']['arch']
        field = arch[arch.index('name="datas"') - 20:arch.index('name="datas"') + 120]
        self.assertIn('id and file_size', field)
        self.assertIn('name="file_size"', arch)


@tagged('woow_docmgmt_core')
class TestRestoreAndDelete(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()
        self._upload(self.editor, self.doc, b'second content')
        self.first = self._versions(self.doc)

    def test_restore_is_a_round_trip_of_content_and_checksum(self):
        original = self.first.checksum
        self.first.with_user(self.editor).action_restore()
        self.assertEqual(self._content(self.doc), b'first content')
        self.assertEqual(self.doc.sudo().checksum, original)
        versions = self._versions(self.doc)
        # the chosen version stays, and the content it replaced is the newest one
        self.assertEqual(versions.mapped('number'), [1, 2])
        self.assertEqual(base64.b64decode(versions[1].datas), b'second content')
        self.assertEqual(self._events('version_restore', self.doc).mapped('detail')[0]['restored'], 1)
        versions[1].with_user(self.editor).action_restore()
        self.assertEqual(self._content(self.doc), b'second content')

    def test_who_may_restore_and_delete(self):
        for user in (self.reader, self.uploader, self.outsider, self.plain, self.portal):
            for action in ('action_restore', 'action_delete'):
                with self.subTest(user=user.login, action=action), self.assertRaises(AccessError):
                    getattr(self.first.with_user(user), action)()
        self.assertEqual((self._content(self.doc), self._versions(self.doc).ids),
                         (b'second content', self.first.ids))
        for user in (self.editor, self.foldermgr, self.docmgr):
            with self.subTest(user=user.login):
                self._upload(user, self.doc, b'by ' + user.login.encode())
                newest = self._versions(self.doc)[-1]
                newest.with_user(user).action_restore()
                newest.exists().with_user(user).action_delete()
                self.assertFalse(newest.exists())

    def test_a_delete_removes_the_row_and_the_file_and_is_logged(self):
        rows = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document.version'), ('res_id', '=', self.first.id),
            ('res_field', '=', 'datas')])
        self.assertEqual(len(rows), 1)
        self.first.with_user(self.editor).action_delete()
        self.assertFalse(self.first.exists())
        self.assertFalse(rows.exists())
        self.assertEqual(self._events('version_delete', self.doc).mapped('detail'),
                         [{'number': 1}])

    def test_a_version_of_a_document_under_legal_hold_is_not_deleted(self):
        self.doc.with_user(self.docmgr).action_set_legal_hold(True)
        for user in (self.editor, self.docmgr):
            with self.subTest(user=user.login), self.assertRaises(UserError):
                self.first.with_user(user).action_delete()
        self.assertTrue(self.first.exists())
        self.doc.with_user(self.docmgr).action_set_legal_hold(False)
        self.first.with_user(self.docmgr).action_delete()
        self.assertFalse(self.first.exists())

    def test_a_restore_is_refused_for_a_trashed_file_or_a_file_locked_by_another_user(self):
        model = type(self.env['woow.document'])
        with patch.object(model, '_woow_lock_holder', lambda doc: self.reader):
            with self.assertRaises(UserError):
                self.first.with_user(self.editor).action_restore()
        self.doc.with_user(self.editor).action_trash()
        with self.assertRaises(AccessError):
            self.first.with_user(self.editor).action_restore()
        self.assertEqual(self._content(self.doc), b'second content')

    def test_a_restore_passes_the_upload_policy_again(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.internal_max_upload_mb', '1')
        big = self._server_version(self.doc, 7, b'x' * (1024 * 1024 + 1))
        with self.assertRaises(WoowUploadRejected):
            big.with_user(self.editor).action_restore()
        self.assertEqual(self._content(self.doc), b'second content')

    def test_the_actions_are_bounded_and_the_download_action_names_its_route(self):
        many = self.first | self._server_version(self.doc, 5, b'another')
        with patch(_CAP, 1), self.assertRaises(UserError):
            many.with_user(self.docmgr).action_delete()
        self.assertEqual(len(many.exists()), 2)
        self.assertEqual(
            self.first.with_user(self.reader).action_download()['url'],
            '/woow_document/version/%d/download' % self.first.id)
        with self.assertRaises(AccessError):
            self.first.with_user(self.plain).action_download()


    def test_a_restore_at_the_limit_keeps_the_version_that_was_chosen(self):
        self._upload(self.editor, self.doc, b'third content')
        self._upload(self.editor, self.doc, b'fourth content')
        self.assertEqual(self._versions(self.doc).mapped('number'), [1, 2, 3])
        with patch(_CAP, 3):
            self.first.with_user(self.editor).action_restore()
        versions = self._versions(self.doc)
        # the oldest one that was not chosen is dropped, the chosen row stays in the list
        self.assertEqual(versions.mapped('number'), [1, 3, 4])
        self.assertEqual(self._content(self.doc), b'first content')
        self.assertEqual(base64.b64decode(versions[0].datas), b'first content')
        self.assertEqual(base64.b64decode(versions[-1].datas), b'fourth content')
        self.assertEqual([event.detail['number']
                          for event in self._events('version_delete', self.doc)], [2])

    def test_a_non_administrator_is_not_told_about_a_legal_hold_on_delete(self):
        self.doc.with_user(self.docmgr).action_set_legal_hold(True)
        with self.assertRaises(UserError) as editor:
            self.first.with_user(self.editor).action_delete()
        with self.assertRaises(UserError) as manager:
            self.first.with_user(self.docmgr).action_delete()
        self.assertNotIn('legal hold', str(editor.exception).lower())
        self.assertIn('legal hold', str(manager.exception).lower())
        self.assertTrue(self.first.exists())

    def test_the_history_of_a_trashed_file_is_not_deleted(self):
        self.doc.with_user(self.editor).action_trash()
        for user in (self.editor, self.docmgr):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.first.with_user(user).action_delete()
        self.assertTrue(self.first.exists())
        self.doc.with_user(self.docmgr).action_restore()
        self.first.with_user(self.editor).action_delete()
        self.assertFalse(self.first.exists())

    def test_the_history_of_a_file_locked_by_another_user_is_not_deleted(self):
        model = type(self.env['woow.document'])
        with patch.object(model, '_woow_lock_holder', lambda doc: self.reader):
            with self.assertRaises(UserError):
                self.first.with_user(self.editor).action_delete()
            self.assertTrue(self.first.exists())
        with patch.object(model, '_woow_lock_holder', lambda doc: self.editor):
            self.first.with_user(self.editor).action_delete()   # the holder is not stopped
        self.assertFalse(self.first.exists())


@tagged('woow_docmgmt_core')
class TestServerOnlyRows(_Fixture, TransactionCase):
    """A direct create, write or unlink over RPC is refused, whoever calls it."""

    def setUp(self):
        super().setUp()
        self._fixture()
        self._upload(self.editor, self.doc, b'second content')
        self.version = self._versions(self.doc)
        self.values = {'document_id': self.doc.id, 'number': 9, 'name': 'forged.txt',
                       'datas': base64.b64encode(b'forged')}

    def test_create_write_and_unlink_are_refused_even_with_the_context_keys(self):
        # a context sent by a client can carry text and numbers, never the server's own object
        contexts = ({}, {'woow_version_swap': True}, {'woow_version_swap': 1},
                    {'woow_version_swap': 'woow_version_swap'})
        for user in (self.editor, self.foldermgr, self.docmgr, self.admin):
            for context in contexts:
                model = self.env['woow.document.version'].with_user(user).with_context(**context)
                with self.subTest(user=user.login, context=context):
                    with self.assertRaises(AccessError):
                        model.create(dict(self.values))
                    with self.assertRaises(AccessError):
                        model.create([dict(self.values), dict(self.values, number=10)])
                    with self.assertRaises(AccessError):
                        self.version.with_user(user).with_context(**context).write(
                            {'name': 'renamed.txt'})
                    with self.assertRaises(AccessError):
                        self.version.with_user(user).with_context(**context).unlink()
        self.assertEqual((self.version.name, self.version.exists().ids),
                         ('note.txt', self._versions(self.doc).ids))
        self.assertEqual(self.env['woow.document.version'].sudo().search_count([]), 1)

    def test_the_document_cannot_write_its_version_list(self):
        for user in (self.editor, self.docmgr):
            for command in ([Command.create(dict(self.values))], [Command.delete(self.version.id)],
                            [Command.update(self.version.id, {'name': 'x.txt'})],
                            [Command.clear()], [Command.set([])]):
                with self.subTest(user=user.login, command=command), self.assertRaises(AccessError):
                    self.doc.with_user(user).write({'version_ids': command})
            with self.subTest(user=user.login, field='version_count'), \
                    self.assertRaises(AccessError):
                self.doc.with_user(user).write({'version_count': 5})
            with self.subTest(user=user.login, create=True), self.assertRaises(AccessError):
                self.env['woow.document'].with_user(user).create({
                    'name': 'with-versions.txt', 'folder_id': self.folder.id,
                    'datas': base64.b64encode(b'x'),
                    'version_ids': [Command.create(dict(self.values))]})
        self.assertEqual(self._versions(self.doc).ids, self.version.ids)

    def test_a_stored_default_does_not_reach_a_version(self):
        self.env['ir.default'].with_user(self.docmgr).set(
            'woow.document.version', 'checksum', 'poison', user_id=True)
        self.env['ir.default'].with_user(self.docmgr).set(
            'woow.document.version', 'mimetype', 'poison/type', user_id=True)
        # a call made with sudo() keeps the uid of its caller, and so his stored defaults
        row = self.env['woow.document.version'].with_user(self.docmgr).sudo().with_context(
            default_file_size=77).create({
                'document_id': self.doc.id, 'number': 5, 'name': 'plain.txt',
                'datas': base64.b64encode(b'plain')})
        self.assertFalse(row.checksum or row.mimetype or row.file_size)

    def test_copy_is_not_offered(self):
        with self.assertRaises(UserError):
            self.version.with_user(self.docmgr).copy()


@tagged('woow_docmgmt_core')
class TestVersionRecordRules(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()
        env = self.env
        folders = env['woow.document.folder'].sudo()
        self.other_folder = folders.create({'name': 'Other versions folder',
                                            'owner_id': self.admin.id})
        env['woow.document.folder.access'].sudo().create({
            'folder_id': self.other_folder.id, 'subject_type': 'user',
            'user_id': self.outsider.id, 'level': 'edit'})
        self.trash_folder = folders.create({'name': 'Folder to trash', 'owner_id': self.admin.id})
        env['woow.document.folder.access'].sudo().create({
            'folder_id': self.trash_folder.id, 'subject_type': 'user',
            'user_id': self.editor.id, 'level': 'edit'})
        self.docs = {
            'open': self.doc,
            'owned': self._document('owned.txt', b'owned', owner=self.uploader),
            'other': self._document('other.txt', b'other', folder=self.other_folder),
            'shared': self._document('shared.txt', b'shared', folder=self.other_folder),
            'archived': self._document('archived.txt', b'archived'),
            'in trashed folder': self._document('infolder.txt', b'infolder',
                                                folder=self.trash_folder),
        }
        self.docs['shared'].sudo().write({'shared_user_ids': [Command.set([self.reader.id])]})
        self.docs['archived'].sudo().write({'active': False})
        self.trash_folder.sudo().action_trash()
        for index, doc in enumerate(self.docs.values()):
            self._server_version(doc, 1, b'old %d' % index)
            self._server_version(doc, 2, b'older %d' % index)
        env.flush_all()
        self.users = {'reader': self.reader, 'uploader': self.uploader, 'editor': self.editor,
                      'foldermgr': self.foldermgr, 'docmgr': self.docmgr,
                      'outsider': self.outsider}

    def test_the_visible_versions_are_exactly_those_of_the_visible_documents(self):
        everything = self.env['woow.document.version'].sudo().search([])
        self.assertEqual(len(everything), 2 * len(self.docs))
        keys = {doc.id: key for key, doc in self.docs.items()}
        seen = {}
        for name, user in self.users.items():
            with self.subTest(user=name):
                documents = self.env['woow.document'].with_user(user).with_context(
                    active_test=False).search([])
                expected = everything.filtered(lambda row: row.document_id in documents)
                visible = self.env['woow.document.version'].with_user(user).search([])
                self.assertEqual(set(visible.ids), set(expected.ids))
                seen[name] = {keys[row.document_id.id] for row in visible}
        # the answers are not all alike: the rule really separates people
        self.assertEqual(seen['docmgr'], set(self.docs))
        self.assertEqual(seen['reader'], {'open', 'owned', 'shared'})
        self.assertEqual(seen['editor'], {'open', 'owned', 'archived'})
        self.assertEqual(seen['foldermgr'], {'open', 'owned', 'archived'})
        self.assertEqual(seen['outsider'], {'other', 'shared'})
        self.assertIn('owned', seen['uploader'])
        self.assertFalse({'other', 'shared', 'in trashed folder'} & seen['uploader'])

    def test_an_archived_document_keeps_its_versions_for_those_who_may_still_read_it(self):
        archived = self.docs['archived']
        versions = self.env['woow.document.version'].sudo().search([('document_id', '=', archived.id)])
        self.assertEqual(set(self.env['woow.document.version'].with_user(self.editor).search(
            [('document_id', '=', archived.id)]).ids), set(versions.ids))
        self.assertFalse(self.env['woow.document.version'].with_user(self.reader).search_count(
            [('document_id', '=', archived.id)]))

    def test_nothing_for_portal_public_and_internal_users_without_the_group(self):
        version = self.env['woow.document.version']
        for user in (self.portal, self.plain, self.env.ref('base.public_user')):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    version.with_user(user).search([])
                with self.assertRaises(AccessError):
                    version.with_user(user).check_access('read')
                with self.assertRaises(AccessError):
                    version.with_user(user).browse(1).read(['name'])

    def test_a_portal_user_who_reads_the_document_cannot_read_its_version_list(self):
        document = self.env['woow.document'].with_user(self.portal)
        self.assertEqual(document.search_count([('id', '=', self.doc.id)]), 1)
        with self.assertRaises(AccessError):
            document.browse(self.doc.id).read(['version_ids'])
        with self.assertRaises(AccessError):
            document.browse(self.doc.id).read(['version_count'])
        # the fields that a portal contact reads in its pages are unchanged
        self.assertEqual(document.browse(self.doc.id).read(['name'])[0]['name'], 'note.txt')

    def test_versions_follow_the_company_of_their_document(self):
        company_b = self.env['res.company'].sudo().create({'name': 'Versions company B'})
        admin_b = new_test_user(
            self.env, login='wver-admin-b', groups=_MANAGER, company_id=company_b.id,
            company_ids=[Command.set([company_b.id])])
        folder_b = self.env['woow.document.folder'].sudo().create({
            'name': 'Company B folder', 'owner_id': admin_b.id, 'company_id': company_b.id})
        doc_b = self._document('b.txt', b'company b', folder=folder_b, owner=admin_b)
        row = self._server_version(doc_b, 1, b'b old')
        self.assertEqual(row.company_id, company_b)
        self.assertFalse(self.env['woow.document.version'].with_user(self.docmgr).search_count(
            [('id', '=', row.id)]))
        self.assertEqual(self.env['woow.document.version'].with_user(admin_b).search_count(
            [('id', '=', row.id)]), 1)
        with self.assertRaises(AccessError):
            row.with_user(self.docmgr).action_download()


@tagged('woow_docmgmt_core')
class TestVersionFiles(_Fixture, TransactionCase):
    """The files of versions are never public, never tokenised and go with their document."""

    def setUp(self):
        super().setUp()
        self._fixture()
        self._upload(self.editor, self.doc, b'second content')
        self.version = self._versions(self.doc)

    def _rows(self):
        return self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document.version'), ('res_id', 'in', self.version.ids),
            ('res_field', '=', 'datas')])

    def test_the_file_of_a_version_is_closed(self):
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows.public)
        self.assertFalse(rows.access_token)
        self.assertEqual(rows.generate_access_token(), [False])
        with self.assertRaises(AccessError):
            rows.validate_access('any-token')
        with self.assertRaises(AccessError):
            rows.with_user(self.editor).write({'public': True})
        with self.assertRaises(AccessError):
            rows.with_user(self.editor).write({'res_model': 'res.partner'})

    def test_a_purge_removes_the_versions_and_their_files(self):
        self._upload(self.editor, self.doc, b'third content')
        versions = self._versions(self.doc)
        row_ids = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document.version'), ('res_id', 'in', versions.ids),
            ('res_field', '=', 'datas')]).ids
        self.assertEqual(len(row_ids), 2)
        self.doc.with_user(self.docmgr).action_trash()
        self.doc.with_user(self.docmgr).unlink()
        self.assertFalse(versions.exists())
        self.assertFalse(self.env['ir.attachment'].sudo().browse(row_ids).exists())
        self.assertFalse(self.env['ir.attachment'].sudo().search_count([
            ('res_model', '=', 'woow.document.version'), ('res_id', 'in', versions.ids),
            ('res_field', '=', 'datas')]))

    def test_a_purge_of_a_folder_takes_the_versions_of_its_documents(self):
        versions = self._versions(self.doc)
        folder = self.folder
        folder.with_user(self.docmgr).action_trash()
        done, failed = folder.with_user(self.docmgr)._woow_purge()
        self.assertEqual((done, failed), (1, 0))
        self.assertFalse(versions.exists())
        self.assertFalse(self._rows().exists())

    def test_a_purge_that_is_refused_keeps_the_versions(self):
        self.doc.with_user(self.docmgr).action_set_legal_hold(True)
        self.doc.with_user(self.docmgr).action_trash()
        with self.assertRaises(UserError):
            self.doc.with_user(self.docmgr).unlink()
        self.assertEqual(self._versions(self.doc), self.version)
        self.assertEqual(len(self._rows()), 1)
        self.doc.with_user(self.docmgr).action_set_legal_hold(False)
        with self.assertRaises(AccessError):
            self.doc.with_user(self.editor).unlink()
        self.assertEqual(self._versions(self.doc), self.version)
        # a document that is still active is not purged either, and its versions stay
        other = self._document('active.txt', b'active content')
        self._server_version(other, 1, b'older')
        with self.assertRaises(UserError):
            other.with_user(self.docmgr).unlink()
        self.assertEqual(len(self._versions(other)), 1)

    def test_the_uninstall_removes_the_versions_and_their_files(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_document_management.uninstall_confirm',
                         params.get_param('database.uuid'))
        row_ids = self._rows().ids
        self.assertEqual(len(row_ids), 1)
        uninstall_hook(self.env)
        self.assertFalse(self.version.exists())
        self.assertFalse(self.env['ir.attachment'].sudo().browse(row_ids).exists())

    def test_the_wizard_row_of_a_user_is_not_visible_to_another_user(self):
        wizard = self.env['woow.document.version.wizard'].with_user(self.editor).create({
            'document_id': self.doc.id, 'datas': base64.b64encode(b'pending'),
            'filename': 'pending.txt'})
        with self.assertRaises(AccessError):
            wizard.with_user(self.foldermgr).read(['datas'])
        with self.assertRaises(AccessError):
            wizard.with_user(self.foldermgr).action_confirm()
        self.assertEqual(self._content(self.doc), b'second content')

    def test_the_wizard_keeps_nothing_of_the_file_after_it_ran(self):
        wizard = self.env['woow.document.version.wizard'].with_user(self.editor).create({
            'document_id': self.doc.id, 'datas': base64.b64encode(b'third content'),
            'filename': 'third.txt'})
        wizard.action_confirm()
        self.assertFalse(wizard.sudo().datas)
        self.assertFalse(self.env['ir.attachment'].sudo().search_count([
            ('res_model', '=', 'woow.document.version.wizard')]))


@tagged('woow_docmgmt_core')
class TestVersionRaces(TransactionCase):
    """Real Repeatable Read transactions of their own (no registry test mode).

    The fixture is committed first: a folder owned by the administrator and one document with
    content. Every race is the same shape: the call under test takes its snapshot, another
    transaction changes the document, and the call must not act on what its snapshot says.
    """

    def setUp(self):
        super().setUp()
        self.admin_id = self.env.ref('base.user_admin').id
        self._documents, self._folders = [], []
        self.addCleanup(self._drop_committed)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {
                'tracking_disable': True, 'mail_create_nolog': True,
                'mail_create_nosubscribe': True})
            folder = env['woow.document.folder'].create({
                'name': 'Version race %s' % secrets.token_hex(6), 'owner_id': self.admin_id})
            document = env['woow.document'].create({
                'name': 'race.txt', 'folder_id': folder.id,
                'datas': base64.b64encode(b'race content')})
            self._folders.append(folder.id)
            self._documents.append(document.id)
            self.document_id = document.id

    def _drop_committed(self):
        with self.registry.cursor() as cr:
            documents, folders = self._documents, self._folders
            cr.execute("DELETE FROM ir_attachment WHERE res_model = 'woow.document.version' "
                       "AND res_id IN (SELECT id FROM woow_document_version "
                       "WHERE document_id = ANY(%s))", (documents,))
            cr.execute('DELETE FROM woow_document_version WHERE document_id = ANY(%s)',
                       (documents,))
            cr.execute('DELETE FROM woow_document_log WHERE document_id = ANY(%s) '
                       'OR folder_id = ANY(%s)', (documents, folders))
            cr.execute("DELETE FROM mail_followers WHERE (res_model = 'woow.document' "
                       "AND res_id = ANY(%s)) OR (res_model = 'woow.document.folder' "
                       "AND res_id = ANY(%s))", (documents, folders))
            cr.execute("DELETE FROM mail_message WHERE (model = 'woow.document' "
                       "AND res_id = ANY(%s)) OR (model = 'woow.document.folder' "
                       "AND res_id = ANY(%s))", (documents, folders))
            cr.execute('DELETE FROM woow_document WHERE id = ANY(%s)', (documents,))
            cr.execute("DELETE FROM ir_attachment WHERE res_model = 'woow.document' "
                       "AND res_id = ANY(%s)", (documents,))
            cr.execute('DELETE FROM woow_document_folder WHERE id = ANY(%s)', (folders,))

    def _env(self, cr):
        return api.Environment(cr, self.admin_id, {})

    def _upload(self, env, content):
        wizard = env['woow.document.version.wizard'].create({
            'document_id': self.document_id, 'datas': base64.b64encode(content),
            'filename': 'race.txt'})
        wizard.action_confirm()

    def _committed(self):
        with self.registry.cursor() as cr:
            cr.execute('SELECT number FROM woow_document_version WHERE document_id = %s '
                       'ORDER BY number', (self.document_id,))
            numbers = [row[0] for row in cr.fetchall()]
            cr.execute('SELECT legal_hold IS TRUE FROM woow_document WHERE id = %s',
                       (self.document_id,))
            return numbers, cr.fetchone()[0]

    def _wait_for_a_blocked_statement(self):
        with self.registry.cursor() as cr:
            for _attempt in range(100):
                cr.execute("SELECT count(*) FROM pg_stat_activity WHERE "
                           "datname = current_database() AND wait_event_type = 'Lock'")
                if cr.fetchone()[0]:
                    return True
                time.sleep(0.1)

        return False

    def test_two_uploads_at_once_take_turns_and_the_second_is_run_again(self):
        """The second upload waits for the first and fails with a serialization error (which the
        server runs again); it never fails on the unique number, and nothing is half done."""
        outcome = {}
        with self.registry.cursor() as cr_first, self.registry.cursor() as cr_second:
            self._upload(self._env(cr_first), b'first racer')
            # made here: a thread cannot take the registry lock while the tests run at load time
            env_second = self._env(cr_second)

            def second():
                try:
                    with mute_logger('odoo.sql_db'):
                        self._upload(env_second, b'second racer')
                    outcome['error'] = None
                except Exception as exc:    # noqa: BLE001 - the type is what the test judges
                    outcome['error'] = exc
                finally:
                    cr_second.rollback()

            thread = threading.Thread(target=second)
            thread.start()
            try:
                self.assertTrue(self._wait_for_a_blocked_statement(),
                                'the second upload did not wait for the first: %r' % outcome)
            finally:
                cr_first.commit()
                thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome['error'], errors.SerializationFailure)
        self.assertEqual(self._committed()[0], [1])
        # run again on a fresh snapshot, it takes the next number
        with self.registry.cursor() as cr:
            self._upload(self._env(cr), b'second racer')
        self.assertEqual(self._committed()[0], [1, 2])

    def test_a_legal_hold_committed_after_the_snapshot_stops_the_delete_of_a_version(self):
        with self.registry.cursor() as cr:
            self._upload(self._env(cr), b'next content')
        with self.registry.cursor() as cr_delete:
            env = self._env(cr_delete)
            version = env['woow.document.version'].search(
                [('document_id', '=', self.document_id)])    # the snapshot of this call
            with self.registry.cursor() as cr_hold:
                self._env(cr_hold)['woow.document'].browse(
                    self.document_id).action_set_legal_hold(True)
            with mute_logger('odoo.sql_db'), self.assertRaises(errors.SerializationFailure):
                version.action_delete()
        numbers, hold = self._committed()
        self.assertEqual((numbers, hold), ([1], True))

    def test_a_delete_started_after_the_hold_is_refused_as_usual(self):
        with self.registry.cursor() as cr:
            self._upload(self._env(cr), b'next content')
        with self.registry.cursor() as cr:
            self._env(cr)['woow.document'].browse(self.document_id).action_set_legal_hold(True)
        with self.registry.cursor() as cr:
            version = self._env(cr)['woow.document.version'].search(
                [('document_id', '=', self.document_id)])
            with self.assertRaises(UserError):
                version.action_delete()
        self.assertEqual(self._committed(), ([1], True))


@tagged('woow_docmgmt_http')
class TestVersionRoute(_Fixture, HttpCase):

    def setUp(self):
        super().setUp()
        self._fixture('http')
        self.password = secrets.token_urlsafe(16)
        for user in (self.reader, self.editor, self.outsider, self.portal, self.plain):
            user.sudo().password = self.password
        self._upload(self.editor, self.doc, b'second content')
        self.version = self._versions(self.doc)
        self.url = '/woow_document/version/%d/download' % self.version.id

    def _missing(self):
        return self.url_open('/woow_document/version/%d/download' % (self.version.id + 1000000))

    def test_a_reader_downloads_an_earlier_version_with_the_headers_of_a_download(self):
        self.authenticate(self.reader.login, self.password)
        response = self.url_open(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'first content')
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertIn('note.txt', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store, private')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', response.headers['Content-Security-Policy'])
        events = self.env['woow.document.log'].sudo().search(
            [('event', '=', 'download'), ('document_id', '=', self.doc.id)])
        self.assertEqual(events.mapped('detail'), [{'number': 1}])
        self.assertEqual(events.user_id, self.reader)

    def test_a_forbidden_a_hidden_and_a_missing_version_get_the_same_answer(self):
        self.authenticate(self.outsider.login, self.password)
        missing = self._missing()
        self.assertEqual(missing.status_code, 404)
        for who in ('outsider', 'portal', 'plain'):
            with self.subTest(user=who):
                self.authenticate(getattr(self, who).login, self.password)
                refused = self.url_open(self.url)
                self.assertEqual(refused.status_code, 404)
                self.assertEqual(refused.content, missing.content)
        # a trashed document and a link answer alike
        self.authenticate(self.editor.login, self.password)
        self.doc.with_user(self.editor).action_trash()
        hidden = self.url_open(self.url)
        self.assertEqual((hidden.status_code, hidden.content), (404, missing.content))
        # a number outside the range of the database column
        huge = self.url_open('/woow_document/version/99999999999999999999/download')
        self.assertEqual((huge.status_code, huge.content), (404, missing.content))

    def test_no_token_opens_a_version(self):
        rows = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document.version'), ('res_id', '=', self.version.id),
            ('res_field', '=', 'datas')])
        self.assertEqual(rows.generate_access_token(), [False])
        self.authenticate(self.outsider.login, self.password)
        missing = self._missing()
        for url in (self.url + '?access_token=anything', '/web/content/%d?access_token=anything'
                    % rows.id, '/web/content/woow.document.version/%d/datas?access_token=x'
                    % self.version.id):
            with self.subTest(url=url):
                response = self.url_open(url)
                self.assertNotEqual(response.status_code, 200)
                self.assertNotIn(b'first content', response.content)
        self.assertEqual(self.url_open(self.url).content, missing.content)
        # not signed in: the page asks for a login and shows nothing
        self.authenticate(None, None)
        anonymous = self.url_open(self.url + '?access_token=anything')
        self.assertNotIn(b'first content', anonymous.content)

    def test_the_core_content_route_serves_a_version_under_the_headers_of_the_module(self):
        self.authenticate(self.editor.login, self.password)
        route = '/web/content/woow.document.version/%d/datas' % self.version.id
        response = self.url_open(route + '?mimetype=text/html&download=false')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'first content')
        self.assertNotIn('text/html', response.headers['Content-Type'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', response.headers['Content-Security-Policy'])
        self.authenticate(self.outsider.login, self.password)
        self.assertNotEqual(self.url_open(route).status_code, 200)
        rows = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document.version'), ('res_id', '=', self.version.id),
            ('res_field', '=', 'datas')])
        self.assertEqual(len(rows), 1)
        self.authenticate(self.editor.login, self.password)
        self.assertNotEqual(self.url_open('/web/content/%d' % rows.id).status_code, 200)

    def test_a_zip_holds_the_current_content_only(self):
        self.authenticate(self.editor.login, self.password)
        archive = self.url_open('/woow_document/zip?document_ids=%d' % self.doc.id)
        self.assertEqual(archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            self.assertEqual(len(zipped.namelist()), 1)
            self.assertEqual(zipped.read(zipped.namelist()[0]), b'second content')
        folder_zip = self.url_open('/woow_document/zip?folder_ids=%d' % self.folder.id)
        self.assertEqual(folder_zip.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(folder_zip.content)) as zipped:
            self.assertNotIn(b'first content', b''.join(zipped.read(n) for n in zipped.namelist()))
