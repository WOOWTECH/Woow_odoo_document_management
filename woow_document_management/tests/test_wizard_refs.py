# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Wizard rows: a missing id and a hidden id get one answer, and a waiting file is bounded."""
import base64

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.upload_policy import WoowUploadRejected
from ..services.constants import MIB

_USER = 'woow_document_management.group_woow_document_user'
_PARAM = 'woow_document_management.'


class WizardRefsCase(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = env.ref('base.user_admin')
        self.outsider = new_test_user(env, login='wr_outsider', groups=_USER)
        self.other = new_test_user(env, login='wr_other', groups=_USER)
        folders = env['woow.document.folder'].sudo()
        self.visible = folders.create({'name': 'Visible', 'owner_id': self.admin.id})
        env['woow.document.folder.access'].sudo().create({
            'folder_id': self.visible.id, 'subject_type': 'user',
            'user_id': self.outsider.id, 'level': 'edit'})
        env['woow.document.folder.access'].sudo().create({
            'folder_id': self.visible.id, 'subject_type': 'user',
            'user_id': self.other.id, 'level': 'edit'})
        self.hidden = folders.create({'name': 'Hidden', 'owner_id': self.admin.id})
        self.hidden_doc = env['woow.document'].sudo().create({
            'name': 'hidden.txt', 'folder_id': self.hidden.id,
            'datas': base64.b64encode(b'hidden')})
        self.visible_doc = env['woow.document'].sudo().create({
            'name': 'visible.txt', 'folder_id': self.visible.id,
            'datas': base64.b64encode(b'visible')})
        env.flush_all()

    def _last_id(self, model):
        return self.env[model].sudo().with_context(active_test=False).search(
            [], order='id desc', limit=1).id + 1000


@tagged('woow_docmgmt_wizard_refs')
class TestWizardRefsAreNotAnOracle(WizardRefsCase):

    def _values(self, kind, hidden):
        """The value that names a record of ``kind``: one the caller cannot see, or a missing one."""
        if kind in ('folder', 'folders'):
            value = self.hidden.id if hidden else self._last_id('woow.document.folder')
        elif kind in ('document', 'documents'):
            value = self.hidden_doc.id if hidden else self._last_id('woow.document')
        elif kind == 'upload_wizard':
            value = self.other_upload.id if hidden else self._last_id('woow.document.upload.wizard')
        elif kind == 'merge_wizard':
            value = self.other_merge.id if hidden else self._last_id('woow.document.pdf.merge.wizard')
        else:
            raise AssertionError(kind)
        return [Command.set([value])] if kind in ('folders', 'documents') else value

    def test_a_missing_id_and_a_hidden_id_are_refused_alike_by_every_wizard(self):
        env = self.env
        self.other_upload = env['woow.document.upload.wizard'].with_user(self.other).create({
            'folder_id': self.visible.id})
        self.other_merge = env['woow.document.pdf.merge.wizard'].with_user(self.other).create({
            'folder_id': self.visible.id, 'name': 'other.pdf'})
        own_merge = env['woow.document.pdf.merge.wizard'].with_user(self.outsider).create({
            'folder_id': self.visible.id, 'name': 'own.pdf'})
        vis, vis_doc = self.visible.id, self.visible_doc.id
        vis_docs = [Command.set([vis_doc])]
        cases = [
            ('woow.document.upload.wizard', 'folder_id', 'folder', {}),
            ('woow.document.upload.wizard.line', 'wizard_id', 'upload_wizard', {'name': 'a.txt'}),
            ('woow.document.rename.wizard', 'document_id', 'document', {'name': 'x'}),
            ('woow.document.rename.wizard', 'folder_id', 'folder', {'name': 'x'}),
            ('woow.document.request.wizard', 'folder_id', 'folder',
             {'name': 'n', 'request_user_id': self.other.id}),
            ('woow.document.request.fulfil.wizard', 'document_id', 'document', {}),
            ('woow.document.move.wizard', 'document_ids', 'documents', {'target_folder_id': vis}),
            ('woow.document.move.wizard', 'folder_ids', 'folders', {'target_folder_id': vis}),
            ('woow.document.move.wizard', 'target_folder_id', 'folder', {}),
            ('woow.document.restore.wizard', 'document_ids', 'documents',
             {'target_folder_id': vis}),
            ('woow.document.restore.wizard', 'folder_ids', 'folders', {'target_folder_id': vis}),
            ('woow.document.restore.wizard', 'target_folder_id', 'folder',
             {'document_ids': vis_docs}),
            ('woow.document.purge.wizard', 'document_ids', 'documents', {}),
            ('woow.document.purge.wizard', 'folder_ids', 'folders', {'document_ids': vis_docs}),
            ('woow.document.add.attachment.wizard', 'folder_id', 'folder', {}),
            ('woow.document.notify.wizard', 'folder_id', 'folder', {}),
            ('woow.document.shortcut.wizard', 'document_id', 'document', {'folder_id': vis}),
            ('woow.document.shortcut.wizard', 'folder_id', 'folder', {'document_id': vis_doc}),
            ('woow.document.version.wizard', 'document_id', 'document', {}),
            ('woow.document.pdf.split.wizard', 'document_id', 'document', {}),
            ('woow.document.pdf.merge.wizard', 'folder_id', 'folder', {'name': 'm.pdf'}),
            ('woow.document.pdf.merge.line', 'document_id', 'document',
             {'wizard_id': own_merge.id}),
            ('woow.document.pdf.merge.line', 'wizard_id', 'merge_wizard', {'document_id': vis_doc}),
        ]
        for model_name, field, kind, extra in cases:
            model = env[model_name].with_user(self.outsider)
            before = env[model_name].sudo().search_count([])
            answers = []
            for hidden in (True, False):
                with self.subTest(model=model_name, field=field, hidden=hidden):
                    with self.assertRaises(AccessError) as refused:
                        model.create(dict(extra, **{field: self._values(kind, hidden)}))
                    answers.append((type(refused.exception), str(refused.exception)))
            with self.subTest(model=model_name, field=field):
                self.assertEqual(answers[0], answers[1])
                self.assertEqual(env[model_name].sudo().search_count([]), before)

    def test_a_record_of_the_caller_is_accepted_and_a_later_write_is_checked(self):
        env = self.env
        vis = self.visible.id
        wizard = env['woow.document.move.wizard'].with_user(self.outsider).create({
            'document_ids': [Command.set([self.visible_doc.id])], 'target_folder_id': vis})
        self.assertEqual(wizard.document_ids, self.visible_doc)
        for vals in ({'document_ids': [Command.link(self.hidden_doc.id)]},
                     {'document_ids': [Command.set([self._last_id('woow.document')])]},
                     {'target_folder_id': self.hidden.id},
                     {'target_folder_id': self._last_id('woow.document.folder')},
                     {'folder_ids': [self.hidden.id]}):
            with self.subTest(vals=vals):
                with self.assertRaises(AccessError):
                    wizard.write(vals)
        self.assertEqual(wizard.document_ids, self.visible_doc)
        self.assertEqual(wizard.target_folder_id, self.visible)
        rename = env['woow.document.rename.wizard'].with_user(self.outsider).create({
            'document_id': self.visible_doc.id, 'name': 'renamed'})
        for vals in ({'document_id': self.hidden_doc.id}, {'folder_id': self.hidden.id}):
            with self.assertRaises(AccessError):
                rename.write(vals)

    def test_the_default_of_the_context_goes_through_the_same_check(self):
        model = self.env['woow.document.notify.wizard'].with_user(self.outsider)
        with self.assertRaises(AccessError) as hidden:
            model.with_context(default_folder_id=self.hidden.id).create({})
        with self.assertRaises(AccessError) as missing:
            model.with_context(default_folder_id=self._last_id('woow.document.folder')).create({})
        self.assertEqual(str(hidden.exception), str(missing.exception))

    def test_a_tag_that_does_not_exist_is_refused_with_one_answer(self):
        missing = self._last_id('woow.document.tag')
        for model_name, extra in (
                ('woow.document.upload.wizard', {'folder_id': self.visible.id}),
                ('woow.document.request.wizard', {
                    'folder_id': self.visible.id, 'name': 'n',
                    'request_user_id': self.other.id})):
            with self.subTest(model=model_name):
                with self.assertRaises(AccessError):
                    self.env[model_name].with_user(self.outsider).create(
                        dict(extra, tag_ids=[Command.set([missing])]))

    def test_a_missing_filing_rule_is_refused_with_the_refusal_of_the_field(self):
        with self.assertRaises(AccessError):
            self.env['woow.document.autofile.wizard'].with_user(self.admin).create({
                'rule_id': self._last_id('woow.document.auto.rule')})

    def test_the_superuser_is_not_checked(self):
        # server code that works with sudo() names the ids itself
        wizard = self.env['woow.document.notify.wizard'].sudo().create({'folder_id': self.hidden.id})
        self.assertEqual(wizard.folder_id, self.hidden)


@tagged('woow_docmgmt_wizard_refs')
class TestFulfilWizardBytesAreBounded(WizardRefsCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(_PARAM + 'internal_max_upload_mb', '1')
        self.wizards = self.env['woow.document.request.fulfil.wizard'].with_user(self.outsider)

    def _file(self, size):
        return base64.b64encode(b'x' * size)

    def _row(self, size=100, user=None):
        model = self.wizards if user is None else self.wizards.with_user(user)
        return model.create({'document_id': self.visible_doc.id, 'file': self._file(size),
                             'filename': 'a.txt'})

    def _kept(self, user):
        return sum(self.env['woow.document.request.fulfil.wizard'].sudo().search(
            [('create_uid', '=', user.id)]).mapped('file_size'))

    def test_one_file_is_at_most_the_upload_limit(self):
        self._row(MIB)
        with self.assertRaises(WoowUploadRejected):
            self._row(MIB + 100)
        row = self._row(10)
        with self.assertRaises(WoowUploadRejected):
            row.write({'file': self._file(MIB + 100)})
        self.assertEqual(row.file_size, 10)
        with self.assertRaises(WoowUploadRejected):
            self.wizards.create({'document_id': self.visible_doc.id, 'file': 12345})

    def test_the_size_is_set_by_the_server(self):
        with self.assertRaises(AccessError):
            self.wizards.create({'document_id': self.visible_doc.id, 'file_size': 1})
        row = self._row(300)
        with self.assertRaises(AccessError):
            row.write({'file_size': 0})
        self.assertEqual(row.file_size, 300)

    def test_the_bytes_of_abandoned_dialogs_are_dropped(self):
        rows = [self._row(1000 + number) for number in range(8)]
        kept = [row for row in rows if row.sudo().file_size]
        self.assertEqual(len(kept), 5)
        self.assertEqual(kept, rows[-5:])
        self.assertFalse(any(row.sudo().file for row in rows[:3]))
        # the dialogs of another user are not touched
        theirs = self._row(500, user=self.other)
        self.assertEqual(theirs.sudo().file_size, 500)
        self._row(10)
        self.assertEqual(theirs.sudo().file_size, 500)

    def test_what_a_user_keeps_waiting_is_bounded(self):
        param = self.env['ir.config_parameter'].sudo()
        param.set_param(_PARAM + 'internal_max_upload_mb', '2')
        for _number in range(5):
            self._row(2 * MIB)
        self.assertEqual(self._kept(self.outsider), 10 * MIB)
        param.set_param(_PARAM + 'internal_max_upload_mb', '1')
        # the limit was lowered: what waits is over the new bound, so another file is refused
        with self.assertRaises(WoowUploadRejected):
            self._row(10)
