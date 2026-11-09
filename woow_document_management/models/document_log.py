# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Append-only audit events and bounded denied-event counters."""
import json
import re

from odoo import api, fields, models
from odoo.http import request
from odoo.exceptions import AccessError

from ..services.constants import CHANNELS, EVENTS, RESULTS
from ..services.client_ip import hmac_ip, request_client_ip, truncate_ip

_MANAGER = 'woow_document_management.group_woow_document_manager'
_EVENT_LABELS = {
    'view': 'Previewed', 'download': 'Downloaded', 'zip': 'Downloaded as ZIP',
    'upload': 'Uploaded', 'replace': 'File replaced', 'trash': 'Moved to trash',
    'restore': 'Restored', 'purge': 'Deleted permanently', 'move': 'Moved',
    'access_change': 'Members changed', 'legal_hold': 'Legal hold changed',
    'share_create': 'Share link created', 'share_send': 'Share link sent',
    'share_open': 'Share link opened', 'share_access': 'Share page viewed',
    'share_download': 'Downloaded through a share link',
    'share_unlock_failed': 'Wrong share password', 'share_revoke': 'Share link revoked',
    'share_expire': 'Share link expired', 'settings_change': 'Settings changed',
    'rate_limited': 'Too many failed attempts',
}


class WoowDocumentLog(models.Model):
    _name = 'woow.document.log'
    _description = 'Document audit log'
    _order = 'id desc'

    event = fields.Selection([(e, _EVENT_LABELS[e]) for e in EVENTS], required=True, index=True)
    result = fields.Selection([(r, r.capitalize()) for r in RESULTS], required=True,
                              default='ok')
    channel = fields.Selection([(c, c.capitalize()) for c in CHANNELS], required=True)
    user_id = fields.Many2one('res.users', ondelete='set null', index=True)
    partner_id = fields.Many2one('res.partner', ondelete='set null', index=True)
    share_id = fields.Many2one('woow.document.share', ondelete='set null', index=True)
    document_id = fields.Many2one('woow.document', ondelete='set null', index=True)
    folder_id = fields.Many2one('woow.document.folder', ondelete='set null', index=True)
    company_id = fields.Many2one('res.company', ondelete='set null', index=True)
    ip_display = fields.Char(size=64, groups=_MANAGER)
    ip_hash = fields.Char(size=64, index=True, groups=_MANAGER)
    user_agent = fields.Char(size=256, groups=_MANAGER)
    count = fields.Integer(default=1)
    bucket_minute = fields.Datetime()
    detail = fields.Json()

    @api.model
    def _sum_count(self, domain):
        """Aggregate audit counters in SQL under the caller's record rules."""
        rows = self.read_group(domain, ['count:sum'], [])
        return int(rows[0].get('count') or 0) if rows else 0

    @api.model
    def _count_share_unlock_failures(self, share_id, *, since, ip_hash=None):
        """Return a count for a share visible to its creator without exposing IP hashes.

        Public HTTP calls use the share creator's non-superuser environment.  The
        aggregate has no record payload and is bounded to one readable share.
        """
        if type(share_id) is not int or share_id <= 0 or self.env.su:
            raise AccessError('Share unavailable.')
        share = self.env['woow.document.share'].search([('id', '=', share_id)], limit=1)
        if not share or share.create_uid.id != self.env.uid:
            raise AccessError('Share unavailable.')
        if ip_hash is not None and (not isinstance(ip_hash, str) or
                                    not re.fullmatch(r'[0-9a-f]{64}', ip_hash)):
            raise ValueError('Invalid IP digest.')
        since = fields.Datetime.to_datetime(since)
        if since is None:
            raise ValueError('Invalid time boundary.')
        self.flush_model(['count', 'bucket_minute', 'ip_hash', 'share_id'])
        self.env.cr.execute(  # sql-ok: Q9
            """SELECT COALESCE(SUM(count), 0) FROM woow_document_log
               WHERE share_id = %s AND event = 'share_unlock_failed'
                 AND result = 'denied' AND bucket_minute >= %s
                 AND (%s IS NULL OR ip_hash = %s)""",
            (share_id, since, ip_hash, ip_hash))
        return int(self.env.cr.fetchone()[0])

    @api.model
    def _log_event(self, event, *, result='ok', channel, user_id=None, partner_id=None,
                   share_id=None, document_id=None, folder_id=None, company_id=None, ip=None,
                   user_agent=None, detail=None):
        """Write a bounded event without storing names, addresses or credentials."""
        if event not in EVENTS or result not in RESULTS or channel not in CHANNELS:
            raise ValueError('invalid audit event')
        detail = detail or {}
        if not isinstance(detail, dict) or any(
                not isinstance(key, str) or not key.isidentifier() or len(key) > 32 or
                not (type(value) in (int, bool) or
                     isinstance(value, list) and all(type(item) is int for item in value))
                for key, value in detail.items()):
            raise ValueError('audit detail must contain only numeric values')
        for value in (user_id, partner_id, share_id, document_id, folder_id, company_id):
            if value is not None and type(value) is not int:
                raise ValueError('audit identifiers must be integers')
        settings = self.env['woow.document.settings']._get_settings()
        if ip is None and request:
            ip = request_client_ip(request.httprequest, settings)
        if user_agent is None and request:
            user_agent = str(request.httprequest.user_agent)
        keys = self.env['woow.document.settings']._share_keys() if ip else None
        ip_hash = hmac_ip(keys.k_ip, ip) if keys else None
        ip_display = (truncate_ip(ip) if settings.log_ip_mode == 'truncate' else
                      ip_hash if settings.log_ip_mode == 'hash' else None)
        user_id = (None if self.env.user._is_public() else self.env.uid) if user_id is None else user_id
        company_id = self.env.company.id if company_id is None else company_id
        vals = {'event': event, 'result': result, 'channel': channel,
                'user_id': user_id, 'partner_id': partner_id, 'share_id': share_id,
                'company_id': company_id, 'ip_hash': ip_hash, 'ip_display': ip_display,
                'user_agent': str(user_agent or '')[:256], 'detail': detail}
        if result == 'ok':
            vals.update(document_id=document_id, folder_id=folder_id)
            self.sudo().create(vals)  # sudo-ok: S1
            return 1
        self.flush_model()
        self.env.cr.execute(  # sql-ok: Q1
            """INSERT INTO woow_document_log
               (create_uid, create_date, write_uid, write_date, event, result, channel,
                user_id, partner_id, share_id, company_id, ip_hash, ip_display,
                user_agent, bucket_minute, count, detail)
               VALUES (%s, now() AT TIME ZONE 'UTC', %s, now() AT TIME ZONE 'UTC',
                       %s, 'denied', %s, %s, %s, %s, %s, %s, %s, %s,
                       date_trunc('minute', now() AT TIME ZONE 'UTC'), 1, %s::jsonb)
               ON CONFLICT (event, (COALESCE(ip_hash, '')), (COALESCE(share_id, 0)), bucket_minute)
                 WHERE result = 'denied'
               DO UPDATE SET count = woow_document_log.count + 1,
                             write_date = EXCLUDED.write_date
               RETURNING count""",
            (self.env.uid, self.env.uid, event, channel, user_id, partner_id,
             share_id, company_id, ip_hash, ip_display, vals['user_agent'],
             json.dumps(detail, ensure_ascii=False, separators=(',', ':'))))
        count = self.env.cr.fetchone()[0]
        self.invalidate_model()
        return count

    def init(self):
        super().init()
        self.env.cr.execute(  # sql-ok: Q3
            """CREATE UNIQUE INDEX IF NOT EXISTS woow_document_log_denied_bucket
               ON woow_document_log (event, (COALESCE(ip_hash, '')),
                   (COALESCE(share_id, 0)), bucket_minute)
               WHERE result = 'denied'""")
