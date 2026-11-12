# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Bounded scheduled maintenance.

Every ``_cron_*`` starts by checking ``self.env.su``.
"""
import logging
from datetime import timedelta

from odoo import _, api, models
from odoo import fields
from odoo.exceptions import AccessError
from ..services.constants import LOG_RETENTION_BATCH, POISON_ALERT_DAYS, PURGE_BATCH

_logger = logging.getLogger(__name__)


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
        in this batch's snapshot, a lock wait that timed out, a legal hold,
        restore or move committed after the snapshot (the lock on the folder rows
        of the purge root's subtree, or the document delete, then fails with
        serialization_failure), or a delete the database refuses (an integrity
        error), or any other error of that record. Skipped records are not counted in
        ``remaining`` (R2b-06); a record that stays undeletable for POISON_ALERT_DAYS
        days after its retention period is reported to the document administrators.
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
        failures = []
        folders = folder_model.search(folder_domain, limit=PURGE_BATCH)
        _done, failed, deleted = folders._woow_purge_batch(PURGE_BATCH, failures=failures)
        if deleted < PURGE_BATCH:
            documents = documents_model.search(document_domain, limit=PURGE_BATCH - deleted)
            done_docs, failed_docs = documents.exists()._woow_purge(failures=failures)
            deleted += done_docs
            failed += failed_docs
        remaining = max(0, folder_model.search_count(folder_domain) +
                        documents_model.search_count(document_domain) - failed)
        self._woow_alert_stuck_trash(failures, cutoff)
        self.env['ir.cron']._notify_progress(done=deleted, remaining=remaining)

    @api.model
    def _woow_alert_stuck_trash(self, failures, cutoff):
        """Tell the document administrators about items that the purge cannot delete (R2b-06).

        The daily run tries every item whose retention period has ended, so an item that
        failed in this run and whose retention ended more than POISON_ALERT_DAYS days ago
        has failed in every run since. Each administrator gets one activity a day, which
        only counts the items (no names). The alert never fails the purge.
        """
        threshold = cutoff - timedelta(days=POISON_ALERT_DAYS)
        stuck = 0
        for model_name in ('woow.document.folder', 'woow.document'):
            ids = [record_id for name, record_id in failures if name == model_name]
            if ids:
                stuck += self.env[model_name].with_context(active_test=False).search_count(
                    [('id', 'in', ids), ('trashed_date', '<', threshold)])
        if not stuck:
            return
        today = fields.Date.context_today(self)
        summary = _('Items in the trash could not be deleted')
        note = _('%(count)s items have stayed in the trash for more than %(days)s days after '
                 'their retention period and could not be deleted. Check the trash for items '
                 'under legal hold or with an error.', count=stuck, days=POISON_ALERT_DAYS)
        partner_model = self.env.ref('base.model_res_partner')
        activity_type = self.env.ref('mail.mail_activity_data_todo')
        activities = self.env['mail.activity']
        group = self.env.ref('woow_document_management.group_woow_document_manager')
        for manager in group.users.filtered('active'):
            partner = manager.partner_id
            if not partner:
                continue
            try:
                with self.env.cr.savepoint():
                    if activities.search_count([
                            ('res_model_id', '=', partner_model.id), ('res_id', '=', partner.id),
                            ('user_id', '=', manager.id), ('date_deadline', '=', today),
                            ('summary', '=', summary)]):
                        continue
                    activities.create({
                        'res_model_id': partner_model.id, 'res_id': partner.id,
                        'user_id': manager.id, 'activity_type_id': activity_type.id,
                        'date_deadline': today, 'summary': summary, 'note': note})
            except Exception as error:  # noqa: BLE001 - an alert never fails the purge
                _logger.warning('The trash alert could not be created for a user (%s)',
                                type(error).__name__)

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

        A batch is the oldest rows, taken in ascending id order, and the run does not count
        what is left. ``create_date`` has no index and the model order is newest first, so a
        batch taken in that order is found only after walking the primary key backwards
        through every live row (the expired rows have the lowest ids), and counting the rest
        reads the whole table: both in every batch, which at the inflow that the denied
        bucket capacity permits keeps the clean-up from keeping up. A full batch is the sign
        that another one is due; a smaller one has read to the end of the table.
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
        rows = self.env['woow.document.log'].search(
            domain, order='id', limit=LOG_RETENTION_BATCH)
        done = len(rows)
        rows.unlink()
        remaining = 1 if done == LOG_RETENTION_BATCH else 0
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
