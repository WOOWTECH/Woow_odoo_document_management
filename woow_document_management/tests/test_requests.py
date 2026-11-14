# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Requested files: who may ask, who may answer, and what a placeholder is not."""
import base64
import io
import secrets
import zipfile
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.requests import REQUEST_CTX_KEY
from ..models.upload_policy import WoowUploadRejected
from ..services import locks
from ..services.constants import REQUEST_REMINDER_MIN_HOURS

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_UPLOAD_TYPE = 'mail.mail_activity_data_upload_document'
_TEXT = b'a requested file'


class RequestsCase(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = env.ref('base.user_admin')
        env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.notice_base_url', 'https://docs.example.test')
        self.requester = new_test_user(env, login='req_requester', groups=_USER,
                                       email='requester@example.test')
        self.asked = new_test_user(env, login='req_asked', groups=_USER,
                                   email='asked@example.test')
        self.editor = new_test_user(env, login='req_editor', groups=_USER)
        self.reader = new_test_user(env, login='req_reader', groups=_USER)
        self.stranger = new_test_user(env, login='req_stranger', groups=_USER)
        self.plain = new_test_user(env, login='req_plain', groups='base.group_user')
        self.tag = env['woow.document.tag'].sudo().create({'name': 'Requested tag'})
        self.folder = self._folder('Requests folder', {
            self.requester: 'upload', self.asked: 'upload', self.editor: 'edit',
            self.reader: 'read'})
        self.model = env['woow.document']
        # the folders above took locks of a higher rank than the quota lock in this transaction
        locks._reset_tracker(env.cr)

    def _folder(self, name, grants):
        folder = self.env['woow.document.folder'].sudo().create({
            'name': name, 'owner_id': self.admin.id})
        for user, level in grants.items():
            self.env['woow.document.folder.access'].sudo().create({
                'folder_id': folder.id, 'subject_type': 'user', 'user_id': user.id,
                'level': level})
        self.env.flush_all()
        return folder

    def _ask(self, user=None, asked=None, folder=None, name='Signed contract', days=7, **kw):
        user = user or self.requester
        return self.model.with_user(user)._woow_open_request(
            folder or self.folder, name, asked or self.asked, days, **kw)

    def _events(self, doc, event):
        return self.env['woow.document.log'].sudo().search([
            ('document_id', '=', doc.id), ('event', '=', event)])

    def _cold(self):
        """What a real RPC call starts with: nothing of an earlier call in the cache."""
        self.env.flush_all()
        self.env.invalidate_all()

    def _activities(self, doc):
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.document'), ('res_id', '=', doc.id)])


@tagged('woow_docmgmt_requests')
class TestRequestCreate(RequestsCase):

    def test_a_request_is_a_placeholder_owned_by_the_person_asked(self):
        doc = self._ask(tags=self.tag, note='By Friday')
        doc = doc.sudo()
        self.assertEqual(doc.document_type, 'request')
        self.assertEqual(doc.name, 'Signed contract')
        self.assertEqual(doc.owner_id, self.asked)
        self.assertEqual(doc.request_user_id, self.asked)
        self.assertEqual(doc.create_uid, self.requester)
        self.assertEqual(doc.request_note, 'By Friday')
        self.assertEqual(doc.tag_ids, self.tag)
        self.assertEqual(doc.request_deadline, fields.Date.context_today(doc) + timedelta(days=7))
        self.assertFalse(doc.request_last_reminder)
        self.assertFalse(doc.attachment_id)
        self.assertFalse(doc.datas)
        self.assertTrue(doc.active)

    def test_the_activity_is_an_upload_document_activity_of_the_person_asked(self):
        doc = self._ask(note='Needs <b>care</b>')
        activity = self._activities(doc)
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.user_id, self.asked)
        self.assertEqual(activity.activity_type_id, self.env.ref(_UPLOAD_TYPE))
        self.assertEqual(activity.date_deadline, doc.sudo().request_deadline)
        self.assertEqual(doc.sudo().request_activity_id, activity)
        self.assertNotIn('<b>', activity.note)       # the note is text, never markup
        visible = self.env['mail.activity'].with_user(self.asked).search([('id', '=', activity.id)])
        self.assertEqual(visible, activity)

    def test_the_request_is_logged_without_names_or_files(self):
        doc = self._ask(days=3)
        rows = self._events(doc, 'request_create')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.user_id, self.requester)
        self.assertEqual(rows.folder_id, self.folder)
        self.assertEqual(rows.detail, {'days': 3})
        self.assertFalse(self._events(doc, 'upload'))

    def test_the_existing_write_rule_lets_the_person_asked_write_the_placeholder(self):
        """R-D2: the owner of a document in a folder where the owner may upload writes it."""
        doc = self._ask()
        self.assertTrue(doc.with_user(self.asked)._filtered_access('write'))
        doc.with_user(self.asked).write({'description': '<p>on it</p>'})
        # a reader does not get the write right from the request
        self.assertFalse(doc.with_user(self.reader)._filtered_access('write'))
        self.assertFalse(doc.with_user(self.stranger)._filtered_access('write'))
        self.assertTrue(doc.with_user(self.requester)._filtered_access('read'))
        # an editor of the folder writes it anyway
        self.assertTrue(doc.with_user(self.editor)._filtered_access('write'))
        # no grant is added: the person asked has no access to a folder without a grant
        other = self._folder('Other folder', {})
        self.assertFalse(self.env['woow.document.folder'].with_user(self.asked).search([
            ('id', '=', other.id)]))

    def test_the_requester_writes_the_pending_placeholder_through_rule_r_q3(self):
        doc = self._ask()
        asker = doc.with_user(self.requester)
        self.assertEqual(doc.sudo().owner_id, self.asked)       # not through ownership
        self.assertTrue(asker._filtered_access('write'))
        asker.write({'description': '<p>by the requester</p>'})
        # the rule reaches the placeholder only: not the request fields, not the type, not a file
        for vals in ({'request_user_id': self.stranger.id}, {'request_last_reminder': False},
                     {'request_deadline': '2030-01-01'}):
            with self.assertRaises(AccessError):
                asker.write(vals)
        for vals in ({'document_type': 'binary'}, {'datas': base64.b64encode(_TEXT)}):
            with self.assertRaises(UserError):
                asker.write(vals)
        self.assertEqual(doc.sudo().document_type, 'request')
        # it needs the upload level: a requester who lost the grant, and others, do not write
        self.assertFalse(doc.with_user(self.stranger)._filtered_access('write'))
        self.env['woow.document.folder.access'].sudo().search([
            ('folder_id', '=', self.folder.id), ('user_id', '=', self.requester.id)]).unlink()
        self.env.flush_all()
        self.assertFalse(doc.with_user(self.requester)._filtered_access('write'))

    def test_the_requester_loses_the_write_right_when_the_file_arrives(self):
        doc = self._ask()
        doc.with_user(self.asked)._woow_fulfil_request('a.txt', _TEXT)
        self.assertEqual(doc.sudo().document_type, 'binary')
        self.assertFalse(doc.with_user(self.requester)._filtered_access('write'))
        with self.assertRaises(AccessError):
            doc.with_user(self.requester).write({'description': '<p>no</p>'})

    def test_who_may_request(self):
        for user in (self.reader, self.stranger, self.plain):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self._ask(user=user)
        self.assertTrue(self._ask(user=self.editor))
        self.assertTrue(self._ask(user=self.admin))
        portal = new_test_user(self.env, login='req_portal', groups='base.group_portal')
        with self.assertRaises(AccessError):
            self._ask(user=portal)

    def test_a_trashed_folder_takes_no_request(self):
        folder = self._folder('Trashed folder', {self.requester: 'upload', self.asked: 'upload'})
        folder.with_user(self.admin).action_trash()
        with self.assertRaises(AccessError):
            self._ask(folder=folder)

    def test_the_person_asked_must_be_able_to_store_a_file_there(self):
        nobody = new_test_user(self.env, login='req_nobody', groups=_USER)
        portal = new_test_user(self.env, login='req_portal2', groups='base.group_portal')
        archived = new_test_user(self.env, login='req_archived', groups=_USER)
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': archived.id,
            'level': 'upload'})
        archived.active = False
        company = self.env['res.company'].sudo().create({'name': 'Requests other company'})
        elsewhere = new_test_user(self.env, login='req_elsewhere', groups=_USER)
        elsewhere.sudo().write({'company_ids': [Command.set([company.id])],
                                'company_id': company.id})
        folder = self._folder('Company folder', {self.requester: 'upload', elsewhere: 'upload'})
        nodoc = new_test_user(self.env, login='req_nodoc', groups='base.group_user')
        cases = {'reader': (self.reader, self.folder), 'no grant': (nobody, self.folder),
                 'not a document user': (nodoc, self.folder),
                 'portal': (portal, self.folder), 'archived': (archived, self.folder),
                 'other company': (elsewhere, folder)}
        for label, (asked, where) in cases.items():
            with self.subTest(asked=label):
                before = self.model.sudo().search_count([('document_type', '=', 'request')])
                with self.assertRaises(UserError):
                    self._ask(asked=asked, folder=where)
                self.assertEqual(
                    self.model.sudo().search_count([('document_type', '=', 'request')]), before)
        with self.assertRaises(UserError):
            self._ask(asked=self.requester)          # not oneself
        self.assertEqual(len(self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.document'), ('user_id', 'in', [nobody.id, portal.id])])), 0)

    def test_a_manager_of_documents_and_an_inherited_grant_are_enough(self):
        manager = new_test_user(self.env, login='req_manager', groups=_MANAGER)
        self.assertTrue(self._ask(asked=manager))
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Inherited child', 'parent_id': self.folder.id, 'owner_id': self.admin.id})
        self.env.flush_all()
        self.assertTrue(self._ask(folder=child))

    def test_the_inputs_are_bounded(self):
        for kwargs in ({'days': 0}, {'days': 366}, {'days': True}, {'name': '   '},
                       {'name': 'x/'}, {'note': 'n' * 501}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(UserError):
                    self._ask(**kwargs)
        hidden = self.env['woow.document.tag'].sudo().create({'name': 'Hidden tag'})
        self.assertTrue(self._ask(tags=hidden))      # tags are readable by every document user

    def test_a_client_cannot_make_or_edit_a_request(self):
        marker_free = self.model.with_user(self.requester)
        with self.assertRaises(UserError):
            marker_free.create({'name': 'Forged', 'folder_id': self.folder.id,
                                'document_type': 'request'})
        with self.assertRaises(AccessError):
            marker_free.create({'name': 'forged.txt', 'folder_id': self.folder.id,
                                'datas': base64.b64encode(_TEXT),
                                'request_user_id': self.asked.id})
        with self.assertRaises(UserError):
            marker_free.with_context(**{REQUEST_CTX_KEY: True}).create({
                'name': 'Forged', 'folder_id': self.folder.id, 'document_type': 'request'})
        doc = self._ask()
        for user in (self.asked, self.editor, self.admin):
            for name, value in (('request_user_id', self.stranger.id),
                                ('request_deadline', '2030-01-01'),
                                ('request_note', 'forged'), ('request_activity_id', False),
                                ('request_last_reminder', '2030-01-01 00:00:00')):
                with self.subTest(user=user.login, field=name):
                    with self.assertRaises(AccessError):
                        doc.with_user(user).write({name: value})
        for vals in ({'document_type': 'binary'}, {'datas': base64.b64encode(_TEXT)},
                     {'url': 'https://example.test/x'}):
            with self.subTest(vals=list(vals)):
                with self.assertRaises(UserError):
                    doc.with_user(self.asked).write(vals)
        self.assertEqual(doc.sudo().document_type, 'request')

    def test_the_note_is_never_cut_by_the_wizard(self):
        wizard = self.env['woow.document.request.wizard'].with_user(self.requester)
        exact = wizard.create({'name': 'Exact note', 'folder_id': self.folder.id,
                               'request_user_id': self.asked.id, 'note': 'n' * 500})
        exact.action_create()
        self.assertEqual(len(self.model.sudo().search([('name', '=', 'Exact note')]).request_note), 500)
        long = wizard.create({'name': 'Long note', 'folder_id': self.folder.id,
                              'request_user_id': self.asked.id, 'note': 'n' * 501})
        self.assertEqual(len(long.note), 501)
        with self.assertRaises(UserError):
            long.action_create()
        self.assertFalse(self.model.sudo().search([('name', '=', 'Long note')]))

    def test_the_type_of_a_new_document_cannot_be_set_to_request_on_the_form(self):
        record = self.model.with_user(self.editor).new({'document_type': 'request'})
        result = record._onchange_document_type_request()
        self.assertEqual(record.document_type, 'binary')
        self.assertIn('warning', result)
        existing = self._ask()
        self.assertEqual(existing.with_user(self.asked)._onchange_document_type_request(), None)
        self.assertEqual(existing.sudo().document_type, 'request')

    def test_the_wizard_creates_the_request(self):
        wizard = self.env['woow.document.request.wizard'].with_user(self.requester).create({
            'name': 'Passport copy', 'folder_id': self.folder.id,
            'request_user_id': self.asked.id, 'days': 5, 'tag_ids': [Command.set(self.tag.ids)],
            'note': 'Both sides'})
        self.assertEqual(wizard.action_create()['type'], 'ir.actions.act_window_close')
        doc = self.model.sudo().search([('name', '=', 'Passport copy')])
        self.assertEqual((doc.document_type, doc.owner_id, doc.tag_ids, doc.request_note),
                         ('request', self.asked, self.tag, 'Both sides'))
        self.assertEqual(self._events(doc, 'request_create').detail, {'days': 5})
        # a wizard row is its creator's alone, and a refused request makes nothing
        with self.assertRaises(AccessError):
            wizard.with_user(self.stranger).read(['name'])
        refused = self.env['woow.document.request.wizard'].with_user(self.reader).create({
            'name': 'Nope', 'folder_id': self.folder.id, 'request_user_id': self.asked.id})
        with self.assertRaises(AccessError):
            refused.action_create()
        self.assertFalse(self.model.sudo().search([('name', '=', 'Nope')]))
        # the access rows: documents users may use the wizard; portal and plain users may not
        with self.assertRaises(AccessError):
            self.env['woow.document.request.wizard'].with_user(self.plain).create({
                'name': 'Nope', 'folder_id': self.folder.id, 'request_user_id': self.asked.id})


@tagged('woow_docmgmt_requests')
class TestRequestFulfil(RequestsCase):

    def setUp(self):
        super().setUp()
        self.doc = self._ask(name='Signed contract', note='please')
        self.activity = self.doc.sudo().request_activity_id

    def _fulfil(self, user, name='contract.txt', data=_TEXT, doc=None):
        return (doc or self.doc).with_user(user)._woow_fulfil_request(name, data)

    def _assert_fulfilled(self):
        doc = self.doc.sudo()
        self.assertEqual(doc.document_type, 'binary')
        self.assertEqual(doc.name, 'Signed contract.txt')
        self.assertEqual(doc.mimetype, 'text/plain')
        self.assertEqual(base64.b64decode(doc.datas), _TEXT)
        self.assertEqual(doc.file_size, len(_TEXT))
        self.assertTrue(doc.checksum)
        self.assertFalse(doc.request_activity_id)
        self.assertFalse(self.activity.exists())
        self.assertFalse(self._activities(doc))
        self.assertTrue(doc.active)

    def test_the_person_asked_fulfils_the_request(self):
        self.assertTrue(self._fulfil(self.asked))
        self._assert_fulfilled()
        self.assertEqual(self.doc.sudo().owner_id, self.asked)
        rows = self._events(self.doc, 'request_done')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows.user_id, rows.detail), (self.asked, {'bytes': len(_TEXT)}))
        done = self.env['mail.message'].sudo().search([
            ('model', '=', 'woow.document'), ('res_id', '=', self.doc.id),
            ('body', 'ilike', 'requested file was uploaded')])
        self.assertEqual(len(done), 1)
        # it is a normal document now: no request is pending, nothing to fill twice
        with self.assertRaises(UserError):
            self._fulfil(self.asked, name='again.txt')

    def test_an_editor_of_the_folder_fulfils_it_too(self):
        self.assertTrue(self._fulfil(self.editor))
        self._assert_fulfilled()
        self.assertEqual(self._events(self.doc, 'request_done').user_id, self.editor)

    def test_the_requester_may_send_the_file_too(self):
        # the requester can write the pending placeholder (R-Q3) and has upload level anyway
        self.assertTrue(self._fulfil(self.requester))
        self._assert_fulfilled()

    def test_a_document_administrator_fulfils_it(self):
        self.assertTrue(self._fulfil(self.admin))
        self._assert_fulfilled()

    def test_others_cannot_fulfil_it(self):
        portal = new_test_user(self.env, login='req_portal3', groups='base.group_portal')
        for user in (self.reader, self.stranger, self.plain, portal):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self._fulfil(user)
                with self.assertRaises(AccessError):
                    self.doc.with_user(user).action_open_fulfil_wizard()
        doc = self.doc.sudo()
        self.assertEqual((doc.document_type, bool(doc.datas)), ('request', False))
        self.assertTrue(self.activity.exists())
        self.assertFalse(self._events(self.doc, 'request_done'))

    def test_the_person_asked_loses_the_right_when_the_grant_is_removed(self):
        self.env['woow.document.folder.access'].sudo().search([
            ('folder_id', '=', self.folder.id), ('user_id', '=', self.asked.id)]).unlink()
        self.env.flush_all()
        with self.assertRaises(AccessError):
            self._fulfil(self.asked)
        self.assertTrue(self._fulfil(self.editor))

    def test_a_trashed_request_cannot_be_fulfilled(self):
        self.doc.with_user(self.asked).action_trash()
        with self.assertRaises(UserError):      # AccessError is a UserError
            self._fulfil(self.asked)
        self.assertEqual(self.doc.sudo().document_type, 'request')

    def test_the_upload_policy_applies(self):
        for label, name, data in (
                ('blocked extension', 'run.exe', b'MZ'),
                ('content does not match', 'file.pdf', b'this is not a pdf'),
                ('no extension', 'contract', _TEXT),
                ('binary text', 'file.txt', b'a\x00b')):
            with self.subTest(case=label):
                with self.assertRaises(WoowUploadRejected):
                    self._fulfil(self.asked, name=name, data=data)
        with self.assertRaises(WoowUploadRejected):
            self._fulfil(self.asked, name='big.txt', data=b'x' * (
                self.env['woow.document.settings']._get_settings().internal_max_upload_mb * 1024 * 1024
                + 1))
        doc = self.doc.sudo()
        self.assertEqual((doc.document_type, bool(doc.datas)), ('request', False))
        self.assertTrue(self.activity.exists())

    def test_the_quota_applies(self):
        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '1')
        with self.assertRaises(WoowUploadRejected) as caught:
            self._fulfil(self.asked, name='big.txt', data=b'x' * (1024 * 1024))
        self.assertEqual(caught.exception.code, 'quota')
        self.assertEqual(self.doc.sudo().document_type, 'request')
        self.assertTrue(self._fulfil(self.asked))      # a small file still fits

    def test_the_name_keeps_the_request_and_takes_the_extension_of_the_file(self):
        doc = self._ask(name='Report.txt')
        self.assertTrue(self._fulfil(self.asked, name='anything.txt', doc=doc))
        self.assertEqual(doc.sudo().name, 'Report.txt')

    def test_the_wizard_fulfils_the_request(self):
        action = self.doc.with_user(self.asked).action_open_fulfil_wizard()
        self.assertEqual(action['res_model'], 'woow.document.request.fulfil.wizard')
        wizard = self.env['woow.document.request.fulfil.wizard'].with_user(self.asked).create({
            'document_id': self.doc.id, 'file': base64.b64encode(_TEXT), 'filename': 'a.txt'})
        self.assertEqual(wizard.action_fulfil()['type'], 'ir.actions.act_window_close')
        self._assert_fulfilled()

    def test_the_wizard_checks_the_live_rights_of_its_user(self):
        wizard = self.env['woow.document.request.fulfil.wizard'].with_user(self.stranger).create({
            'document_id': self.doc.id, 'file': base64.b64encode(_TEXT), 'filename': 'a.txt'})
        with self.assertRaises(AccessError):
            wizard.action_fulfil()
        empty = self.env['woow.document.request.fulfil.wizard'].with_user(self.asked).create({
            'document_id': self.doc.id})
        with self.assertRaises(UserError):
            empty.action_fulfil()
        broken = self.env['woow.document.request.fulfil.wizard'].with_user(self.asked).create({
            'document_id': self.doc.id, 'file': b'!!!not base64!!!', 'filename': 'a.txt'})
        with self.assertRaises(WoowUploadRejected):
            broken.action_fulfil()
        self.assertEqual(self.doc.sudo().document_type, 'request')
        with self.assertRaises(AccessError):
            wizard.with_user(self.asked).read(['filename'])      # not the creator's row

    def test_a_retargeted_task_posts_nothing_on_another_record(self):
        hidden_folder = self._folder('Hidden folder', {})
        hidden = self.model.sudo().create({
            'name': 'hidden.txt', 'folder_id': hidden_folder.id,
            'datas': base64.b64encode(_TEXT)})
        self.env.flush_all()
        self._cold()
        # the requester made the task and may write it; the new target is not checked by core
        self.activity.with_user(self.requester).write({'res_id': hidden.id})
        self.env.flush_all()
        before = self.env['mail.message'].sudo().search_count([
            ('model', '=', 'woow.document'), ('res_id', '=', hidden.id)])
        self._cold()
        self.assertTrue(self._fulfil(self.asked))
        after = self.env['mail.message'].sudo().search_count([
            ('model', '=', 'woow.document'), ('res_id', '=', hidden.id)])
        self.assertEqual(after, before)
        doc = self.doc.sudo()
        self.assertEqual(doc.document_type, 'binary')
        self.assertFalse(doc.request_activity_id)
        self.assertEqual(len(self._events(self.doc, 'request_done')), 1)
        note = self.env['mail.message'].sudo().search([
            ('model', '=', 'woow.document'), ('res_id', '=', self.doc.id),
            ('body', 'ilike', 'requested file was uploaded')])
        self.assertEqual(len(note), 1)

    def test_the_core_upload_button_and_mark_done_do_not_end_the_task(self):
        attachment = self.env['ir.attachment'].with_user(self.asked).create({
            'name': 'run.exe', 'raw': b'MZ', 'res_model': 'mail.compose.message', 'res_id': 0})
        self._cold()
        with self.assertRaises(UserError):
            self.activity.with_user(self.asked).action_feedback(
                feedback='done', attachment_ids=[attachment.id])
        with self.assertRaises(UserError):
            self.activity.with_user(self.asked).action_done()
        with self.assertRaises(UserError):
            self.activity.with_user(self.editor).action_feedback(feedback='done')
        doc = self.doc.sudo()
        self.assertEqual((doc.document_type, doc.request_activity_id), ('request', self.activity))
        self.assertTrue(self.activity.exists())
        self.assertEqual(attachment.res_model, 'mail.compose.message')   # nothing was re-parented
        self.assertTrue(self._fulfil(self.asked))

    def test_an_ordinary_task_on_a_document_is_completed_as_before(self):
        plain = self.model.with_user(self.editor).create({
            'name': 'plain.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(_TEXT)})
        task = plain.activity_schedule(_UPLOAD_TYPE, summary='bring it', user_id=self.editor.id)
        self.assertTrue(task.action_feedback(feedback='ok') is not None)
        self.assertFalse(task.exists())
        other = self.env['res.partner'].sudo().create({'name': 'Task partner'})
        todo = other.sudo().activity_schedule(_UPLOAD_TYPE, user_id=self.plain.id)
        todo.with_user(self.plain).action_feedback(feedback='ok')      # no access to documents
        self.assertFalse(todo.exists())

    def test_a_document_that_is_not_a_request_has_no_fulfil_path(self):
        plain = self.model.with_user(self.editor).create({
            'name': 'plain.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(_TEXT)})
        with self.assertRaises(UserError):
            plain._woow_fulfil_request('b.txt', _TEXT)
        with self.assertRaises(UserError):
            plain.action_open_fulfil_wizard()
        with self.assertRaises(UserError):
            plain.action_send_reminder()


@tagged('woow_docmgmt_requests')
class TestRequestReminderAndCancel(RequestsCase):

    def setUp(self):
        super().setUp()
        self.doc = self._ask()

    def _mails(self):
        return self.env['mail.mail'].sudo().search([
            ('model', '=', 'woow.document'), ('res_id', '=', self.doc.id),
            ('subject', 'ilike', 'still requested')])

    def test_the_requester_sends_a_reminder_once_per_period(self):
        self.assertTrue(self.doc.with_user(self.requester).action_send_reminder())
        mails = self._mails()
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails.recipient_ids, self.asked.partner_id)
        self.assertIn('https://docs.example.test/web#id=%d&amp;model=woow.document' % self.doc.id,
                      mails.body_html)
        self.assertNotIn('access_token', mails.body_html)
        self.assertIn('Signed contract', mails.body_html)
        self.assertEqual(mails.state, 'outgoing')       # queued, not sent now
        self.assertTrue(self.doc.sudo().request_last_reminder)
        with self.assertRaises(UserError):
            self.doc.with_user(self.requester).action_send_reminder()
        with self.assertRaises(UserError):
            self.doc.with_user(self.admin).action_send_reminder()
        self.assertEqual(len(self._mails()), 1)
        # just inside the limit it is still refused, just past it it goes out again
        now = fields.Datetime.now()
        self.doc.sudo().request_last_reminder = now - timedelta(
            hours=REQUEST_REMINDER_MIN_HOURS - 1)
        with self.assertRaises(UserError):
            self.doc.with_user(self.requester).action_send_reminder()
        self.doc.sudo().request_last_reminder = now - timedelta(
            hours=REQUEST_REMINDER_MIN_HOURS + 1)
        self.assertTrue(self.doc.with_user(self.requester).action_send_reminder())
        self.assertEqual(len(self._mails()), 2)

    def test_a_reminder_works_on_a_cold_cache_for_a_requester_with_upload_level_only(self):
        # core reads an activity that the user is not assigned to only with write access on its
        # record: the requester has it through rule R-Q3 (a warm cache hid this once)
        self.assertEqual(self.folder.with_user(self.requester).woow_user_rank, 2)
        self._cold()
        self.assertTrue(self.doc.with_user(self.requester).action_send_reminder())
        self.assertEqual(len(self._mails()), 1)
        self._cold()
        with self.assertRaises(UserError):
            self.doc.with_user(self.requester).action_send_reminder()
        self.assertEqual(len(self._mails()), 1)
        activity = self._activities(self.doc)
        self._cold()
        self.assertTrue(activity.with_user(self.requester).has_access('read'))
        self.assertEqual(activity.with_user(self.requester).user_id, self.asked)

    def test_the_limit_is_not_in_a_field_that_the_two_users_can_write(self):
        self.assertNotIn('woow_request_last_reminder', self.env['mail.activity']._fields)
        self.assertTrue(self.doc.with_user(self.requester).action_send_reminder())
        stamp = self.doc.sudo().request_last_reminder
        self.assertTrue(stamp)
        far = fields.Datetime.now() + timedelta(days=3650)
        for user in (self.requester, self.asked, self.editor, self.stranger):
            for value in (False, far):
                with self.subTest(user=user.login, value=bool(value)):
                    self._cold()
                    with self.assertRaises(AccessError):
                        self.doc.with_user(user).write({'request_last_reminder': value})
        self.assertEqual(self.doc.sudo().request_last_reminder, stamp)
        self._cold()
        with self.assertRaises(UserError):
            self.doc.with_user(self.requester).action_send_reminder()
        self.assertEqual(len(self._mails()), 1)

    def test_a_removed_task_is_scheduled_again_by_the_reminder(self):
        activity = self.doc.sudo().request_activity_id
        activity.unlink()
        self.assertFalse(self.doc.sudo().request_activity_id)
        self._cold()
        self.assertTrue(self.doc.with_user(self.requester).action_send_reminder())
        new = self.doc.sudo().request_activity_id
        self.assertTrue(new)
        self.assertEqual((new.user_id, new.res_id), (self.asked, self.doc.id))
        self.assertEqual(len(self._mails()), 1)

    def test_a_folder_manager_sends_a_reminder_and_others_cannot(self):
        manager = new_test_user(self.env, login='req_fmanager', groups=_USER)
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': manager.id,
            'level': 'manage'})
        self.env.flush_all()
        for user in (self.editor, self.reader, self.asked):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self.doc.with_user(user).action_send_reminder()
        with self.assertRaises(AccessError):
            self.doc.with_user(self.stranger).action_send_reminder()
        self.assertFalse(self._mails())
        self.assertTrue(self.doc.with_user(manager).action_send_reminder())
        self.assertEqual(len(self._mails()), 1)

    def test_a_reminder_needs_a_reachable_person_who_can_still_upload(self):
        self.asked.partner_id.email = False
        with self.assertRaises(UserError):
            self.doc.with_user(self.requester).action_send_reminder()
        self.asked.partner_id.email = 'asked@example.test'
        self.env['woow.document.folder.access'].sudo().search([
            ('folder_id', '=', self.folder.id), ('user_id', '=', self.asked.id)]).unlink()
        self.env.flush_all()
        with self.assertRaises(UserError):
            self.doc.with_user(self.requester).action_send_reminder()
        self.assertFalse(self._mails())
        self.assertFalse(self.doc.sudo().request_last_reminder)

    def test_cancelling_is_trashing_and_removes_the_activity(self):
        activity = self.doc.sudo().request_activity_id
        self.assertTrue(self.doc.with_user(self.editor).action_trash())
        doc = self.doc.sudo().with_context(active_test=False)
        self.assertFalse(doc.active)
        self.assertEqual(doc.trashed_by_id, self.editor)
        self.assertFalse(activity.exists())
        self.assertFalse(doc.request_activity_id)
        self.assertTrue(self._events(self.doc, 'trash'))
        with self.assertRaises(UserError):
            self.doc.with_user(self.admin).action_send_reminder()
        with self.assertRaises(UserError):
            self.doc.with_user(self.asked).action_open_fulfil_wizard()

    def test_who_may_cancel(self):
        # the write rules decide: a reader and a stranger do not write the placeholder
        for user in (self.reader, self.stranger, self.plain):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self.doc.with_user(user).action_trash()
        self.assertTrue(self.doc.sudo().active)
        self.assertTrue(self.doc.sudo().request_activity_id.exists())
        self.assertTrue(self.doc.with_user(self.admin).action_trash())

    def test_the_requester_with_upload_level_cancels_the_request(self):
        activity = self.doc.sudo().request_activity_id
        self._cold()
        self.assertTrue(self.doc.with_user(self.requester).action_trash())
        doc = self.doc.sudo().with_context(active_test=False)
        self.assertFalse(doc.active)
        self.assertEqual(doc.trashed_by_id, self.requester)
        self.assertFalse(activity.exists())
        self.assertTrue(self._events(self.doc, 'trash'))

    def test_restoring_a_cancelled_request_gives_the_person_asked_a_task_again(self):
        self.doc.with_user(self.asked).action_trash()
        self.assertFalse(self._activities(self.doc))
        self.assertTrue(self.doc.with_user(self.editor).action_restore())
        doc = self.doc.sudo()
        self.assertTrue(doc.active)
        task = self._activities(doc)
        self.assertEqual(len(task), 1)
        self.assertEqual((task.user_id, doc.request_activity_id), (self.asked, task))
        self.assertGreaterEqual(task.date_deadline, fields.Date.context_today(doc))
        self.assertEqual(task.activity_type_id, self.env.ref(_UPLOAD_TYPE))
        self._cold()
        self.assertTrue(self.doc.with_user(self.requester).action_send_reminder())
        self.assertEqual(len(self._activities(doc)), 1)       # no second task
        self.assertTrue(self.doc.with_user(self.asked)._woow_fulfil_request('a.txt', _TEXT))

    def test_restoring_does_not_ask_someone_who_cannot_store_the_file(self):
        self.doc.with_user(self.asked).action_trash()
        self.env['woow.document.folder.access'].sudo().search([
            ('folder_id', '=', self.folder.id), ('user_id', '=', self.asked.id)]).unlink()
        self.env.flush_all()
        self.assertTrue(self.doc.with_user(self.editor).action_restore())
        self.assertTrue(self.doc.sudo().active)
        self.assertFalse(self._activities(self.doc))

    def test_the_person_asked_trashes_it_and_the_activity_goes_too(self):
        activity = self.doc.sudo().request_activity_id
        self.assertTrue(self.doc.with_user(self.asked).action_trash())
        self.assertFalse(self.doc.sudo().active)
        self.assertFalse(activity.exists())

    def test_the_placeholder_can_be_removed_for_good(self):
        activity = self.doc.sudo().request_activity_id
        self.doc.with_user(self.asked).action_trash()
        self.assertEqual(self.doc.sudo().with_context(active_test=False)._woow_purge(), (1, 0))
        self.assertFalse(self.doc.exists())
        self.assertFalse(activity.exists())


@tagged('woow_docmgmt_requests')
class TestRequestIsNoFile(RequestsCase):

    def setUp(self):
        super().setUp()
        self.doc = self._ask()

    def test_a_placeholder_has_no_content_on_the_stream_path(self):
        doc = self.doc.with_user(self.asked)
        self.assertFalse(doc.datas)
        self.assertEqual(doc.sudo().file_size, 0)
        self.assertFalse(doc.sudo().checksum)
        with self.assertRaises(MissingError):
            self.env['ir.binary'].with_user(self.asked)._record_to_stream(doc, 'datas')

    def test_the_search_filter_finds_requests(self):
        found = self.model.with_user(self.asked).search([('document_type', '=', 'request')])
        self.assertEqual(found, self.doc)
        mine = self.model.with_user(self.asked).search([
            ('document_type', '=', 'request'), ('request_user_id', '=', self.asked.id)])
        self.assertEqual(mine, self.doc)
        filters = self.env['ir.ui.view'].sudo().search([
            ('model', '=', 'woow.document'), ('type', '=', 'search')])
        arch = ''.join(filters.mapped('arch_db'))
        self.assertIn('woow_requested_files', arch)

    def test_portal_users_never_see_a_placeholder(self):
        portal = new_test_user(self.env, login='req_portal4', groups='base.group_portal')
        partner = portal.partner_id
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner', 'partner_id': partner.id,
            'level': 'upload'})
        visible = self.model.sudo().create({
            'name': 'visible.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(_TEXT)})
        self.env.flush_all()
        docs = self.model.with_user(portal)
        self.assertEqual(docs.search([('folder_id', '=', self.folder.id)]), visible)
        self.assertFalse(docs.search([('document_type', '=', 'request')]))
        self.assertEqual(docs.search_count([('id', '=', self.doc.id)]), 0)
        with self.assertRaises(AccessError):
            self.doc.with_user(portal).read(['name'])
        # a direct share does not show it either
        self.doc.sudo().write({'shared_partner_ids': [Command.set([partner.id])]})
        self.env.flush_all()
        self.assertEqual(docs.search_count([('id', '=', self.doc.id)]), 0)
        self.assertFalse(docs._woow_portal_direct_documents())
        with self.assertRaises(AccessError):
            self.doc.with_user(portal).read(['name'])
        # the request fields are not readable by a share user in any case
        with self.assertRaises(AccessError):
            self.doc.with_user(portal).read(['request_user_id'])
        # internal users are not restricted by the rule
        self.assertEqual(self.model.with_user(self.requester).search_count([
            ('id', '=', self.doc.id)]), 1)


@tagged('woow_docmgmt_requests_http')
class TestRequestRoutes(HttpCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.pw = secrets.token_urlsafe(16)
        self.admin = env.ref('base.user_admin')
        self.asked = new_test_user(env, login='reqhttp_asked', groups=_USER, password=self.pw)
        self.folder = env['woow.document.folder'].sudo().create({
            'name': 'Requests http folder', 'owner_id': self.admin.id})
        env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.asked.id,
            'level': 'upload'})
        portal = new_test_user(env, login='reqhttp_portal', groups='base.group_portal',
                               password=self.pw)
        env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner',
            'partner_id': portal.partner_id.id, 'level': 'read'})
        env.flush_all()
        self.doc = env['woow.document'].with_user(self.admin)._woow_open_request(
            self.folder, 'Http request', self.asked, 5)

    def test_download_preview_and_zip_know_no_file_for_a_placeholder(self):
        self.authenticate('reqhttp_asked', self.pw)
        for path in ('download', 'preview'):
            response = self.url_open('/woow_document/doc/%d/%s' % (self.doc.id, path))
            self.assertEqual(response.status_code, 404, path)
        content = self.url_open('/web/content/woow.document/%d/datas' % self.doc.id)
        self.assertNotEqual(content.status_code, 200)
        archive = self.url_open('/woow_document/zip?document_ids=%d' % self.doc.id)
        if archive.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
                self.assertEqual(zipped.namelist(), [])
        self.url_open('/web/session/logout')

    def test_a_portal_contact_gets_nothing_for_a_placeholder(self):
        self.authenticate('reqhttp_portal', self.pw)
        for path in ('/my/docs/file/%d' % self.doc.id, '/my/docs/file/%d/download' % self.doc.id):
            self.assertEqual(self.url_open(path).status_code, 404, path)
        page = self.url_open('/my/docs/folder/%d' % self.folder.id)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'Http request', page.content)
        archive = self.url_open('/my/docs/folder/%d/zip' % self.folder.id)
        if archive.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
                self.assertEqual([name for name in zipped.namelist() if not name.endswith('/')], [])
