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
from ..services.client_ip import hmac_ip


def _minute(cr):
    """UTC minute of the cursor's transaction, as the denied buckets use it."""
    return cr.now().replace(second=0, microsecond=0)


@tagged('woow_docmgmt_core')
class TestAuditLog(TransactionCase):

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
            self.assertTrue(log._denied_budget_exhausted())
            # A new source takes one slot for its counter row and one for its bucket.
            self.assertEqual(written, [1, 1] + [0] * 10)
            self.assertIsNone(log._count_denied_across_shares('share_access', '198.51.100.99'))
            self.assertEqual(log._count_denied_across_shares('share_access', '198.51.100.1'), 1)
        rows = log.search([('event', '=', 'share_access'), ('result', '=', 'denied')])
        self.assertEqual(len(rows), 2)
        # The third source got its counter row before the budget refused its bucket.
        self.assertEqual(sorted(counters.search(mine).mapped('count')), [0, 1, 1])
        self.assertEqual(len(rows) + len(counters.search(mine)), 5)
        budget = self.env['woow.document.denied.budget'].sudo().search([])
        self.assertEqual(len(budget), 1)
        self.assertEqual(budget.count, 6)

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
            cr.execute('DELETE FROM woow_document_denied_counter WHERE ip_hash = ANY(%s)',
                       (list(self._committed_sources),))
            cr.execute('DELETE FROM woow_document_share WHERE id IN %s', (tuple(share_ids),))

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
        k_ip = self.env['woow.document.settings']._share_keys().k_ip

        def attempt(env, ip, share_id):
            # The decision of the share controller: refuse at the limit, else record.
            log = env['woow.document.log']
            prior = log._count_denied_across_shares('share_access', ip)
            if prior is None or prior >= limit:
                return 'limited'
            log._log_event('share_access', result='denied', channel='share',
                           share_id=share_id, ip=ip)
            return 'counted'

        # The global minute budget would also serialize new buckets on its one row.
        # Patch it out: the test isolates the source counter and commits no budget row.
        with patch.object(type(self.env['woow.document.denied.budget']), '_take_slot',
                          lambda budget: True):
            for _try in range(3):
                # Keep every transaction of one try inside one minute bucket.
                self.cr.execute('SELECT EXTRACT(SECOND FROM clock_timestamp())')
                second_now = float(self.cr.fetchone()[0])
                if second_now > 50:
                    time.sleep(61 - second_now)
                ip = '2001:db8:%x::%x' % (secrets.randbelow(0xffff), secrets.randbelow(0xffff))
                self._committed_sources.add(hmac_ip(k_ip, ip))
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
                        'WHERE event = %s AND ip_hash = %s', ('share_access', hmac_ip(k_ip, ip)))
                    self.assertEqual(check.fetchall(), [(limit,)])
                    check.execute(
                        'SELECT share_id, count FROM woow_document_log '
                        'WHERE event = %s AND ip_hash = %s ORDER BY share_id',
                        ('share_access', hmac_ip(k_ip, ip)))
                    self.assertEqual(check.fetchall(), [(first, limit)])
                return
        self.fail('no try stayed within one minute')
