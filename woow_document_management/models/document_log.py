# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Append-only audit events, bounded denied-event buckets and per-source counters."""
import json
import re

from odoo import api, fields, models
from odoo.http import request
from odoo.exceptions import AccessError

from ..services.constants import CHANNELS, EVENTS, RESULTS, MAX_DENIED_ROWS_PER_MIN
from ..services.client_ip import hmac_ip, request_client_ip, truncate_ip

_MANAGER = 'woow_document_management.group_woow_document_manager'
# Denied events limited per source across all share ids (denied_rate_per_min).
SOURCE_LIMITED_EVENTS = ('share_access', 'share_unlock_failed')
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
    def _count_denied_across_shares(self, event, ip):
        """Rate-limit one IP across all share IDs without revealing link existence.

        Returns this minute's denied count of the source, read through its locked
        counter row (Repeatable Read safe, see ``woow.document.denied.counter``), or
        None when no row capacity is left for a new source this minute (answer 429).
        The row stays locked until the transaction ends, so the caller's decision
        and its own denied event cannot interleave with another request.
        """
        if event not in SOURCE_LIMITED_EVENTS or not isinstance(ip, str):
            raise ValueError('Invalid denied counter request.')
        digest = hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, ip)
        return self.env['woow.document.denied.counter']._lock_source(event, digest)

    @api.model
    def _denied_budget_exhausted(self):
        """Fail closed for public share requests after the global minute budget."""
        self.env.cr.execute(  # sql-ok: Q15
            """SELECT count >= %s FROM woow_document_denied_budget
               WHERE bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""",
            (MAX_DENIED_ROWS_PER_MIN,))
        row = self.env.cr.fetchone()
        return bool(row and row[0])

    @api.model
    def _denied_bucket_exists(self, event, ip_hash, share_id):
        """Only new audit buckets consume the global row budget."""
        self.flush_model(['event', 'result', 'ip_hash', 'share_id', 'bucket_minute'])
        self.env.cr.execute(  # sql-ok: Q16
            """SELECT 1 FROM woow_document_log
               WHERE result = 'denied' AND event = %s
                 AND COALESCE(ip_hash, '') = COALESCE(%s, '')
                 AND COALESCE(share_id, 0) = COALESCE(%s, 0)
                 AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')
               LIMIT 1""",
            (event, ip_hash, share_id))
        return self.env.cr.fetchone() is not None

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
        # Lock order: source counter row, global minute budget, event bucket.
        counters = self.env['woow.document.denied.counter']
        source = (hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, ip)
                  if event in SOURCE_LIMITED_EVENTS and isinstance(ip, str) else None)
        if source is not None and counters._lock_source(event, source) is None:
            return 0
        if (not self._denied_bucket_exists(event, ip_hash, share_id) and
                not self.env['woow.document.denied.budget']._take_slot()):
            return 0
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
        if source is not None:
            counters._add_one(event, source)
        self.invalidate_model()
        return count

    def init(self):
        super().init()
        self.env.cr.execute(  # sql-ok: Q3
            """CREATE UNIQUE INDEX IF NOT EXISTS woow_document_log_denied_bucket
               ON woow_document_log (event, (COALESCE(ip_hash, '')),
                   (COALESCE(share_id, 0)), bucket_minute)
               WHERE result = 'denied'""")


class WoowDocumentDeniedBudget(models.Model):
    _name = 'woow.document.denied.budget'
    _description = 'Bounded denied audit minute budget'
    _log_access = False

    bucket_minute = fields.Datetime(required=True, index=True)
    count = fields.Integer(required=True)

    @api.model
    def _take_slot(self):
        """Atomically reserve one of the finite denied rows (audit buckets and source
        counters) for this minute."""
        self.env.cr.execute(  # sql-ok: Q14
            """INSERT INTO woow_document_denied_budget (bucket_minute, count)
               VALUES (date_trunc('minute', now() AT TIME ZONE 'UTC'), 1)
               ON CONFLICT (bucket_minute)
               DO UPDATE SET count = LEAST(woow_document_denied_budget.count + 1, %s)
               RETURNING count""",
            (MAX_DENIED_ROWS_PER_MIN + 1,))
        return self.env.cr.fetchone()[0] <= MAX_DENIED_ROWS_PER_MIN

    def init(self):
        super().init()
        self.env.cr.execute(  # sql-ok: Q3
            """CREATE UNIQUE INDEX IF NOT EXISTS woow_document_denied_budget_minute
               ON woow_document_denied_budget (bucket_minute)""")


class WoowDocumentDeniedCounter(models.Model):
    """Per-source minute counter behind ``denied_rate_per_min``.

    Repeatable Read takes the snapshot at the first statement of a transaction. A
    request that waited for another one, or ran beside one on a different link,
    therefore cannot see that request's committed denial in a COUNT over audit rows.
    One row per (event, source, minute) makes all requests of one source meet on
    the same row: INSERT ... ON CONFLICT and SELECT ... FOR UPDATE fail with
    serialization_failure when another transaction created or changed the row
    after this snapshot, and Odoo retries the request with a fresh snapshot.
    """
    _name = 'woow.document.denied.counter'
    _description = 'Denied share requests per source'
    _log_access = False

    event = fields.Char(size=32, required=True)
    ip_hash = fields.Char(size=64, required=True)
    bucket_minute = fields.Datetime(required=True, index=True)
    count = fields.Integer(required=True)

    @api.model
    def _lock_source(self, event, ip_hash):
        """Lock the source's row of this minute and return its count.

        A new row takes one slot of the global minute budget, like a new audit
        bucket; None when no slot is left.
        """
        self.env.cr.execute(  # sql-ok: Q13
            """INSERT INTO woow_document_denied_counter (event, ip_hash, bucket_minute, count)
               VALUES (%s, %s, date_trunc('minute', now() AT TIME ZONE 'UTC'), 0)
               ON CONFLICT (event, ip_hash, bucket_minute) DO NOTHING
               RETURNING count""",
            (event, ip_hash))
        if self.env.cr.fetchone():
            if self.env['woow.document.denied.budget']._take_slot():
                return 0
            self.env.cr.execute(  # sql-ok: Q13
                """DELETE FROM woow_document_denied_counter
                   WHERE event = %s AND ip_hash = %s
                     AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""",
                (event, ip_hash))
            return None
        self.env.cr.execute(  # sql-ok: Q13
            """SELECT count FROM woow_document_denied_counter
               WHERE event = %s AND ip_hash = %s
                 AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')
               FOR UPDATE""",
            (event, ip_hash))
        row = self.env.cr.fetchone()
        return row[0] if row else None

    @api.model
    def _add_one(self, event, ip_hash):
        """Count one recorded denial on the row that ``_lock_source`` locked."""
        self.env.cr.execute(  # sql-ok: Q13
            """UPDATE woow_document_denied_counter SET count = count + 1
               WHERE event = %s AND ip_hash = %s
                 AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""",
            (event, ip_hash))
        self.invalidate_model(['count'])

    def init(self):
        super().init()
        self.env.cr.execute(  # sql-ok: Q3
            """CREATE UNIQUE INDEX IF NOT EXISTS woow_document_denied_counter_source
               ON woow_document_denied_counter (event, ip_hash, bucket_minute)""")
