# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Cron identities and schedule wiring."""
import base64
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_cron')
class TestDocumentCron(TransactionCase):

    def test_share_and_quota_jobs_run_as_root(self):
        for xmlid in ('ir_cron_woow_document_share_maintenance',
                      'ir_cron_woow_document_quota',
                      'ir_cron_woow_document_purge_trash',
                      'ir_cron_woow_document_log_retention'):
            job = self.env.ref('woow_document_management.' + xmlid)
            self.assertEqual(job.user_id, self.env.ref('base.user_root'))
            self.assertEqual(job.model_id.model, 'woow.document')
        self.env['woow.document'].sudo()._cron_share_maintenance()
        self.env['woow.document'].sudo()._cron_quota()

    def test_regular_user_cannot_run_jobs(self):
        admin = self.env.ref('base.user_admin')
        with self.assertRaises(AccessError):
            self.env['woow.document'].with_user(admin)._cron_share_maintenance()

    def test_expired_trash_is_removed_but_held_content_survives(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Retention', 'owner_id': admin.id})
        document = self.env['woow.document'].sudo().create({
            'name': 'old.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'data')})
        held = self.env['woow.document'].sudo().create({
            'name': 'held.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'data')})
        document.with_user(admin).action_trash()
        held.with_user(admin).action_set_legal_hold(True)
        held.with_user(admin).action_trash()
        cutoff = fields.Datetime.now() - timedelta(days=120)
        document.sudo().write({'trashed_date': cutoff})
        held.sudo().write({'trashed_date': cutoff})
        self.env['woow.document'].sudo()._cron_purge_trash()
        self.assertFalse(document.exists())
        self.assertTrue(held.exists())
