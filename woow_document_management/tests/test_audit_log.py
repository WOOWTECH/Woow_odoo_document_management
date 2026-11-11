# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import secrets
import time
from datetime import timedelta
from unittest.mock import patch

from psycopg2 import errors

from odoo import Command, SUPERUSER_ID, api, fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models import document_log as document_log_module
from ..services.client_ip import hmac_ip, source_key
from ..services.constants import SHARE_RECORD_RETENTION_DAYS


def _minute(cr):
    """UTC minute of the cursor's transaction, as the denied buckets use it."""
    return cr.now().replace(second=0, microsecond=0)


def _budget(env):
    """This transaction's minute capacity row: the count of reserved slots, or 0."""
    env.cr.execute("""SELECT count FROM woow_document_denied_budget
                      WHERE bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""")
    row = env.cr.fetchone()
    return row[0] if row else 0


def _set_budget(cr, minute, count):
    """Set the slots used in one minute (the minute's row version changes)."""
    cr.execute("""INSERT INTO woow_document_denied_budget (bucket_minute, count)
                  VALUES (%s, %s)
                  ON CONFLICT (bucket_minute) DO UPDATE SET count = EXCLUDED.count""",
               (minute, count))


def _budget_version(cr, minute):
    """Count and row version (ctid) of one minute's capacity row."""
    cr.execute('SELECT count, ctid::text FROM woow_document_denied_budget '
               'WHERE bucket_minute = %s', (minute,))
    return cr.fetchone()


