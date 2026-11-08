# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Scheduled actions (IF-CRON, owned by trash-audit-cron). Day-0 stub.

Every ``_cron_*`` starts by checking ``self.env.su``.
"""
from odoo import _, api, models
from odoo.exceptions import AccessError


class WoowDocumentCron(models.Model):
    _inherit = 'woow.document'

    @api.model
    def _cron_purge_trash(self):
        """Daily: purge trash older than trash_retention_days, in batches."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        raise NotImplementedError  # WOOW-STUB

    @api.model
    def _cron_share_maintenance(self):
        """Hourly: share link maintenance."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        raise NotImplementedError  # WOOW-STUB

    @api.model
    def _cron_log_retention(self):
        """Weekly: delete audit rows older than log_retention_days, in batches."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        raise NotImplementedError  # WOOW-STUB

    @api.model
    def _cron_quota(self):
        """Daily: quota warning."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        raise NotImplementedError  # WOOW-STUB
