# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Cron identities and schedule wiring."""
import base64
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_cron')
class TestDocumentCron(TransactionCase):

    def _limited_share_with_old_download(self, owner, folder, *, max_downloads, days_old,
                                         expire_in_days=60):
        """A share whose one successful download row is already older than the retention."""
        share, _code = self.env['woow.document.share'].with_user(owner)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=expire_in_days),
            'max_downloads': max_downloads})
        scope_env = share.with_user(owner).env
        scope_env['woow.document.log']._log_event(
            'share_download', channel='share', share_id=share.id,
            company_id=share.company_id.id, detail={'files': 1, 'bytes': 7})
        row = self.env['woow.document.log'].sudo().search([
            ('share_id', '=', share.id), ('event', '=', 'share_download'), ('result', '=', 'ok')])
        self.assertEqual(len(row), 1)
        self.env.cr.execute('UPDATE woow_document_log SET create_date = %s WHERE id = %s',
                            (fields.Datetime.now() - timedelta(days=days_old), row.id))
        self.env.invalidate_all()
        return share, row, scope_env

    def test_log_retention_keeps_the_download_counter_of_live_limited_shares(self):
        """retention must not reset max_downloads of an active, unexpired share."""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.log_retention_days', '30')
        group = self.env.ref('woow_document_management.group_woow_document_user')
        owner = self.env['res.users'].sudo().create({
            'name': 'Retention share owner', 'login': 'wdoc-retention-owner',
            'groups_id': [Command.set([group.id])]})
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Retention share folder', 'owner_id': owner.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user',
            'user_id': owner.id, 'level': 'manage'})
        self.env['woow.document'].sudo().create({
            'name': 'retention.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'retained')})
        limited, counted, scope_env = self._limited_share_with_old_download(
            owner, folder, max_downloads=1, days_old=31)
        unlimited, uncounted, _env = self._limited_share_with_old_download(
            owner, folder, max_downloads=0, days_old=31)
        expired, stale, _env = self._limited_share_with_old_download(
            owner, folder, max_downloads=1, days_old=31)
        expired.sudo().write({'expire_at': fields.Datetime.now() - timedelta(minutes=1)})
        self.env['woow.document.log'].sudo().create({
            'event': 'upload', 'result': 'ok', 'channel': 'backend', 'user_id': owner.id})
        plain = self.env['woow.document.log'].sudo().search([
            ('event', '=', 'upload'), ('user_id', '=', owner.id)], limit=1)
        self.env.cr.execute('UPDATE woow_document_log SET create_date = %s WHERE id = %s',
                            (fields.Datetime.now() - timedelta(days=31), plain.id))
        self.env.invalidate_all()

        def slots_left(share, env):
            # The same creator-scoped aggregate that _download_slot_available compares;
            # the HTTP test in test_share_routes.py exercises the locked path itself.
            prior = env['woow.document.log']._sum_count([
                ('share_id', '=', share.id), ('event', '=', 'share_download'),
                ('result', '=', 'ok')])
            return prior < share.max_downloads

        self.assertFalse(slots_left(limited, scope_env))

        progress = []
        cron = type(self.env['ir.cron'])
        with patch.object(cron, '_notify_progress',
                          lambda _self, *, done, remaining: progress.append((done, remaining))):
            self.env['woow.document'].sudo()._cron_log_retention()
        self.assertEqual(progress, [(3, 0)])
        self.assertTrue(counted.exists())
        self.assertFalse(uncounted.exists())
        self.assertFalse(stale.exists())
        self.assertFalse(plain.exists())
        self.env.invalidate_all()
        self.assertFalse(slots_left(limited, scope_env))
        self.assertEqual(limited.with_user(owner).download_count, 1)

        # Once the share is revoked its rows follow the ordinary retention period.
        limited.with_user(owner).action_revoke()
        self.env['woow.document'].sudo()._cron_log_retention()
        self.assertFalse(counted.exists())
        self.assertEqual(self.env['woow.document.log'].sudo().search_count([
            ('share_id', '=', limited.id), ('event', '=', 'share_download')]), 0)

    def test_log_retention_still_removes_fresh_counter_rows_once_they_age_out_after_expiry(self):
        """An expired-by-state share keeps nothing: the maintenance job and retention agree."""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.log_retention_days', '30')
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Retention expiry folder', 'owner_id': admin.id})
        share, row, _env = self._limited_share_with_old_download(
            admin, folder, max_downloads=2, days_old=31)
        share.sudo().write({'expire_at': fields.Datetime.now() - timedelta(minutes=1)})
        self.env['woow.document.share'].sudo()._maintenance_run()
        self.assertEqual(share.state, 'expired')
        self.env['woow.document'].sudo()._cron_log_retention()
        self.assertFalse(row.exists())

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
