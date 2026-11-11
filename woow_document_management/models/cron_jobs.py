# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Bounded scheduled maintenance.

Every ``_cron_*`` starts by checking ``self.env.su``.
"""
from datetime import timedelta

from odoo import _, api, models
from odoo import fields
from odoo.exceptions import AccessError
from ..services.constants import LOG_RETENTION_BATCH, PURGE_BATCH


class WoowDocumentCron(models.Model):
    _inherit = 'woow.document'

    @api.model
    def _cron_purge_trash(self):
        """Daily: purge trash older than trash_retention_days, in batches.

        One run deletes at most PURGE_BATCH records, documents and folders alike,
        however large a trashed folder is; the framework runs the next batch while
        ``remaining`` is not zero. A folder whose subtree does not fit stays in the
        trash, partly deleted, and is continued by the next batch. A record that
        cannot be deleted now is skipped and the batch still commits: a hold found
        in this batch's snapshot, a lock wait that timed out, or a legal hold or
        restore committed after the snapshot (the purge root's row lock or the
        document delete then fails with serialization_failure). Skipped records
        are not counted in ``remaining`` (R2b-06).
        """
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        retention = self.env['woow.document.settings']._get_settings().trash_retention_days
        if not retention:
            return
        cutoff = fields.Datetime.to_datetime(fields.Datetime.now()) - timedelta(days=retention)
        domain = [('active', '=', False), ('trashed_date', '<', cutoff)]
        folder_domain = domain + [('subtree_legal_hold', '=', False)]
        document_domain = domain + [('legal_hold', '=', False)]
        folder_model = self.env['woow.document.folder'].with_context(active_test=False)
        documents_model = self.with_context(active_test=False)
        folders = folder_model.search(folder_domain, limit=PURGE_BATCH)
        _done, failed, deleted = folders._woow_purge_batch(PURGE_BATCH)
        if deleted < PURGE_BATCH:
            documents = documents_model.search(document_domain, limit=PURGE_BATCH - deleted)
            done_docs, failed_docs = documents.exists()._woow_purge()
            deleted += done_docs
            failed += failed_docs
        remaining = max(0, folder_model.search_count(folder_domain) +
                        documents_model.search_count(document_domain) - failed)
        self.env['ir.cron']._notify_progress(done=deleted, remaining=remaining)

    @api.model
    def _cron_share_maintenance(self):
        """Hourly: share link maintenance."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        self.env['woow.document.share']._maintenance_run()
        failed_cutoff = fields.Datetime.to_datetime(fields.Datetime.now()) - timedelta(hours=24)
        remaining = self.env['mail.mail'].search_count([
            ('model', '=', 'woow.document.share'), ('state', '=', 'exception'),
            ('auto_delete', '=', True), ('is_notification', '=', False),
            ('create_date', '<=', failed_cutoff)])
        self.env['ir.cron']._notify_progress(done=0, remaining=remaining)

    @api.model
    def _cron_log_retention(self):
        """Weekly: delete audit rows older than log_retention_days, in batches.

        Exception (decided retention rule): the successful ``share_download`` rows of a
        share that is still active, not yet expired and limited (``max_downloads`` > 0)
        are its download counter and stay until the share expires or is revoked; from
        then on they follow the ordinary retention period.
        """
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        days = self.env['woow.document.settings']._get_settings().log_retention_days
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        cutoff = now - timedelta(days=days)
        counted_by_live_share = [
            '&', '&', ('event', '=', 'share_download'), ('result', '=', 'ok'),
            ('share_id', 'any', [('state', '=', 'active'), ('expire_at', '>', now),
                                 ('max_downloads', '>', 0)])]
        domain = ['&', ('create_date', '<', cutoff), '!', *counted_by_live_share]
        rows = self.env['woow.document.log'].search(domain, limit=LOG_RETENTION_BATCH)
        done = len(rows)
        rows.unlink()
        remaining = self.env['woow.document.log'].search_count(domain)
        # Minute counters only decide requests of their own minute.
        minute_domain = [('bucket_minute', '<', now - timedelta(days=2))]
        for model in ('woow.document.denied.budget', 'woow.document.denied.counter'):
            counters = self.env[model].search(minute_domain, limit=LOG_RETENTION_BATCH)
            done += len(counters)
            counters.unlink()
            remaining += self.env[model].search_count(minute_domain)
        self.env['ir.cron']._notify_progress(done=done, remaining=remaining)

    @api.model
    def _cron_quota(self):
        """Daily: quota warning."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        self.env['woow.document.quota']._cron_check_warn()
