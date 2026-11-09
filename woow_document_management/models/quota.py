# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Storage quota (IF-QUOTA, owned by upload-quota). Q2 and Q5 live here.

Day-0 stub, fail closed: every quota check rejects.
"""
import math

from odoo import _, fields, models
from odoo.exceptions import AccessError

from .upload_policy import WoowUploadRejected
from .settings import PARAM_PREFIX
from ..services.constants import MIB
from ..services.locks import lock
from ..services.locks import try_lock


class WoowDocumentQuota(models.AbstractModel):
    _name = 'woow.document.quota'
    _description = 'Document storage quota'

    def _usage_bytes(self) -> int:
        """Q2: deduplicated bytes of all stored attachments."""
        self.env['ir.attachment'].flush_model(['store_fname', 'file_size'])
        self.env.cr.execute('''
            SELECT COALESCE(SUM(file_size), 0)
            FROM (SELECT DISTINCT ON (store_fname) file_size FROM ir_attachment
                  WHERE store_fname IS NOT NULL) AS stored
        ''')  # sql-ok: Q2
        return int(self.env.cr.fetchone()[0] or 0)

    def _bytes_if_new(self, checksum, size) -> int:
        """Q5: 0 when a stored attachment with this checksum exists, else ``size``."""
        self.env['ir.attachment'].flush_model(['checksum', 'store_fname'])
        self.env.cr.execute('''
            SELECT 1 FROM ir_attachment
            WHERE checksum = %s AND store_fname IS NOT NULL LIMIT 1
        ''', (checksum,))  # sql-ok: Q5
        return 0 if self.env.cr.fetchone() else size

    def _check(self, checksum, size) -> None:
        """Refuse uploads above quota_block_pct under lock('quota'); LockBusy on timeout."""
        settings = self.env['woow.document.settings']._get_settings()
        if not settings.quota_mb:
            return
        self.env['ir.attachment'].flush_model(['store_fname', 'file_size', 'checksum'])
        lock(self.env.cr, 'quota')
        # A transaction-level advisory lock only orders entrants; under Repeatable Read
        # the next entrant can still read its pre-lock snapshot. Updating this existing
        # quota setting row forces a serialization retry when a concurrent upload won.
        self.env['ir.config_parameter'].flush_model(['key', 'value'])
        self.env.cr.execute(  # sql-ok: Q10
            'UPDATE ir_config_parameter SET value = value '
            'WHERE key = %s RETURNING id', (PARAM_PREFIX + 'quota_mb',))
        if not self.env.cr.fetchone():
            raise WoowUploadRejected('quota')
        projected = self._usage_bytes() + self._bytes_if_new(checksum, size)
        capacity = settings.quota_mb * MIB
        if projected * 100 > capacity * settings.quota_block_pct:
            raise WoowUploadRejected('quota', pct=math.ceil(projected * 100 / capacity))

    def _cron_check_warn(self) -> None:
        """Daily: activity for document administrators when usage reaches quota_warn_pct."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        settings = self.env['woow.document.settings']._get_settings()
        if not settings.quota_mb:
            return
        # Serialise cron runs without losing the warning when an upload holds quota.
        if not try_lock(self.env.cr, 'quota:warn'):
            return
        used = self._usage_bytes()
        capacity = settings.quota_mb * MIB
        if used * 100 < capacity * settings.quota_warn_pct:
            return
        pct = math.ceil(used * 100 / capacity)
        today = fields.Date.context_today(self)
        summary = _('Document storage capacity warning')
        model = self.env.ref('base.model_res_partner')
        activity_type = self._quota_activity_type()
        group = self.env.ref('woow_document_management.group_woow_document_manager')
        activities = self.env['mail.activity']
        for manager in group.users.filtered('active'):
            partner = manager.partner_id
            if not partner:
                continue
            exists = activities.with_context(active_test=False).search_count([
                ('res_model_id', '=', model.id), ('res_id', '=', partner.id),
                ('user_id', '=', manager.id), ('activity_type_id', '=', activity_type.id),
                ('date_deadline', '=', today), ('summary', '=', summary),
            ])
            if exists:
                continue
            activities.create({
                'res_model_id': model.id, 'res_id': partner.id,
                'user_id': manager.id, 'activity_type_id': activity_type.id,
                'date_deadline': today, 'summary': summary,
                'note': _('Document storage is %(pct)s%% full.', pct=pct),
            })

    def _quota_activity_type(self):
        """Keep completed warnings so the same-day cron can see its prior work."""
        xmlid = 'woow_document_management.activity_type_quota_warning'
        activity_type = self.env.ref(xmlid, raise_if_not_found=False)
        if activity_type:
            return activity_type
        activity_type = self.env['mail.activity.type'].create({
            'name': 'Document storage warning',
            'summary': 'Document storage capacity warning',
            'res_model': 'res.partner',
            'keep_done': True,
        })
        self.env['ir.model.data'].create({
            'module': 'woow_document_management',
            'name': 'activity_type_quota_warning',
            'model': 'mail.activity.type',
            'res_id': activity_type.id,
            'noupdate': True,
        })
        return activity_type