@tagged('woow_docmgmt_core')
class TestAuditLog(TransactionCase):

    def setUp(self):
        super().setUp()
        # Capacity slots are reserved through registry.cursor(). In registry test mode
        # that cursor shares this test's transaction, so the reservations are visible
        # here and rolled back with the test.
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)

    def test_direct_folder_hold_change_is_audited(self):
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Held folder fixture'})
        manager = self.env['res.users'].sudo().create({
            'name': 'Hold audit manager', 'login': 'wdoc-hold-audit',
            'groups_id': [Command.set([
                self.env.ref('woow_document_management.group_woow_document_manager').id])],
        })
        folder.with_user(manager).write({'legal_hold': True})
        events = self.env['woow.document.log'].sudo().search([
            ('folder_id', '=', folder.id), ('event', '=', 'legal_hold')])
        self.assertEqual(len(events), 1)
        self.assertEqual(events.detail, {'hold': True})

    def test_member_create_change_and_delete_are_audited(self):
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Member audit fixture'})
        member = self.env['res.users'].sudo().create({
            'name': 'Member audit user', 'login': 'wdoc-member-audit',
            'groups_id': [Command.set([
                self.env.ref('woow_document_management.group_woow_document_user').id])],
        })
        line = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user',
            'user_id': member.id, 'level': 'read',
        })
        line.write({'level': 'upload'})
        line.unlink()
        events = self.env['woow.document.log'].sudo().search([
            ('folder_id', '=', folder.id), ('event', '=', 'access_change')])
        self.assertEqual(len(events), 3)

    def test_ok_events_are_append_only_for_normal_users(self):
        log = self.env['woow.document.log']
        self.assertEqual(log._log_event('upload', channel='backend', detail={'files': 1}), 1)
        event = log.search([('event', '=', 'upload')], limit=1)
        self.assertEqual(event.detail, {'files': 1})
        admin = self.env.ref('base.user_admin')
        with self.assertRaises(AccessError):
            event.with_user(admin).write({'detail': {'files': 2}})
        with self.assertRaises(AccessError):
            event.with_user(admin).unlink()

    def test_denied_bucket_merges_one_thousand_calls_even_without_ip(self):
        log = self.env['woow.document.log']
        for count in range(1, 1001):
            result = log._log_event('share_open', result='denied', channel='share',
                                    detail={'share': 1})
            self.assertEqual(result, count)
        rows = log.search([('event', '=', 'share_open'), ('result', '=', 'denied')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.count, 1000)
        self.assertEqual(self.env['woow.document.denied.budget'].sudo().search([]).count, 1)

    def test_rotating_ips_cannot_create_unbounded_denied_rows(self):
        log = self.env['woow.document.log']
        k_ip = self.env['woow.document.settings']._share_keys().k_ip
        ips = ['198.51.100.%d' % (n + 1) for n in range(12)]
        counters = self.env['woow.document.denied.counter'].sudo()
        mine = [('ip_hash', 'in', [hmac_ip(k_ip, ip) for ip in ips])]
        with patch.object(document_log_module, 'MAX_DENIED_ROWS_PER_MIN', 5):
            written = [log._log_event('share_access', result='denied', channel='share',
                                      ip=ip) for ip in ips]
            # A new source takes one slot for its counter row and one for its bucket.
            # The third source got the last slot for its counter row: its refusal is
            # counted there although no slot was left for its audit bucket.
            self.assertEqual(written, [1, 1] + [0] * 10)
            self.assertIsNone(log._count_denied_across_shares('share_access', '198.51.100.99'))
            self.assertEqual(log._count_denied_across_shares('share_access', '198.51.100.1'), 1)
            self.assertEqual(log._count_denied_across_shares('share_access', '198.51.100.3'), 1)
        rows = log.search([('event', '=', 'share_access'), ('result', '=', 'denied')])
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(counters.search(mine).mapped('count')), [1, 1, 1])
        self.assertEqual(len(rows) + len(counters.search(mine)), 5)
        # The capacity row counts the slots used: never more than the maximum.
        budget = self.env['woow.document.denied.budget'].sudo().search([])
        self.assertEqual(len(budget), 1)
        self.assertEqual(budget.count, 5)

    def test_one_ipv6_network_counts_as_one_source(self):
        """Addresses of one /64 share one counter row and one bucket of the minute."""
        log = self.env['woow.document.log']
        k_ip = self.env['woow.document.settings']._share_keys().k_ip
        network = ['2001:db8:0:7::%x' % (n + 1) for n in range(12)]
        written = [log._log_event('share_access', result='denied', channel='share', ip=ip)
                   for ip in network]
        self.assertEqual(written, list(range(1, 13)))
        digest = hmac_ip(k_ip, '2001:db8:0:7::/64')
        self.assertEqual(log._peek_denied_across_shares('share_access', network[0]), 12)
        self.assertEqual(log._peek_denied_across_shares('share_access', '2001:db8:0:7:ff::1'), 12)
        self.assertEqual(log._peek_denied_across_shares('share_access', '2001:db8:0:8::1'), 0)
        self.assertEqual(self.env['woow.document.denied.counter'].sudo().search([
            ('ip_hash', '=', digest)]).mapped('count'), [12])
        self.assertEqual(log.sudo().search([
            ('event', '=', 'share_access'), ('result', '=', 'denied')]).mapped('ip_hash'), [digest])
        # One counter row and one bucket for the whole network.
        self.assertEqual(_budget(self.env), 2)

    def test_audit_detail_rejects_strings(self):
        with self.assertRaises(ValueError):
            self.env['woow.document.log']._log_event(
                'upload', channel='backend', detail={'name': 'private.txt'})

    def test_counter_sum_uses_record_rule_aware_aggregate(self):
        log = self.env['woow.document.log']
        domain = [('event', '=', 'share_unlock_failed'), ('result', '=', 'denied')]
        self.assertEqual(log._sum_count(domain), 0)
        for expected in range(1, 4):
            self.assertEqual(log._log_event('share_unlock_failed', result='denied',
                                             channel='share'), expected)
        log._log_event('upload', channel='backend')
        self.assertEqual(log._sum_count(domain), 3)

    def test_share_denied_limit_counts_all_share_ids_for_one_ip(self):
        log = self.env['woow.document.log']
        ip = '203.0.113.42'
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Denied counter root'})
        share = self.env['woow.document.share'].sudo().create({
            'share_type': 'folder', 'folder_id': folder.id,
            'company_id': self.env.company.id, 'token_hash': 'a' * 64,
            'expire_at': '2030-01-01 00:00:00',
        })
        log._log_event('share_access', result='denied', channel='share', ip=ip)
        log._log_event('share_access', result='denied', channel='share',
                       share_id=share.id, ip=ip)
        # Refused link requests are kept per source and minute, whichever id they
        # name: one slot for the source's counter row and one for its bucket.
        self.assertEqual(_budget(self.env), 2)
        self.assertEqual(log._count_denied_across_shares('share_access', ip), 2)
        self.assertEqual(log._count_denied_across_shares('share_access', '203.0.113.43'), 0)
        # The source counter row always equals the sum of the source's audit buckets.
        digest = hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, ip)
        counter = self.env['woow.document.denied.counter'].sudo().search([
            ('event', '=', 'share_access'), ('ip_hash', '=', digest)])
        self.assertEqual(counter.count, 2)
        buckets = log.sudo().search([('event', '=', 'share_access'), ('ip_hash', '=', digest)])
        self.assertEqual([(row.share_id, row.count) for row in buckets],
                         [(self.env['woow.document.share'], 2)])
        self.assertEqual(log._count_denied_across_shares('share_unlock_failed', ip), 0)
        # A counted wrong password keeps its link: the per-link limits count it.
        log._log_event('share_unlock_failed', result='denied', channel='share',
                       share_id=share.id, ip=ip)
        self.assertEqual(log.sudo().search([
            ('event', '=', 'share_unlock_failed'), ('ip_hash', '=', digest)]).share_id, share)
        # The new source above took one slot for its counter row; wrong passwords none.
        self.assertEqual(_budget(self.env), 3)

    def test_user_agent_control_characters_are_dropped_before_writing(self):
        """PostgreSQL text cannot hold NUL: the header is cleaned on both write paths."""
        log = self.env['woow.document.log']
        agent = 'Mozilla\x00/5.0\r\n\t(\x1b[31mX11)\x7f' + 'y' * 300
        self.assertEqual(log._log_event('upload', channel='backend', user_agent=agent), 1)
        self.assertEqual(log._log_event('share_access', result='denied', channel='share',
                                        ip='198.51.100.80', user_agent=agent), 1)
        cleaned = ('Mozilla/5.0([31mX11)' + 'y' * 300)[:256]
        self.env.flush_all()
        self.env.cr.execute("""SELECT event, result, user_agent FROM woow_document_log
                               WHERE user_agent LIKE %s ORDER BY event""", ('Mozilla%',))
        self.assertEqual(self.env.cr.fetchall(), [('share_access', 'denied', cleaned),
                                                  ('upload', 'ok', cleaned)])
        self.assertEqual(len(cleaned), 256)

    def test_full_minute_is_refused_without_a_write_or_second_connection(self):
        """A used-up minute is refused from the snapshot: no row version, no other cursor.

        Slots are never returned within a minute, so a minute that is full in the
        request snapshot is full. Reserving a slot then neither borrows a second
        connection nor writes or locks the minute row that every new source's
        refusal would otherwise queue on.
        """
        log = self.env['woow.document.log']
        k_ip = self.env['woow.document.settings']._share_keys().k_ip
        ips = ('198.51.100.90', '198.51.100.91', '198.51.100.92')
        mine = [('ip_hash', 'in', [hmac_ip(k_ip, ip) for ip in ips])]
        minute = _minute(self.cr)
        maximum = document_log_module.MAX_DENIED_ROWS_PER_MIN
        for used in (maximum, maximum + 1):
            _set_budget(self.cr, minute, used)
            version = _budget_version(self.cr, minute)
            with patch.object(self.registry, 'cursor', side_effect=AssertionError(
                    'a second connection was used for a full minute')) as second:
                self.assertIsNone(log._count_denied_across_shares('share_access', ips[0]))
                self.assertEqual(log._log_event('share_access', result='denied', channel='share',
                                                ip=ips[1]), 0)
                self.assertEqual(log._log_event('rate_limited', result='denied',
                                                channel='share', ip=ips[2]), 0)
                self.assertFalse(self.env['woow.document.denied.budget']._take_slot())
            second.assert_not_called()
            self.assertEqual(_budget_version(self.cr, minute), version)
            self.assertFalse(self.env['woow.document.denied.counter'].sudo().search(mine))
            self.assertFalse(log.sudo().search(mine))


