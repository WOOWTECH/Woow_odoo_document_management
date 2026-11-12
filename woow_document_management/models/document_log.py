# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Append-only audit events, bounded denied-event buckets and per-source counters."""
import contextlib
import json
import re
import threading
from itertools import islice

from psycopg2 import OperationalError
from psycopg2.pool import PoolError

from odoo import api, fields, models
from odoo.http import request
from odoo.exceptions import AccessError
from odoo.tools import config

from ..services.constants import CHANNELS, EVENTS, RESULTS, MAX_DENIED_ROWS_PER_MIN
from ..services.client_ip import hmac_ip, request_client_ip, source_key, truncate_ip

_MANAGER = 'woow_document_management.group_woow_document_manager'
# Denied events limited per source across all share ids (denied_rate_per_min).
SOURCE_LIMITED_EVENTS = ('share_access', 'share_unlock_failed')
# A wrong password can only be sent with a valid link and session grant, and each
# link counts at most SHARE_UNLOCK_FAIL_PER_HOUR of them, so its records are bounded
# by the links and never use the per-minute capacity (MAX_DENIED_ROWS_PER_MIN).
# Only these denied events keep their share in the bucket key (as ``link_ref``, see
# there); every other denied event is kept per (event, source, minute), whichever share
# it names.
UNBUDGETED_EVENTS = ('share_unlock_failed',)
# Refused requests that anybody can send (a link or an id that is not valid, no grant) are
# counted per source and recorded without a lock or a row version that another request of
# the source must wait for, or fail on (``_refuse``): every write is one statement in a
# short transaction of its own. A wrong password needs a valid link and grant, and its
# comparison is serialized through the link and the source (share_unlock), so it keeps
# the counter row that a request locks.
ATOMIC_EVENTS = ('share_access',)
USER_AGENT_SIZE = 256
# A slot is reserved over a second database connection that the request borrows while
# it keeps its own. A threaded server without a thread limit can run more requests than
# the pool has connections, so reservations are limited per process: at most
# SLOT_CONNECTIONS (a thirty-second of the pool, never fewer than one) are open at the
# same time, and the connections left are the ones the requests themselves still need.
# A reservation that waits more than SLOT_WAIT seconds for its turn finds no slot, like
# one that finds the pool full. Prefork workers serve one request at a time and never
# wait here.
SLOT_CONNECTIONS = max(1, int(config['db_maxconn']) // 32)
SLOT_WAIT = 0.5
_SLOT_GATE = threading.BoundedSemaphore(SLOT_CONNECTIONS)
# Longest id list one audit row may carry: a folder has at most this many members.
DETAIL_LIST_MAX = 500
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


@contextlib.contextmanager
def _short_transaction(registry):
    """A READ COMMITTED transaction of its own that commits when the block ends.

    Yields its cursor, or None when no turn or connection can be had (``SLOT_CONNECTIONS``
    of these run at the same time in a process and each waits ``SLOT_WAIT`` seconds for its
    turn; a full connection pool or a database that refuses one more connection is the
    same). The block never runs while another one of the same request is open, so
    the turns cannot deadlock. In registry test mode the cursor shares the test transaction.

    The block must not need a row that its own request holds uncommitted (a counter, a bucket
    or a budget row): it would wait for the transaction of its own request, which the
    database cannot see as a deadlock. No flow of the module does.
    """
    if not _SLOT_GATE.acquire(timeout=SLOT_WAIT):
        yield None
        return
    try:
        try:
            side = registry.cursor()
        except (PoolError, OperationalError):
            yield None
            return
        with side as cr:
            if not registry.in_test_mode():
                cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')  # sql-ok: Q14
            yield cr
    finally:
        _SLOT_GATE.release()


class WoowDocumentLog(models.Model):
    _name = 'woow.document.log'
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'Document audit log'
    _order = 'id desc'
    # Only the server writes a row, and ``count`` is what the limits of the share links and of
    # the share mails add up: no column takes a stored default (a row is made with ``sudo()``,
    # which keeps the uid of the caller and so his defaults).
    _woow_own_default_fields = frozenset({
        'event', 'result', 'channel', 'user_id', 'partner_id', 'share_id', 'link_ref',
        'document_id', 'folder_id', 'company_id', 'ip_display', 'ip_hash', 'user_agent', 'count',
        'bucket_minute', 'detail'})

    event = fields.Selection([(e, _EVENT_LABELS[e]) for e in EVENTS], required=True, index=True)
    result = fields.Selection([(r, r.capitalize()) for r in RESULTS], required=True,
                              default='ok')
    channel = fields.Selection([(c, c.capitalize()) for c in CHANNELS], required=True)
    user_id = fields.Many2one('res.users', ondelete='set null', index=True)
    partner_id = fields.Many2one('res.partner', ondelete='set null', index=True)
    share_id = fields.Many2one('woow.document.share', ondelete='set null', index=True)
    # The link a counted wrong password named, as a plain integer. ``share_id`` is
    # cleared when its link is deleted; a bucket key that included it would then change
    # and could merge into the bucket of another deleted link (same event, source and
    # minute), which the unique index refuses, so deleting that link would fail. This
    # column is not a foreign key: it never changes after the insert, and ids are never
    # reused. Only denied buckets of UNBUDGETED_EVENTS carry it (and the denied rows of
    # earlier versions that named a link, see ``init``).
    link_ref = fields.Integer()
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
    def _source_digest(self, event, ip):
        """HMAC of the rate-limit source of ``ip`` (an IPv6 /64 counts as one)."""
        if event not in SOURCE_LIMITED_EVENTS or not isinstance(ip, str):
            raise ValueError('Invalid denied counter request.')
        return hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, source_key(ip))

    @api.model
    def _count_denied_across_shares(self, event, ip):
        """Rate-limit one source across all share IDs without revealing link existence.

        Returns this minute's denied count of the source, read through its locked
        counter row (Repeatable Read safe, see ``woow.document.denied.counter``), or
        None when a new source finds no capacity left for its row this minute
        (answer 429). The row stays locked until the transaction ends, so the
        caller's decision and its own denied event cannot interleave with another
        request of the same source.
        """
        return self.env['woow.document.denied.counter']._lock_source(
            event, self._source_digest(event, ip))

    @api.model
    def _peek_denied_across_shares(self, event, ip):
        """The source's count in the request snapshot, without locks or writes.

        A stale snapshot can only miss refusals, so a limit reached here is reached
        for certain. Before relying on a count below a limit, lock it with
        ``_count_denied_across_shares``.
        """
        return self.env['woow.document.denied.counter']._peek_source(
            event, self._source_digest(event, ip))

    @api.model
    def _release_denied_source(self, event, ip):
        """Drop the source's counter row of this minute while it counts nothing.

        ``_count_denied_across_shares`` creates a missing row with count 0 so that
        concurrent requests of one source meet on it; an attempt that ends without
        a refusal (a correct password) removes it again.
        """
        self.env['woow.document.denied.counter']._release_source(
            event, self._source_digest(event, ip))

    @api.model
    def _denied_bucket_exists(self, event, ip_hash, link_ref):
        """Only new audit buckets consume the global row budget."""
        self.flush_model(['event', 'result', 'ip_hash', 'link_ref', 'bucket_minute'])
        self.env.cr.execute(  # sql-ok: Q16
            """SELECT 1 FROM woow_document_log
               WHERE result = 'denied' AND event = %s
                 AND COALESCE(ip_hash, '') = COALESCE(%s, '')
                 AND COALESCE(link_ref, 0) = COALESCE(%s, 0)
                 AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')
               LIMIT 1""",
            (event, ip_hash, link_ref))
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
    def _event_values(self, event, *, result, channel, user_id=None, partner_id=None,
                      share_id=None, document_id=None, folder_id=None, company_id=None,
                      ip=None, user_agent=None, detail=None):
        """Check one event and return its bounded column values (see ``_log_event``)."""
        if event not in EVENTS or result not in RESULTS or channel not in CHANNELS:
            raise ValueError('invalid audit event')
        detail = detail or {}
        if not isinstance(detail, dict) or any(
                not isinstance(key, str) or not key.isidentifier() or len(key) > 32 or
                not (type(value) in (int, bool) or
                     isinstance(value, list) and len(value) <= DETAIL_LIST_MAX and
                     all(type(item) is int for item in value))
                for key, value in detail.items()):
            raise ValueError('audit detail must contain only numeric values')
        for value in (user_id, partner_id, share_id, document_id, folder_id):
            if value is not None and type(value) is not int:
                raise ValueError('audit identifiers must be integers')
        if company_id is not None and company_id is not False and type(company_id) is not int:
            raise ValueError('audit identifiers must be integers')
        settings = self.env['woow.document.settings']._get_settings()
        if ip is None and request:
            ip = request_client_ip(request.httprequest, settings)
        if user_agent is None and request:
            user_agent = str(request.httprequest.user_agent)
        # The header is client data. PostgreSQL text cannot hold NUL: written as is,
        # it would make the write fail and roll back the request with the refusal it
        # counts. Other control characters are dropped as well.
        user_agent = ''.join(islice(filter(str.isprintable, str(user_agent or '')),
                                    USER_AGENT_SIZE))
        keys = self.env['woow.document.settings']._share_keys() if ip else None
        # Refused requests are kept per rate-limit source (an IPv6 /64 is one), so
        # one network cannot spread them over a new bucket for every address.
        ip_hash = (hmac_ip(keys.k_ip, source_key(ip) if result == 'denied' else ip)
                   if keys else None)
        ip_display = (truncate_ip(ip) if settings.log_ip_mode == 'truncate' else
                      ip_hash if settings.log_ip_mode == 'hash' else None)
        user_id = (None if self.env.user._is_public() else self.env.uid) if user_id is None else user_id
        # None names the current company; False (what the empty company of a folder reads
        # as) is a record that belongs to no company, kept with an empty company so that
        # every company's administrators may read it (R-L0).
        company_id = self.env.company.id if company_id is None else (company_id or None)
        return {'event': event, 'result': result, 'channel': channel,
                'user_id': user_id, 'partner_id': partner_id, 'share_id': share_id,
                'company_id': company_id, 'ip_hash': ip_hash, 'ip_display': ip_display,
                'user_agent': user_agent, 'detail': detail, 'ip': ip}

    @api.model
    def _log_event(self, event, *, result='ok', channel, user_id=None, partner_id=None,
                   share_id=None, document_id=None, folder_id=None, company_id=None, ip=None,
                   user_agent=None, detail=None):
        """Write a bounded event without storing names, addresses or credentials.

        ``company_id`` is None for the current company, False for a record without a
        company (stored as no company), or an id.

        A denied event is merged into its (event, source, minute) bucket and returns
        the bucket's count; only an event in UNBUDGETED_EVENTS keeps its link in the
        bucket: ``share_id`` for the link's readers and ``link_ref``, which is part of
        the bucket key and stays when the link is deleted (``share_id`` is then
        cleared). A new bucket uses one slot of the minute capacity unless its event
        is in UNBUDGETED_EVENTS; without a slot nothing is written to the audit log
        and 0 is returned. A source-limited event also adds one to the source's
        counter row: a new source's row needs a slot too (0 and nothing written
        without one), an existing row always counts the event. The user agent is
        stored without control characters, cut to USER_AGENT_SIZE characters.
        """
        vals = self._event_values(
            event, result=result, channel=channel, user_id=user_id, partner_id=partner_id,
            share_id=share_id, document_id=document_id, folder_id=folder_id,
            company_id=company_id, ip=ip, user_agent=user_agent, detail=detail)
        ip = vals.pop('ip')
        if result == 'ok':
            vals.update(document_id=document_id, folder_id=folder_id)
            self.sudo().create(vals)  # sudo-ok: S1
            return 1
        if event not in UNBUDGETED_EVENTS:
            # A refusal that uses the minute capacity is kept per source, not per link.
            # With the share in the key, refusing a live link without a grant would
            # need a new bucket where an unknown id merges into the source's bucket,
            # and the capacity left would tell whether a share exists.
            vals['share_id'] = None
        # The bucket key names the link by ``link_ref`` (the same id as ``share_id``
        # while the link exists), not by the foreign key that deleting the link clears.
        vals['link_ref'] = vals['share_id']
        # Lock order: source counter row, then the event bucket. Slots of the minute
        # capacity are reserved in their own short transactions (``_take_slot``).
        counters = self.env['woow.document.denied.counter']
        source = (self._source_digest(event, ip)
                  if event in SOURCE_LIMITED_EVENTS and isinstance(ip, str) else None)
        if source is not None and counters._lock_source(event, source) is None:
            return 0
        count = 0
        if (event in UNBUDGETED_EVENTS
                or self._denied_bucket_exists(event, vals['ip_hash'], vals['link_ref'])
                or self.env['woow.document.denied.budget']._take_slot()):
            self.flush_model()
            self.env.cr.execute(  # sql-ok: Q1
                """INSERT INTO woow_document_log
                   (create_uid, create_date, write_uid, write_date, event, result, channel,
                    user_id, partner_id, share_id, link_ref, company_id, ip_hash, ip_display,
                    user_agent, bucket_minute, count, detail)
                   VALUES (%s, now() AT TIME ZONE 'UTC', %s, now() AT TIME ZONE 'UTC',
                           %s, 'denied', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           date_trunc('minute', now() AT TIME ZONE 'UTC'), 1, %s::jsonb)
                   ON CONFLICT (event, (COALESCE(ip_hash, '')), (COALESCE(link_ref, 0)), bucket_minute)
                     WHERE result = 'denied'
                   DO UPDATE SET count = woow_document_log.count + 1,
                                 write_date = EXCLUDED.write_date
                   RETURNING count""",
                (self.env.uid, self.env.uid, event, channel, vals['user_id'], vals['partner_id'],
                 vals['share_id'], vals['link_ref'], vals['company_id'], vals['ip_hash'],
                 vals['ip_display'], vals['user_agent'],
                 json.dumps(vals['detail'], ensure_ascii=False, separators=(',', ':'))))
            count = self.env.cr.fetchone()[0]
        # With no capacity left for a new bucket the refusal still counts for its
        # source, whose row already exists: the limits never depend on capacity.
        if source is not None:
            counters._add_one(event, source)
        self.invalidate_model()
        return count

    @api.model
    def _refuse(self, event, ip, *, limit, channel='share'):
        """Count one refused request of a source against ``limit`` and record it.

        For the events of ATOMIC_EVENTS. Nothing here waits for, or fails on, another
        request of the same source: the source's counter row is raised by one statement
        that only applies below the limit, and the audit bucket by one statement that
        adds to the bucket of the minute, each in a short transaction of its own that
        commits at once (a read committed statement sees what the others committed
        and never fails for it). So the answer depends on the exact count, whichever
        way requests of one source interleave, and a burst of them never has a request
        rolled back and run again. A request that is rolled back or fails later keeps
        its count: the count can only be above what was answered, never below.

        Returns 'counted' (the refusal counted: answer 404), 'limited' (the source used
        up its limit, a ``rate_limited`` event was recorded: answer 429) or 'busy' (a
        source that is new in the minute found no capacity, turn or connection for its
        counter row: answer 429, nothing was written).
        """
        if event not in ATOMIC_EVENTS:
            raise ValueError('invalid denied counter request')
        digest = self._source_digest(event, ip)
        state = self.env['woow.document.denied.counter']._count_below(event, digest, limit)
        if state is None:
            return 'busy'
        counted = state > 0
        self._bump_denied_bucket(self._event_values(
            event if counted else 'rate_limited', result='denied', channel=channel, ip=ip))
        return 'counted' if counted else 'limited'

    @api.model
    def _bump_denied_bucket(self, vals):
        """Add one to the denied bucket of an event, in a short transaction of its own.

        ``vals`` come from ``_event_values`` (a denied event of ATOMIC_EVENTS or
        ``rate_limited``: kept per source, never per link). An existing bucket of the
        minute is raised. A new one takes a slot of the minute capacity in the same
        transaction: only the request that creates the row does, and without a slot the
        row is not created, so the slots used are exactly the rows written. Returns the
        bucket's count, or 0 when nothing could be written (no slot, turn or
        connection): the refusal is answered all the same.
        """
        key = (vals['event'], vals['ip_hash'])
        bump = """UPDATE woow_document_log
                  SET count = count + 1, write_date = now() AT TIME ZONE 'UTC'
                  WHERE result = 'denied' AND event = %s
                    AND COALESCE(ip_hash, '') = COALESCE(%s, '') AND COALESCE(link_ref, 0) = 0
                    AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')
                  RETURNING count"""
        budget = self.env['woow.document.denied.budget']
        with _short_transaction(self.env.registry) as cr:
            if cr is None:
                return 0
            cr.execute(bump, key)  # sql-ok: Q23
            row = cr.fetchone()
            if row:
                return row[0]
            if budget._minute_is_full():
                return 0
            cr.execute(  # sql-ok: Q23
                """INSERT INTO woow_document_log
                   (create_uid, create_date, write_uid, write_date, event, result, channel,
                    user_id, partner_id, share_id, link_ref, company_id, ip_hash, ip_display,
                    user_agent, bucket_minute, count, detail)
                   VALUES (%s, now() AT TIME ZONE 'UTC', %s, now() AT TIME ZONE 'UTC',
                           %s, 'denied', %s, %s, %s, NULL, NULL, %s, %s, %s, %s,
                           date_trunc('minute', now() AT TIME ZONE 'UTC'), 1, %s::jsonb)
                   ON CONFLICT (event, (COALESCE(ip_hash, '')), (COALESCE(link_ref, 0)), bucket_minute)
                     WHERE result = 'denied'
                   DO NOTHING RETURNING count""",
                (self.env.uid, self.env.uid, vals['event'], vals['channel'], vals['user_id'],
                 vals['partner_id'], vals['company_id'], vals['ip_hash'], vals['ip_display'],
                 vals['user_agent'],
                 json.dumps(vals['detail'], ensure_ascii=False, separators=(',', ':'))))
            if cr.fetchone():
                if budget._reserve(cr):
                    return 1
                cr.rollback()           # no slot: the row is not created
                return 0
            # Another request created the bucket meanwhile: count on it.
            cr.execute(bump, key)  # sql-ok: Q23
            row = cr.fetchone()
            return row[0] if row else 0

    def init(self):
        """The unique key of the denied buckets, on ``link_ref`` and not on ``share_id``.

        Earlier versions keyed the buckets on ``share_id``, which deleting a link
        clears: two buckets of one source and minute on two links then became equal,
        and deleting the second link failed. Rows an earlier version wrote keep their
        link in ``link_ref`` before the new index is built, so they stay unique, and
        the old index is dropped. The old index is the marker: later calls skip the
        copy.
        """
        super().init()
        cr = self.env.cr
        cr.execute(  # sql-ok: Q3
            "SELECT to_regclass('woow_document_log_denied_bucket') IS NOT NULL")
        if cr.fetchone()[0]:
            cr.execute(  # sql-ok: Q3
                """UPDATE woow_document_log SET link_ref = share_id
                   WHERE result = 'denied' AND share_id IS NOT NULL AND link_ref IS NULL""")
        cr.execute(  # sql-ok: Q3
            """CREATE UNIQUE INDEX IF NOT EXISTS woow_document_log_denied_link_bucket
               ON woow_document_log (event, (COALESCE(ip_hash, '')),
                   (COALESCE(link_ref, 0)), bucket_minute)
               WHERE result = 'denied'""")
        cr.execute('DROP INDEX IF EXISTS woow_document_log_denied_bucket')  # sql-ok: Q3


