# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Whole-site filestore accounting and quota threshold checks."""

import base64
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from ..models.upload_policy import WoowUploadRejected
from ..services import locks
from ..services.locks import try_lock


@tagged('woow_docmgmt_files')
class TestQuota(TransactionCase):

    def setUp(self):
        super().setUp()
        self.quota = self.env['woow.document.quota']
        self.params = self.env['ir.config_parameter'].sudo()

    def test_disabled_quota_does_not_query_storage(self):
        self.assertIsNone(self.quota._check('missing', 20 * 1024 * 1024))

    def test_existing_file_is_counted_once(self):
        content = b'quota-integration-content' * 30
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'quota.txt', 'type': 'binary',
            'res_model': 'res.partner', 'res_id': self.env.user.partner_id.id,
            'datas': base64.b64encode(content),
        })
        self.assertTrue(attachment.store_fname)
        self.assertGreaterEqual(self.quota._usage_bytes(), len(content))
        self.assertEqual(self.quota._bytes_if_new(attachment.checksum, len(content)), 0)
        self.assertEqual(self.quota._bytes_if_new('new-checksum', len(content)), len(content))

    def test_block_threshold_rejects_new_file(self):
        self.params.set_param('woow_document_management.quota_mb', '1')
        with self.assertRaises(WoowUploadRejected) as caught:
            self.quota._check('new-checksum', 1024 * 1024)
        self.assertEqual(caught.exception.code, 'quota')

    def test_warning_cron_requires_su_and_deduplicates(self):
        with self.assertRaises(AccessError):
            self.quota.with_user(self.env.ref('base.public_user'))._cron_check_warn()
        self.params.set_param('woow_document_management.quota_mb', '1')
        self.params.set_param('woow_document_management.quota_warn_pct', '1')
        group = self.env.ref('woow_document_management.group_woow_document_manager')
        manager = self.env.ref('base.user_admin')
        group.write({'users': [(4, manager.id)]})
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'warning.txt', 'type': 'binary',
            'res_model': 'res.partner', 'res_id': manager.partner_id.id,
            'datas': base64.b64encode(b'w' * 20_000),
        })
        self.assertTrue(attachment.store_fname)
        self.quota.sudo()._cron_check_warn()
        self.quota.sudo()._cron_check_warn()
        domain = [
            ('res_model', '=', 'res.partner'),
            ('res_id', '=', manager.partner_id.id),
            ('user_id', '=', manager.id),
            ('summary', '=', 'Document storage capacity warning'),
        ]
        activities = self.env['mail.activity'].sudo().with_context(active_test=False)
        self.assertEqual(activities.search_count(domain), 1)
        activity = activities.search(domain)
        self.assertTrue(activity.activity_type_id.keep_done)
        activity.action_feedback()
        self.assertFalse(activity.active)
        self.quota.sudo()._cron_check_warn()
        self.assertEqual(activities.search_count(domain), 1)

    def test_warning_cron_runs_while_upload_quota_lock_held(self):
        self.params.set_param('woow_document_management.quota_mb', '1')
        self.params.set_param('woow_document_management.quota_warn_pct', '1')
        manager = self.env.ref('base.user_admin')
        self.env.ref('woow_document_management.group_woow_document_manager').write({
            'users': [(4, manager.id)]})
        self.env['ir.attachment'].sudo().create({
            'name': 'warning.txt', 'type': 'binary', 'res_model': 'res.partner',
            'res_id': manager.partner_id.id, 'datas': base64.b64encode(b'w' * 20_000),
        })
        with self.env.registry.cursor() as other:
            self.assertTrue(try_lock(other, 'quota'))
            self.quota.sudo()._cron_check_warn()
            self.assertTrue(self.env.ref(
                'woow_document_management.activity_type_quota_warning',
                raise_if_not_found=False))

    def test_parallel_warning_cron_uses_own_lock(self):
        self.params.set_param('woow_document_management.quota_mb', '1')
        with self.env.registry.cursor() as other:
            self.assertTrue(try_lock(other, 'quota:warn'))
            self.quota.sudo()._cron_check_warn()
            self.assertFalse(self.env.ref(
                'woow_document_management.activity_type_quota_warning',
                raise_if_not_found=False))