@tagged('woow_docmgmt_core')
class TestDeniedConcurrency(TransactionCase):
    """Real Repeatable Read transactions of their own (no registry test mode)."""

    def setUp(self):
        super().setUp()
        # Committed audit buckets and counter rows of the sources a test used are
        # removed afterwards (after its links, which are cleaned up first).
        self._committed_sources = set()
        self.addCleanup(self._drop_source_rows)

    def _drop_source_rows(self):
        if not self._committed_sources:
            return
        with self.registry.cursor() as cr:
            cr.execute('DELETE FROM woow_document_log WHERE ip_hash = ANY(%s)',
                       (list(self._committed_sources),))
            cr.execute('DELETE FROM woow_document_denied_counter WHERE ip_hash = ANY(%s)',
                       (list(self._committed_sources),))

    def _commit_links(self, count):
        """Share rows that other real transactions can reference; removed after the test."""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            ids = env['woow.document.share'].create([{
                'share_type': 'documents', 'company_id': env.company.id,
                'token_hash': secrets.token_hex(32),
                'expire_at': fields.Datetime.now() + timedelta(days=1),
            } for _index in range(count)]).ids
        self.addCleanup(self._drop_committed_links, ids)
        return ids

    def _drop_committed_links(self, share_ids):
        with self.registry.cursor() as cr:
            cr.execute('DELETE FROM woow_document_log WHERE share_id IN %s', (tuple(share_ids),))
            cr.execute('DELETE FROM woow_document_share WHERE id IN %s', (tuple(share_ids),))

    def _keep_capacity_rows(self):
        """Restore the committed minute capacity rows after the test."""
        with self.registry.cursor() as cr:
            cr.execute('SELECT bucket_minute, count FROM woow_document_denied_budget')
            before = dict(cr.fetchall())

        def restore():
            with self.registry.cursor() as cr:
                cr.execute('SELECT bucket_minute FROM woow_document_denied_budget')
                for (minute,) in cr.fetchall():
                    if minute in before:
                        cr.execute('UPDATE woow_document_denied_budget SET count = %s '
                                   'WHERE bucket_minute = %s', (before[minute], minute))
                    else:
                        cr.execute('DELETE FROM woow_document_denied_budget '
                                   'WHERE bucket_minute = %s', (minute,))
        self.addCleanup(restore)

    def _source(self, ip):
        return hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, source_key(ip))

    def _new_source(self):
        """A rate-limit source nobody used before (its own IPv6 /64)."""
        ip = '2001:db8:%x:%x::1' % (secrets.randbelow(0xffff), secrets.randbelow(0xffff))
        self._committed_sources.add(self._source(ip))
        return ip

    def _inside_one_minute(self):
        """Wait so that the next transactions of a try start in one minute bucket."""
        self.cr.execute('SELECT EXTRACT(SECOND FROM clock_timestamp())')
        second_now = float(self.cr.fetchone()[0])
        if second_now > 50:
            time.sleep(61 - second_now)

    def test_source_limit_rejects_a_stale_snapshot_from_another_link(self):
        """Two real Repeatable Read transactions of one source, on two links.

        B takes its snapshot first. A then records the source's last allowed denial
        on the other link and commits. A COUNT over audit rows in B's snapshot would
        still see the old total, so B would pass the limit as well. The locked source
        counter makes B fail with serialization_failure instead, which Odoo retries;
        the retry reads the committed count and is limited.
        """
        limit = 3
        first, second = self._commit_links(2)

        def attempt(env, ip, share_id):
            # The decision of the share controller: refuse at the limit, else record.
            log = env['woow.document.log']
            prior = log._count_denied_across_shares('share_access', ip)
            if prior is None or prior >= limit:
                return 'limited'
            log._log_event('share_access', result='denied', channel='share',
                           share_id=share_id, ip=ip)
            return 'counted'

        # Isolate the source counter: no capacity slot is reserved or committed here
        # (test_new_sources_never_conflict_on_the_capacity covers the capacity row).
        with patch.object(type(self.env['woow.document.denied.budget']), '_take_slot',
                          lambda budget: True):
            for _try in range(3):
                self._inside_one_minute()
                ip = self._new_source()
                minutes = set()
                with self.registry.cursor() as setup:
                    for _index in range(limit - 1):
                        self.assertEqual(attempt(api.Environment(setup, SUPERUSER_ID, {}),
                                                 ip, first), 'counted')
                    minutes.add(_minute(setup))
                with self.registry.cursor() as cr_b, self.registry.cursor() as cr_a:
                    minutes.add(_minute(cr_b))   # B's first statement takes its snapshot.
                    env_a = api.Environment(cr_a, SUPERUSER_ID, {})
                    env_b = api.Environment(cr_b, SUPERUSER_ID, {})
                    self.assertEqual(attempt(env_a, ip, first), 'counted')
                    minutes.add(_minute(cr_a))
                    cr_a.commit()
                    with mute_logger('odoo.sql_db'), \
                            self.assertRaises(errors.SerializationFailure):
                        attempt(env_b, ip, second)
                    cr_b.rollback()
                    retried = attempt(env_b, ip, second)
                    minutes.add(_minute(cr_b))
                if len(minutes) > 1:
                    continue   # a minute boundary fell inside this try; start a new source
                self.assertEqual(retried, 'limited')
                with self.registry.cursor() as check:
                    check.execute(
                        'SELECT count FROM woow_document_denied_counter '
                        'WHERE event = %s AND ip_hash = %s', ('share_access', self._source(ip)))
                    self.assertEqual(check.fetchall(), [(limit,)])
                    # One bucket per source and minute, whichever link was refused.
                    check.execute(
                        'SELECT share_id, count FROM woow_document_log '
                        'WHERE event = %s AND ip_hash = %s ORDER BY share_id',
                        ('share_access', self._source(ip)))
                    self.assertEqual(check.fetchall(), [(None, limit)])
                return
        self.fail('no try stayed within one minute')

    def test_source_lock_reads_a_row_changed_after_the_insert_probe(self):
        """The locked read, not INSERT ... DO NOTHING, refuses a stale count.

        B takes its snapshot and runs the INSERT ... DO NOTHING of _lock_source on
        the source's existing row, which does not lock a row its snapshot sees. Right
        after that statement A records the source's last allowed failure on another
        link and commits. Only SELECT ... FOR UPDATE then makes B fail with
        serialization_failure; a plain read would return the stale count 2.
        """
        limit = 3
        first, second = self._commit_links(2)
        event = 'share_unlock_failed'   # needs no capacity slot: nothing else is committed

        def record(env, ip, share_id):
            log = env['woow.document.log']
            prior = log._count_denied_across_shares(event, ip)
            self.assertLess(prior, limit)
            log._log_event(event, result='denied', channel='share', share_id=share_id, ip=ip)

        for _try in range(3):
            self._inside_one_minute()
            ip = self._new_source()
            minutes = set()
            with self.registry.cursor() as setup:
                for _index in range(limit - 1):
                    record(api.Environment(setup, SUPERUSER_ID, {}), ip, first)
                minutes.add(_minute(setup))
            with self.registry.cursor() as cr_b, self.registry.cursor() as cr_a:
                minutes.add(_minute(cr_b))   # B's first statement takes its snapshot.
                env_a = api.Environment(cr_a, SUPERUSER_ID, {})
                env_b = api.Environment(cr_b, SUPERUSER_ID, {})
                execute = cr_b.execute
                probes = []

                def execute_then_let_a_commit(query, params=None, log_exceptions=True):
                    result = execute(query, params, log_exceptions)
                    if not probes and 'INSERT INTO woow_document_denied_counter' in str(query):
                        probes.append(cr_b.rowcount)   # 0: DO NOTHING on a row B can see
                        record(env_a, ip, first)
                        minutes.add(_minute(cr_a))
                        cr_a.commit()
                    return result

                with patch.object(cr_b, 'execute', execute_then_let_a_commit), \
                        mute_logger('odoo.sql_db'), \
                        self.assertRaises(errors.SerializationFailure):
                    env_b['woow.document.log']._count_denied_across_shares(event, ip)
                self.assertEqual(probes, [0])
                cr_b.rollback()
                retried = env_b['woow.document.log']._count_denied_across_shares(event, ip)
                minutes.add(_minute(cr_b))
                cr_b.rollback()
            if len(minutes) > 1:
                continue   # a minute boundary fell inside this try; start a new source
            self.assertEqual(retried, limit)
            return
        self.fail('no try stayed within one minute')

    def test_new_sources_never_conflict_on_the_capacity(self):
        """Two new sources on two links reserve capacity without serialization failure.

        B takes its snapshot first; A records a refusal of another new source on the
        other link and commits. Both reserve slots of the same minute (a counter row
        and a bucket each), in short transactions of their own, so B's refusal is
        recorded at once instead of failing on a shared minute row.
        """
        first, second = self._commit_links(2)
        self._keep_capacity_rows()
        for _try in range(3):
            self._inside_one_minute()
            ip_a, ip_b = self._new_source(), self._new_source()
            with self.registry.cursor() as cr_b, self.registry.cursor() as cr_a:
                cr_b.execute('SELECT 1')   # B's first statement takes its snapshot.
                env_a = api.Environment(cr_a, SUPERUSER_ID, {})
                env_b = api.Environment(cr_b, SUPERUSER_ID, {})
                before = _budget(env_b)
                self.assertEqual(env_a['woow.document.log']._log_event(
                    'share_access', result='denied', channel='share',
                    share_id=first, ip=ip_a), 1)
                minutes = {_minute(cr_a), _minute(cr_b)}
                cr_a.commit()
                self.assertEqual(env_b['woow.document.log']._log_event(
                    'share_access', result='denied', channel='share',
                    share_id=second, ip=ip_b), 1)
                cr_b.commit()
            if len(minutes) > 1:
                continue   # a minute boundary fell inside this try; use new sources
            with self.registry.cursor() as check:
                check.execute('SELECT count FROM woow_document_denied_budget '
                              'WHERE bucket_minute = %s', (minutes.pop(),))
                self.assertEqual(check.fetchone()[0], before + 4)
                check.execute('SELECT ip_hash, share_id, count FROM woow_document_log '
                              'WHERE ip_hash = ANY(%s) ORDER BY ip_hash',
                              ([self._source(ip_a), self._source(ip_b)],))
                self.assertEqual(check.fetchall(), sorted(
                    [(self._source(ip_a), None, 1), (self._source(ip_b), None, 1)]))
            return
        self.fail('no try stayed within one minute')

    def test_user_agent_with_nul_never_loses_a_refusal(self):
        """A refusal whose User-Agent holds NUL is recorded like any other one.

        A new source's slots are committed in their own transactions before its rows
        are written. Written as is, NUL would make the audit write fail and roll the
        request back: its counter row and bucket would be lost while both slots stay
        used, so such refusals would use up the capacity without being counted. The
        cleaned header is written and the refusal commits with two slots.
        """
        self._keep_capacity_rows()
        ip = self._new_source()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            minute = _minute(cr)
            before = _budget(env)
            self.assertEqual(env['woow.document.log']._log_event(
                'share_access', result='denied', channel='share', ip=ip,
                user_agent='fixture\x00agent\r\n'), 1)
        with self.registry.cursor() as check:
            check.execute('SELECT count FROM woow_document_denied_counter '
                          'WHERE event = %s AND ip_hash = %s', ('share_access', self._source(ip)))
            self.assertEqual(check.fetchall(), [(1,)])
            check.execute("SELECT count, user_agent FROM woow_document_log "
                          "WHERE result = 'denied' AND ip_hash = %s", (self._source(ip),))
            self.assertEqual(check.fetchall(), [(1, 'fixtureagent')])
            self.assertEqual(_budget_version(check, minute)[0], before + 2)

    def test_full_minute_row_is_never_rewritten_from_a_stale_snapshot(self):
        """A snapshot that still shows room gets no slot once the minute is full.

        B takes its snapshot while the minute has room; another transaction then
        uses the last slot and commits. B's snapshot still shows room, so B asks its
        own short transaction for a slot: the minute row is full there, and B gets
        none while the row keeps its count and its row version.
        """
        self._keep_capacity_rows()
        maximum = document_log_module.MAX_DENIED_ROWS_PER_MIN
        for _try in range(3):
            self._inside_one_minute()
            with self.registry.cursor() as setup:
                minute = _minute(setup)
                _set_budget(setup, minute, maximum - 1)
            with self.registry.cursor() as cr_b:
                if _minute(cr_b) != minute:   # B's first statement takes its snapshot.
                    continue
                env_b = api.Environment(cr_b, SUPERUSER_ID, {})
                with self.registry.cursor() as other:
                    _set_budget(other, minute, maximum)
                with self.registry.cursor() as check:
                    full = _budget_version(check, minute)
                self.assertEqual(_budget(env_b), maximum - 1)
                self.assertFalse(env_b['woow.document.denied.budget']._take_slot())
                cr_b.rollback()
            with self.registry.cursor() as check:
                self.assertEqual(_budget_version(check, minute), full)
                self.assertEqual(full[0], maximum)
            return
        self.fail('no try stayed within one minute')