class WoowDocumentDeniedBudget(models.Model):
    """Per-minute capacity for the records of refused share requests.

    Every new source counter row and every new denied audit bucket, except those
    of UNBUDGETED_EVENTS, reserves one of MAX_DENIED_ROWS_PER_MIN slots of its
    minute. Existing rows are updated without a slot. ``count`` is the number of
    slots reserved in the minute; it only grows and never exceeds the maximum.
    """
    _name = 'woow.document.denied.budget'
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'Bounded denied audit minute budget'
    _log_access = False
    _woow_own_default_fields = frozenset({'bucket_minute', 'count'})     # written by statements

    bucket_minute = fields.Datetime(required=True, index=True)
    count = fields.Integer(required=True)

    @api.model
    def _take_slot(self):
        """Reserve one slot of the calling transaction's minute; False when none is left.

        A minute that is full in the calling transaction's snapshot is refused at
        once, without a write, a row lock or a second connection: slots are never
        returned within a minute, so a stale snapshot can only show fewer of them.
        A used-up capacity therefore never makes refused requests queue on the
        minute row. Otherwise the reservation runs in its own short READ COMMITTED
        transaction that commits at once and only updates a row that still has
        room. Requests therefore never meet on the minute row inside their
        Repeatable Read transactions, and reserving a slot cannot fail with
        serialization_failure or hold the row while the request goes on. A slot
        reserved by a request that is later rolled back or retried stays used: the
        capacity can only be under-used, never exceeded. In registry test mode the
        cursor shares the test transaction, which serves one request at a time.

        The second connection is a convenience of the record keeping, never a
        requirement of the request, and it must not cost other requests theirs. At
        most SLOT_CONNECTIONS reservations of a process run at the same time (the
        others wait their turn, for SLOT_WAIT seconds at most), so a burst of new
        sources cannot take the connections that the requests themselves still need.
        When no turn or no connection can be had (the wait ran out, the connection
        pool is full, or the database refuses one more connection) there is no slot:
        False, which refuses a new source's first refusal of the minute with 429 and
        writes nothing, and never fails the request with a server error. Only the
        failure to obtain the connection is treated this way; an error inside the
        reservation propagates.
        """
        self.env.cr.execute(  # sql-ok: Q14
            """SELECT date_trunc('minute', now() AT TIME ZONE 'UTC'),
                      (SELECT count FROM woow_document_denied_budget
                       WHERE bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC'))""")
        minute, used = self.env.cr.fetchone()
        if used is not None and used >= MAX_DENIED_ROWS_PER_MIN:
            return False
        if not _SLOT_GATE.acquire(timeout=SLOT_WAIT):
            return False
        try:
            try:
                side = self.env.registry.cursor()
            except (PoolError, OperationalError):
                return False
            with side as cr:
                if not self.env.registry.in_test_mode():
                    cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')  # sql-ok: Q14
                cr.execute(  # sql-ok: Q14
                    """INSERT INTO woow_document_denied_budget (bucket_minute, count)
                       VALUES (%s, 1)
                       ON CONFLICT (bucket_minute)
                       DO UPDATE SET count = woow_document_denied_budget.count + 1
                         WHERE woow_document_denied_budget.count < %s
                       RETURNING count""",
                    (minute, MAX_DENIED_ROWS_PER_MIN))
                return cr.fetchone() is not None
        finally:
            _SLOT_GATE.release()

    @api.model
    def _minute_is_full(self):
        """Whether the caller's snapshot shows this minute's capacity used up.

        Slots are never returned within a minute, so a stale snapshot can only show
        fewer of them: a minute that is full here is full for certain. It saves a request
        that would be refused for lack of a slot from queueing on the minute row.
        """
        self.env.cr.execute(  # sql-ok: Q14
            """SELECT count FROM woow_document_denied_budget
               WHERE bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""")
        row = self.env.cr.fetchone()
        return row is not None and row[0] >= MAX_DENIED_ROWS_PER_MIN

    @api.model
    def _reserve(self, cr):
        """Reserve one slot of the minute of ``cr``'s transaction, on ``cr`` itself.

        The caller undoes the transaction when it finds no slot, so a slot is only ever
        kept together with the row that needed it. Returns False when the minute is
        full; the count never passes the maximum.
        """
        cr.execute(  # sql-ok: Q14
            """INSERT INTO woow_document_denied_budget (bucket_minute, count)
               VALUES (date_trunc('minute', now() AT TIME ZONE 'UTC'), 1)
               ON CONFLICT (bucket_minute)
               DO UPDATE SET count = woow_document_denied_budget.count + 1
                 WHERE woow_document_denied_budget.count < %s
               RETURNING count""", (MAX_DENIED_ROWS_PER_MIN,))
        return cr.fetchone() is not None

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
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'Denied share requests per source'
    _log_access = False
    _woow_own_default_fields = frozenset({'event', 'ip_hash', 'bucket_minute', 'count'})

    event = fields.Char(size=32, required=True)
    ip_hash = fields.Char(size=64, required=True)
    bucket_minute = fields.Datetime(required=True, index=True)
    count = fields.Integer(required=True)

    @api.model
    def _peek_source(self, event, ip_hash):
        """The source's count of this minute in the request snapshot (0 without a row)."""
        self.env.cr.execute(  # sql-ok: Q13
            """SELECT count FROM woow_document_denied_counter
               WHERE event = %s AND ip_hash = %s
                 AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""",
            (event, ip_hash))
        row = self.env.cr.fetchone()
        return row[0] if row else 0

    @api.model
    def _count_below(self, event, ip_hash, limit):
        """Count one refusal of the source against ``limit`` in a short transaction of its own.

        One statement raises the source's row of this minute by one while it is below
        ``limit``; a read committed statement waits for a concurrent change of the row
        and applies on top of it, so exactly ``limit`` refusals are counted however
        requests of the source interleave, and none fails because of another. A source
        without a row this minute inserts it with count 1 and takes a slot of the
        minute capacity in the same transaction: only the request that creates the row
        does, and without a slot the row is not created. Returns the new count; 0 when
        the source has used its limit; None when a new source finds no slot, turn or
        connection for its row (nothing is written).

        That statement matches no row both when the source has none and when its row is
        at the limit, and each statement after it sees what other requests committed
        meanwhile. A row that exists below the limit when it is read, or that another
        request created while this one was about to insert its own, was therefore
        created after the first statement: it is counted on, and only a row at the limit
        is answered with 0.
        """
        count = """UPDATE woow_document_denied_counter SET count = count + 1
                   WHERE event = %s AND ip_hash = %s AND count < %s
                     AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')
                   RETURNING count"""
        budget = self.env['woow.document.denied.budget']
        with _short_transaction(self.env.registry) as cr:
            if cr is None:
                return None
            cr.execute(count, (event, ip_hash, limit))  # sql-ok: Q22
            row = cr.fetchone()
            if row:
                return row[0]
            cr.execute(  # sql-ok: Q22
                """SELECT count FROM woow_document_denied_counter
                   WHERE event = %s AND ip_hash = %s
                     AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""",
                (event, ip_hash))
            row = cr.fetchone()
            if row is not None and row[0] >= limit:
                return 0
            if row is None:
                if budget._minute_is_full():
                    return None
                cr.execute(  # sql-ok: Q22
                    """INSERT INTO woow_document_denied_counter
                           (event, ip_hash, bucket_minute, count)
                       VALUES (%s, %s, date_trunc('minute', now() AT TIME ZONE 'UTC'), 1)
                       ON CONFLICT (event, ip_hash, bucket_minute) DO NOTHING RETURNING count""",
                    (event, ip_hash))
                if cr.fetchone():
                    if budget._reserve(cr):
                        return 1
                    cr.rollback()           # no slot: the row is not created
                    return None
            # Another request created the row after the first statement: count on it.
            cr.execute(count, (event, ip_hash, limit))  # sql-ok: Q22
            row = cr.fetchone()
            return row[0] if row else 0

    @api.model
    def _lock_source(self, event, ip_hash):
        """Lock the source's row of this minute and return its count.

        A missing row is created with count 0 in the caller's transaction, so that
        concurrent requests of the source wait for it and then fail with
        serialization_failure. For a budgeted event the new row reserves one slot of
        the minute capacity; without a slot the row is removed again and None is
        returned. An existing row is locked with SELECT ... FOR UPDATE: INSERT ...
        DO NOTHING does not lock a row that the snapshot already sees, and only the
        locked read fails when another transaction changed the row after this
        snapshot.
        """
        self.env.cr.execute(  # sql-ok: Q13
            """INSERT INTO woow_document_denied_counter (event, ip_hash, bucket_minute, count)
               VALUES (%s, %s, date_trunc('minute', now() AT TIME ZONE 'UTC'), 0)
               ON CONFLICT (event, ip_hash, bucket_minute) DO NOTHING
               RETURNING count""",
            (event, ip_hash))
        if self.env.cr.fetchone():
            if event in UNBUDGETED_EVENTS or self.env['woow.document.denied.budget']._take_slot():
                return 0
            self._release_source(event, ip_hash)
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

    @api.model
    def _release_source(self, event, ip_hash):
        """Remove the source's row of this minute while its count is still 0."""
        self.env.cr.execute(  # sql-ok: Q13
            """DELETE FROM woow_document_denied_counter
               WHERE event = %s AND ip_hash = %s AND count = 0
                 AND bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""",
            (event, ip_hash))
        self.invalidate_model()

    def init(self):
        super().init()
        self.env.cr.execute(  # sql-ok: Q3
            """CREATE UNIQUE INDEX IF NOT EXISTS woow_document_denied_counter_source
               ON woow_document_denied_counter (event, ip_hash, bucket_minute)""")
