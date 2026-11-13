# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Cron identities and schedule wiring."""
import base64
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from ..models import cron_jobs


@tagged('woow_docmgmt_cron')
class TestDocumentCron(TransactionCase):

    def test_log_retention_takes_the_oldest_rows_first_and_does_not_count_the_rest(self):
        """A batch is the oldest rows in id order, and a full batch is the sign that more are due.

        The model order is newest first, so a batch taken in it is found only after walking
        the primary key backwards through every live row (the expired rows have the lowest
        ids), and counting the rows that are left reads the whole table (``create_date`` has
        no index), in every batch. On a busy log that keeps the weekly clean-up from keeping
        up with it.
        """
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.log_retention_days', '30')
        log = self.env['woow.document.log'].sudo()
        values = {'event': 'upload', 'result': 'ok', 'channel': 'backend'}
        expired = log.create([dict(values) for _index in range(3)]).sorted('id')
        fresh = log.create(dict(values))
        self.env.cr.execute('UPDATE woow_document_log SET create_date = %s WHERE id IN %s',
                            (fields.Datetime.now() - timedelta(days=31), tuple(expired.ids)))
        self.env.invalidate_all()
        progress = []
        cron = type(self.env['ir.cron'])

        def count_the_rest(_self, *_args, **_kwargs):
            raise AssertionError('the rows that are left were counted')

        # A plain function: the ORM collects the methods of a model that have an ``_ondelete``
        # attribute once per class, and a mock answers to every attribute name.
        with patch.object(cron, '_notify_progress',
                          lambda _self, *, done, remaining: progress.append((done, remaining))), \
                patch.object(cron_jobs, 'LOG_RETENTION_BATCH', 2), \
                patch.object(type(log), 'search_count', count_the_rest):
            self.env['woow.document'].sudo()._cron_log_retention()
            # The two oldest rows are the batch and the batch is full: another one is due.
            self.assertEqual(expired.exists(), expired[2])
            self.assertEqual(len(progress), 1)
            self.assertEqual(progress[0][0], 2)
            self.assertGreater(progress[0][1], 0)
            self.env['woow.document'].sudo()._cron_log_retention()
            self.assertFalse(expired.exists())
            self.assertEqual(progress[1], (1, 0))
            self.env['woow.document'].sudo()._cron_log_retention()
            self.assertEqual(progress[2], (0, 0))
        self.assertEqual(fresh.exists(), fresh)

    def test_scheduled_jobs_run_as_root(self):
        for xmlid in ('ir_cron_woow_document_quota',
                      'ir_cron_woow_document_purge_trash',
                      'ir_cron_woow_document_log_retention'):
            job = self.env.ref('woow_document_management.' + xmlid)
            self.assertEqual(job.user_id, self.env.ref('base.user_root'))
            self.assertEqual(job.model_id.model, 'woow.document')
        self.env['woow.document'].sudo()._cron_quota()

    def test_regular_user_cannot_run_jobs(self):
        admin = self.env.ref('base.user_admin')
        with self.assertRaises(AccessError):
            self.env['woow.document'].with_user(admin)._cron_quota()

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