@tagged('woow_docmgmt_core')
class TestFailureBucketsOfDeletedLinks(TransactionCase):
    """Buckets of counted wrong passwords never merge when their links are deleted.

    Deleting a link clears ``share_id`` of its audit rows. The buckets of one source
    and minute on two links differ only by that link, so a bucket key built on
    ``share_id`` would make them equal once both links are gone, and the unique index
    would refuse the delete of the second link (and with it the maintenance job and a
    purge of the folder that holds the links).
    """

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.log = self.env['woow.document.log']
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Failure bucket folder', 'owner_id': self.admin.id})
        self.keys = self.env['woow.document.settings']._share_keys()

    def _link(self):
        share, _code = self.env['woow.document.share'].with_user(self.admin)._issue({
            'share_type': 'folder', 'folder_id': self.folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
            'password': secrets.token_urlsafe(16)})
        return share

    def _wrong_password(self, share, ip):
        """What share_unlock records for a counted wrong password."""
        return self.log._log_event('share_unlock_failed', result='denied', channel='share',
                                   share_id=share.id, ip=ip)

    def _rows(self, ip):
        """The source's wrong password buckets as {link id: (live link id, count)}."""
        self.env.flush_all()
        self.env.invalidate_all()
        digest = hmac_ip(self.keys.k_ip, source_key(ip))
        rows = self.log.sudo().search([('event', '=', 'share_unlock_failed'),
                                       ('ip_hash', '=', digest)])
        self.assertEqual(len(rows), len(set(rows.mapped('link_ref'))))
        return {row.link_ref: (row.share_id.id, row.count) for row in rows}

    def _fixture(self, ip):
        """Three links with buckets of one source in one minute, and a link that stays."""
        shares = self._link() | self._link() | self._link()
        for share in shares:
            self.assertEqual(self._wrong_password(share, ip), 1)
        self.assertEqual(self._wrong_password(shares[0], ip), 2)
        bystander = self._link()
        self.assertEqual(self._wrong_password(bystander, ip), 1)
        self.log._log_event('upload', channel='backend', detail={'files': 1})
        return shares, bystander

    def _assert_buckets_kept(self, ip, shares, bystander, upload_rows):
        self.assertFalse(shares.exists())
        self.assertTrue(bystander.exists())
        self.assertEqual(self._rows(ip), {
            shares[0].id: (False, 2), shares[1].id: (False, 1), shares[2].id: (False, 1),
            bystander.id: (bystander.id, 1)})
        digest = hmac_ip(self.keys.k_ip, source_key(ip))
        counter = self.env['woow.document.denied.counter'].sudo().search([
            ('event', '=', 'share_unlock_failed'), ('ip_hash', '=', digest)])
        self.assertEqual(counter.count, 5)
        self.assertEqual(self.log.sudo().search([('event', '=', 'upload')]), upload_rows)

    def test_deleting_links_that_share_a_failure_minute_keeps_their_buckets(self):
        orders = {
            'in order': lambda shares: [share.unlink() for share in shares],
            'reversed': lambda shares: [share.unlink() for share in shares[::-1]],
            'in one call': lambda shares: shares.unlink(),
        }
        for number, (name, delete) in enumerate(orders.items()):
            with self.subTest(order=name):
                ip = '198.51.100.%d' % (170 + number)
                shares, bystander = self._fixture(ip)
                upload_rows = self.log.sudo().search([('event', '=', 'upload')])
                with self.cr.savepoint():
                    delete(shares.sudo())
                self._assert_buckets_kept(ip, shares, bystander, upload_rows)

    def test_maintenance_deletes_old_links_that_share_a_failure_minute(self):
        ip = '198.51.100.180'
        shares, bystander = self._fixture(ip)
        upload_rows = self.log.sudo().search([('event', '=', 'upload')])
        old = fields.Datetime.now() - timedelta(days=SHARE_RECORD_RETENTION_DAYS + 1)
        shares[0].sudo().write({'state': 'revoked', 'revoked_at': old})
        shares[1].sudo().write({'state': 'expired', 'expire_at': old})
        shares[2].sudo().write({'state': 'revoked', 'revoked_at': old})
        self.env['woow.document.share'].sudo()._maintenance_run()
        self._assert_buckets_kept(ip, shares, bystander, upload_rows)

    def test_module_update_gives_earlier_denied_rows_their_link_key(self):
        """Rows an earlier version wrote keep their link in ``link_ref`` and stay unique.

        An earlier version keyed the buckets on ``share_id`` and had no ``link_ref``.
        The update copies the link of every such denied row before the new index is
        built, drops the old index, and a second update leaves everything as it is.
        """
        cr = self.env.cr
        ip = '198.51.100.190'
        digest = hmac_ip(self.keys.k_ip, source_key(ip))
        shares = self._link() | self._link()
        cr.execute('DROP INDEX woow_document_log_denied_link_bucket')
        cr.execute("""CREATE UNIQUE INDEX woow_document_log_denied_bucket
                      ON woow_document_log (event, (COALESCE(ip_hash, '')),
                          (COALESCE(share_id, 0)), bucket_minute)
                      WHERE result = 'denied'""")
        for share in shares:
            for event in ('share_access', 'share_unlock_failed'):
                cr.execute("""INSERT INTO woow_document_log
                                  (create_uid, create_date, write_uid, write_date, event, result,
                                   channel, share_id, ip_hash, bucket_minute, count, detail)
                              VALUES (1, now(), 1, now(), %s, 'denied', 'share', %s, %s,
                                      date_trunc('minute', now() AT TIME ZONE 'UTC'), 1, '{}'::jsonb)""",
                           (event, share.id, digest))
        self.env.invalidate_all()
        self.log.init()
        cr.execute("SELECT to_regclass('woow_document_log_denied_bucket') IS NULL, "
                   "to_regclass('woow_document_log_denied_link_bucket') IS NOT NULL")
        self.assertEqual(cr.fetchone(), (True, True))
        cr.execute("""SELECT event, share_id, link_ref FROM woow_document_log
                      WHERE ip_hash = %s ORDER BY event, share_id""", (digest,))
        expected = [(event, share, share) for event in ('share_access', 'share_unlock_failed')
                    for share in shares.ids]
        self.assertEqual(cr.fetchall(), expected)
        self.log.init()
        shares.sudo().unlink()
        cr.execute("""SELECT event, share_id, link_ref FROM woow_document_log
                      WHERE ip_hash = %s ORDER BY event, link_ref""", (digest,))
        self.assertEqual(cr.fetchall(), [(event, None, share) for event in (
            'share_access', 'share_unlock_failed') for share in shares.ids])


