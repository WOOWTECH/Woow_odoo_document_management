# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Whole-site filestore accounting and quota threshold checks."""

import base64

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from ..models.upload_policy import WoowUploadRejected
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
