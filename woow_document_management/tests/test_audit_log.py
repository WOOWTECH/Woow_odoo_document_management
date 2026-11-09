# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

from odoo.exceptions import AccessError
from odoo import Command
from odoo.tests import TransactionCase, tagged


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