@tagged('woow_docmgmt_core')
class TestMemberChangeAudit(TransactionCase):
    """Member changes are recorded once per folder, naming only that folder's lines."""

    def test_each_folder_row_names_only_its_own_lines(self):
        admin = self.env.ref('base.user_admin')
        group = self.env.ref('woow_document_management.group_woow_document_user')
        users = self.env['res.users'].sudo().create([{
            'name': 'Audit member %d' % index, 'login': 'wdoc-audit-member-%d' % index,
            'groups_id': [Command.set([group.id])]} for index in range(2)])
        folders = self.env['woow.document.folder'].sudo().create([{
            'name': 'Audit folder %d' % index, 'owner_id': admin.id} for index in range(3)])
        lines = self.env['woow.document.folder.access'].sudo().create([{
            'folder_id': folder.id, 'subject_type': 'user', 'user_id': user.id,
            'level': 'read'} for folder in folders for user in users])
        log = self.env['woow.document.log'].sudo()

        def rows():
            return {row.folder_id.id: row.detail['member_ids'] for row in log.search([
                ('event', '=', 'access_change'), ('folder_id', 'in', folders.ids),
                ('id', '>', mark)])}

        expected = {folder.id: sorted(lines.filtered(lambda line: line.folder_id == folder).ids)
                    for folder in folders}
        mark = max(log.search([]).ids or [0])
        lines.with_user(admin).write({'level': 'upload'})
        self.assertEqual(rows(), expected)
        mark = max(log.search([]).ids or [0])
        moved = lines.filtered(lambda line: line.folder_id == folders[0])
        target = self.env['woow.document.folder'].sudo().create({
            'name': 'Audit target', 'owner_id': admin.id})
        moved.with_user(admin).write({'folder_id': target.id})
        self.assertEqual(log.search([('event', '=', 'access_change'), ('id', '>', mark)]).mapped(
            lambda row: (row.folder_id.id, row.detail['member_ids'])),
            [(target.id, sorted(moved.ids)), (folders[0].id, sorted(moved.ids))])
        mark = max(log.search([]).ids or [0])
        remaining = lines - moved
        remaining.with_user(admin).unlink()
        self.assertEqual(rows(), {folder.id: expected[folder.id] for folder in folders[1:]})

    def test_audit_id_lists_are_bounded(self):
        log = self.env['woow.document.log']
        log._log_event('access_change', channel='backend', detail={'member_ids': list(range(500))})
        with self.assertRaises(ValueError):
            log._log_event('access_change', channel='backend',
                           detail={'member_ids': list(range(501))})
