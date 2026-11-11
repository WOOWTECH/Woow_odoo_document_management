# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Cron identities and schedule wiring."""
import base64
import secrets
from datetime import timedelta
from unittest.mock import patch

from psycopg2 import errors

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from ..models import share_service
from ..services.constants import SHARE_RECORD_RETENTION_DAYS


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

    def test_share_maintenance_skips_a_link_the_database_refuses_to_delete(self):
        """One link that cannot be deleted never stops the run or undoes its other work.

        The refused delete is skipped and logged by id; the links before and after it
        are still deleted, an expired link is still marked, and the next run, with
        the database no longer refusing, deletes the skipped link.
        """
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Maintenance skip folder', 'owner_id': admin.id})
        service = self.env['woow.document.share']
        issue = service.with_user(admin)._issue
        old = fields.Datetime.now() - timedelta(days=SHARE_RECORD_RETENTION_DAYS + 1)
        links = service
        for _index in range(3):
            share, _code = issue({
                'share_type': 'folder', 'folder_id': folder.id,
                'expire_at': fields.Datetime.now() + timedelta(days=1),
                'password': secrets.token_urlsafe(16)})
            share.sudo().write({'state': 'revoked', 'revoked_at': old})
            links |= share
        middle = links.sorted('id')[1]
        lapsed, _code = issue({'share_type': 'folder', 'folder_id': folder.id,
                               'expire_at': fields.Datetime.now() + timedelta(days=1)})
        lapsed.sudo().write({'expire_at': fields.Datetime.now() - timedelta(minutes=1)})
        unlink = type(service).unlink

        def refusing_unlink(records):
            if middle.id in records.ids:
                raise errors.UniqueViolation()
            return unlink(records)

        with patch.object(type(service), 'unlink', refusing_unlink), \
                self.assertLogs(share_service.__name__, 'WARNING') as logs:
            service.sudo()._maintenance_run()
        self.assertEqual(links.exists(), middle)
        self.assertEqual(lapsed.state, 'expired')
        self.assertEqual(len(logs.records), 1)
        self.assertIn('id %d' % middle.id, logs.records[0].getMessage())
        service.sudo()._maintenance_run()
        self.assertFalse(links.exists())

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

    def test_log_retention_removes_minute_counters_older_than_two_days(self):
        now = fields.Datetime.now()
        old, recent = now - timedelta(days=3), now - timedelta(hours=1)
        counters = self.env['woow.document.denied.counter'].sudo().create([{
            'event': 'share_access', 'ip_hash': 'c' * 64, 'bucket_minute': minute,
            'count': 1} for minute in (old, recent)])
        budgets = self.env['woow.document.denied.budget'].sudo().create([{
            'bucket_minute': minute, 'count': 1} for minute in (old, recent)])
        self.env['woow.document'].sudo()._cron_log_retention()
        self.assertEqual(counters.exists(), counters[1])
        self.assertEqual(budgets.exists(), budgets[1])
