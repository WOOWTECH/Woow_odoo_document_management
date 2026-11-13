# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Advisory lock key, ordering and transaction recovery tests."""

from odoo.tests import TransactionCase, tagged

from ..services.locks import LockBusy, _reset_tracker, advisory_key, bounded_wait, lock, try_lock


@tagged('woow_docmgmt_files')
class TestLocks(TransactionCase):

    def setUp(self):
        super().setUp()
        _reset_tracker(self.cr)

    def test_lock_key_stable_sha256(self):
        self.assertEqual(advisory_key('quota'), -6111433934557586916)
        with self.assertRaises(ValueError):
            advisory_key('quota;drop')

    def test_lock_names_are_a_closed_set(self):
        for name in ('zip:u:1', 'zip:slot:1', 'zip:slot:4', 'quota', 'quota:warn', 'quota:u:3',
                     'access:7'):
            advisory_key(name)
        for name in ('share:1', 'zip:s:1:abcd', 'zip:slot:5', 'access', 'quota:x', 'other:1'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                advisory_key(name)

    def test_lock_order_and_reentrant(self):
        lock(self.cr, 'zip:u:1')
        self.assertTrue(try_lock(self.cr, 'zip:slot:1'))
        lock(self.cr, 'quota')
        self.assertTrue(try_lock(self.cr, 'quota'))
        lock(self.cr, 'access:7')
        self.assertTrue(try_lock(self.cr, 'access:7'))
        with self.assertRaises(AssertionError):
            try_lock(self.cr, 'zip:u:2')
        with self.assertRaises(AssertionError):
            try_lock(self.cr, 'quota')

    def test_negative_quota_key_keeps_rank_before_zip(self):
        lock(self.cr, 'quota')
        with self.assertRaises(AssertionError):
            try_lock(self.cr, 'zip:u:9')

    def test_rank_released_with_savepoint(self):
        with self.assertRaises(ValueError):
            with self.cr.savepoint():
                lock(self.cr, 'quota')
                raise ValueError('rollback the quota lock')
        self.assertTrue(try_lock(self.cr, 'zip:u:8'))

    def test_timeout_keeps_transaction_usable(self):
        with self.env.registry.cursor() as other:
            self.assertTrue(try_lock(other, 'access:289'))
            with self.assertRaises(LockBusy):
                lock(self.cr, 'access:289', timeout_ms=20)
            with bounded_wait(self.cr, 20):
                self.cr.execute('SELECT 1')
                self.assertEqual(self.cr.fetchone()[0], 1)

    def test_bounded_wait_rolls_back_orm_cache_on_row_lock(self):
        partner = self.env.ref('base.public_partner')
        original = partner.comment
        with self.env.registry.cursor() as other:
            other.execute('SELECT id FROM res_partner WHERE id = %s FOR UPDATE', (partner.id,))
            with self.assertRaises(LockBusy):
                with bounded_wait(self.cr, 20):
                    partner.write({'comment': 'temporary note'})
            self.assertEqual(partner.comment, original)