@tagged('woow_docmgmt_files')
class TestQuotaAnswersDoNotDependOnOtherContent(TransactionCase):
    """An upload's quota answer never tells whether some other stored file has the same bytes."""

    def setUp(self):
        super().setUp()
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.set_param('woow_document_management.quota_mb', '1')
        self.params.set_param('woow_document_management.quota_block_pct', '95')
        admin = self.env.ref('base.user_admin')
        partner = self.env['res.partner'].sudo().create({'name': 'Quota uploader'})
        self.portal = self.env['res.users'].sudo().create({
            'name': 'Quota uploader', 'login': 'wdoc-quota-uploader', 'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Quota upload folder', 'owner_id': admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner',
            'partner_id': partner.id, 'level': 'upload'})
        other_company = self.env['res.company'].sudo().create({'name': 'Quota other company'})
        stranger = self.env['res.partner'].sudo().create({
            'name': 'Quota stranger', 'company_id': other_company.id})
        self.hidden = b'hidden-content-' * 4370
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'stored.txt', 'type': 'binary', 'raw': self.hidden,
            'res_model': 'res.partner', 'res_id': stranger.id, 'company_id': other_company.id})
        self.assertTrue(attachment.store_fname)
        self.fresh = b'f' * len(self.hidden)
        self.env.flush_all()
        # The fixture's member locks (rank access) are held until the test ends;
        # forget their rank so the uploads below may take the quota lock.
        locks._reset_tracker(self.env.cr)

    def _answer(self, user, content):
        documents = self.env['woow.document'].with_user(user)
        try:
            with self.env.cr.savepoint():
                documents.create({'name': 'probe.txt', 'folder_id': self.folder.id,
                                  'datas': base64.b64encode(content)})
        except WoowUploadRejected as error:
            return error.code, str(error)
        return 'ok', ''

    def test_portal_answer_is_the_same_for_stored_and_new_content(self):
        capacity = 1024 * 1024
        quota_model = type(self.env['woow.document.quota'])
        for usage in (capacity * 95 // 100 - len(self.hidden) // 2, capacity, 1024):
            with self.subTest(usage=usage), patch.object(
                    quota_model, '_usage_bytes', return_value=usage):
                answers = {self._answer(self.portal, content)
                           for content in (self.hidden, self.fresh)}
                self.assertEqual(len(answers), 1, answers)
                code, message = answers.pop()
                self.assertEqual(code, 'ok' if usage == 1024 else 'quota')
                self.assertNotIn('%', message)

    def test_administrators_see_the_percentage_and_imports_keep_their_credit(self):
        admin = self.env.ref('base.user_admin')
        quota_model = type(self.env['woow.document.quota'])
        with patch.object(quota_model, '_usage_bytes', return_value=1024 * 1024):
            code, message = self._answer(admin, self.fresh)
        self.assertEqual(code, 'quota')
        self.assertRegex(message, r'\d+% full')
        importer = self.env['res.users'].sudo().create({
            'name': 'Quota importer', 'login': 'wdoc-quota-importer',
            'groups_id': [Command.set([
                self.env.ref('woow_document_management.group_woow_document_user').id,
                self.env.ref('woow_document_management.group_woow_document_attachment_overview').id])]})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': importer.id,
            'level': 'upload'})
        locks._reset_tracker(self.env.cr)
        source = self.env['ir.attachment'].sudo().create({
            'name': 'readable.txt', 'type': 'binary', 'raw': b'readable-content-' * 4000,
            'res_model': 'res.partner', 'res_id': importer.partner_id.id})
        self.env.flush_all()
        usage = 1024 * 1024 * 95 // 100 - 1000
        with patch.object(quota_model, '_usage_bytes', return_value=usage):
            self.assertEqual(self._answer(importer, b'readable-content-' * 4000)[0], 'quota')
            wizard = self.env['woow.document.add.attachment.wizard'].with_user(importer).with_context(
                active_model='ir.attachment', active_ids=[source.id]).create(
                    {'folder_id': self.folder.id})
            result = wizard.action_add()
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(self.env['woow.document'].sudo().search_count([
            ('folder_id', '=', self.folder.id), ('source_res_id', '=', importer.partner_id.id)]), 1)
