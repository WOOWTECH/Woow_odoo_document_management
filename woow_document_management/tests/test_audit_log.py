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


def _minute(cr):
    """UTC minute of the cursor's transaction, as the denied buckets use it."""
    return cr.now().replace(second=0, microsecond=0)


def _budget(env):
    """This transaction's minute capacity row: the count of reserved slots, or 0."""
    env.cr.execute("""SELECT count FROM woow_document_denied_budget
                      WHERE bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""")
    row = env.cr.fetchone()
    return row[0] if row else 0


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
        budget = self.env['woow.document.denied.budget'].sudo().search([])
        self.assertEqual(len(budget), 1)
        self.assertEqual(budget.count, 6)

    def test_one_ipv6_network_counts_as_one_source(self):
        """Addresses of one /64 share one counter row and one bucket per link."""
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
        self.assertEqual(log._count_denied_across_shares('share_access', ip), 2)
        self.assertEqual(log._count_denied_across_shares('share_access', '203.0.113.43'), 0)
        # The source counter row always equals the sum of the source's audit buckets.
        digest = hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, ip)
        counter = self.env['woow.document.denied.counter'].sudo().search([
            ('event', '=', 'share_access'), ('ip_hash', '=', digest)])
        self.assertEqual(counter.count, 2)
        self.assertEqual(sum(log.sudo().search([
            ('event', '=', 'share_access'), ('ip_hash', '=', digest)]).mapped('count')), 2)
        self.assertEqual(log._count_denied_across_shares('share_unlock_failed', ip), 0)


@tagged('woow_docmgmt_core')
class TestDeniedConcurrency(TransactionCase):
    """Real Repeatable Read transactions of their own (no registry test mode)."""

    def _commit_links(self, count):
        """Share rows that other real transactions can reference; removed after the test."""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            ids = env['woow.document.share'].create([{
                'share_type': 'documents', 'company_id': env.company.id,
                'token_hash': secrets.token_hex(32),
                'expire_at': fields.Datetime.now() + timedelta(days=1),
            } for _index in range(count)]).ids
        self.addCleanup(self._drop_committed_rows, ids)
        return ids

    def _drop_committed_rows(self, share_ids):
        with self.registry.cursor() as cr:
            cr.execute('DELETE FROM woow_document_log WHERE share_id IN %s', (tuple(share_ids),))
            cr.execute('DELETE FROM woow_document_log WHERE ip_hash = ANY(%s)',
                       (list(self._committed_sources),))
            cr.execute('DELETE FROM woow_document_denied_counter WHERE ip_hash = ANY(%s)',
                       (list(self._committed_sources),))
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
        self._committed_sources = set()
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
                    check.execute(
                        'SELECT share_id, count FROM woow_document_log '
                        'WHERE event = %s AND ip_hash = %s ORDER BY share_id',
                        ('share_access', self._source(ip)))
                    self.assertEqual(check.fetchall(), [(first, limit)])
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
        self._committed_sources = set()
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
        self._committed_sources = set()
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
                check.execute('SELECT share_id, count FROM woow_document_log '
                              'WHERE ip_hash = ANY(%s) ORDER BY share_id',
                              ([self._source(ip_a), self._source(ip_b)],))
                self.assertEqual(check.fetchall(), [(first, 1), (second, 1)])
            return
        self.fail('no try stayed within one minute')
