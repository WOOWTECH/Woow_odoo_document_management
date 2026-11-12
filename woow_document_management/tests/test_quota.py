# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Whole-site filestore accounting and quota threshold checks."""

import base64
from unittest.mock import patch

from psycopg2 import OperationalError
from psycopg2.pool import PoolError

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models import quota as quota_module
from ..models.quota import LEDGER_KEY, QuotaLedger
from ..models.upload_policy import WoowUploadRejected
from ..services import locks
from ..services.constants import MIB
from ..services.locks import LockBusy, try_lock


@tagged('woow_docmgmt_files')
class TestQuota(TransactionCase):

    def setUp(self):
        super().setUp()
        self.quota = self.env['woow.document.quota']
        self.params = self.env['ir.config_parameter'].sudo()

    def test_disabled_quota_does_not_query_storage(self):
        self.assertIsNone(self.quota._check('missing', 20 * 1024 * 1024))

    def test_existing_file_is_counted_once(self):
        content = b'quota-integration-content' * 30
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'quota.txt', 'type': 'binary',
            'res_model': 'res.partner', 'res_id': self.env.user.partner_id.id,
            'datas': base64.b64encode(content),
        })
        self.assertTrue(attachment.store_fname)
        self.assertGreaterEqual(self.quota._usage_bytes(), len(content))
        self.assertEqual(self.quota._bytes_if_new(attachment.checksum, len(content)), 0)
        self.assertEqual(self.quota._bytes_if_new('new-checksum', len(content)), len(content))

    def test_block_threshold_rejects_new_file(self):
        self.params.set_param('woow_document_management.quota_mb', '1')
        with self.assertRaises(WoowUploadRejected) as caught:
            self.quota._check('new-checksum', 1024 * 1024)
        self.assertEqual(caught.exception.code, 'quota')

    def test_warning_cron_requires_su_and_deduplicates(self):
        with self.assertRaises(AccessError):
            self.quota.with_user(self.env.ref('base.public_user'))._cron_check_warn()
        self.params.set_param('woow_document_management.quota_mb', '1')
        self.params.set_param('woow_document_management.quota_warn_pct', '1')
        group = self.env.ref('woow_document_management.group_woow_document_manager')
        manager = self.env.ref('base.user_admin')
        group.write({'users': [(4, manager.id)]})
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'warning.txt', 'type': 'binary',
            'res_model': 'res.partner', 'res_id': manager.partner_id.id,
            'datas': base64.b64encode(b'w' * 20_000),
        })
        self.assertTrue(attachment.store_fname)
        self.quota.sudo()._cron_check_warn()
        self.quota.sudo()._cron_check_warn()
        domain = [
            ('res_model', '=', 'res.partner'),
            ('res_id', '=', manager.partner_id.id),
            ('user_id', '=', manager.id),
            ('summary', '=', 'Document storage capacity warning'),
        ]
        activities = self.env['mail.activity'].sudo().with_context(active_test=False)
        self.assertEqual(activities.search_count(domain), 1)
        activity = activities.search(domain)
        self.assertTrue(activity.activity_type_id.keep_done)
        activity.action_feedback()
        self.assertFalse(activity.active)
        self.quota.sudo()._cron_check_warn()
        self.assertEqual(activities.search_count(domain), 1)

    def test_warning_cron_runs_while_upload_quota_lock_held(self):
        self.params.set_param('woow_document_management.quota_mb', '1')
        self.params.set_param('woow_document_management.quota_warn_pct', '1')
        manager = self.env.ref('base.user_admin')
        self.env.ref('woow_document_management.group_woow_document_manager').write({
            'users': [(4, manager.id)]})
        self.env['ir.attachment'].sudo().create({
            'name': 'warning.txt', 'type': 'binary', 'res_model': 'res.partner',
            'res_id': manager.partner_id.id, 'datas': base64.b64encode(b'w' * 20_000),
        })
        with self.env.registry.cursor() as other:
            self.assertTrue(try_lock(other, 'quota'))
            self.quota.sudo()._cron_check_warn()
            self.assertTrue(self.env.ref(
                'woow_document_management.activity_type_quota_warning',
                raise_if_not_found=False))

    def test_parallel_warning_cron_uses_own_lock(self):
        self.params.set_param('woow_document_management.quota_mb', '1')
        with self.env.registry.cursor() as other:
            self.assertTrue(try_lock(other, 'quota:warn'))
            self.quota.sudo()._cron_check_warn()
            self.assertFalse(self.env.ref(
                'woow_document_management.activity_type_quota_warning',
                raise_if_not_found=False))


@tagged('woow_docmgmt_files')
class TestQuotaAnswersDoNotDependOnOtherContent(TransactionCase):
    """An upload's quota answer never tells whether some other stored file has the same bytes."""

    def setUp(self):
        super().setUp()
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.set_param('woow_document_management.quota_mb', '1')
        self.params.set_param('woow_document_management.quota_block_pct', '95')
        admin = self.env.ref('base.user_admin')
        partner = self.env['res.partner'].sudo().create({'name': 'Quota uploader'})
        self.portal = self.env['res.users'].sudo().create({
            'name': 'Quota uploader', 'login': 'wdoc-quota-uploader', 'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Quota upload folder', 'owner_id': admin.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner',
            'partner_id': partner.id, 'level': 'upload'})
        other_company = self.env['res.company'].sudo().create({'name': 'Quota other company'})
        stranger = self.env['res.partner'].sudo().create({
            'name': 'Quota stranger', 'company_id': other_company.id})
        self.hidden = b'hidden-content-' * 4370
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'stored.txt', 'type': 'binary', 'raw': self.hidden,
            'res_model': 'res.partner', 'res_id': stranger.id, 'company_id': other_company.id})
        self.assertTrue(attachment.store_fname)
        self.fresh = b'f' * len(self.hidden)
        self.env.flush_all()
        # The fixture's member locks (rank access) are held until the test ends;
        # forget their rank so the uploads below may take the quota lock.
        locks._reset_tracker(self.env.cr)

    def _answer(self, user, content):
        documents = self.env['woow.document'].with_user(user)
        try:
            with self.env.cr.savepoint():
                documents.create({'name': 'probe.txt', 'folder_id': self.folder.id,
                                  'datas': base64.b64encode(content)})
        except WoowUploadRejected as error:
            return error.code, str(error)
        return 'ok', ''

    def test_portal_answer_is_the_same_for_stored_and_new_content(self):
        capacity = 1024 * 1024
        quota_model = type(self.env['woow.document.quota'])
        for usage in (capacity * 95 // 100 - len(self.hidden) // 2, capacity, 1024):
            with self.subTest(usage=usage), patch.object(
                    quota_model, '_usage_bytes', return_value=usage):
                answers = {self._answer(self.portal, content)
                           for content in (self.hidden, self.fresh)}
                self.assertEqual(len(answers), 1, answers)
                code, message = answers.pop()
                self.assertEqual(code, 'ok' if usage == 1024 else 'quota')
                self.assertNotIn('%', message)

    def test_administrators_see_the_percentage_and_imports_keep_their_credit(self):
        admin = self.env.ref('base.user_admin')
        quota_model = type(self.env['woow.document.quota'])
        with patch.object(quota_model, '_usage_bytes', return_value=1024 * 1024):
            code, message = self._answer(admin, self.fresh)
        self.assertEqual(code, 'quota')
        self.assertRegex(message, r'\d+% full')
        importer = self.env['res.users'].sudo().create({
            'name': 'Quota importer', 'login': 'wdoc-quota-importer',
            'groups_id': [Command.set([
                self.env.ref('woow_document_management.group_woow_document_user').id,
                self.env.ref('woow_document_management.group_woow_document_attachment_overview').id])]})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': importer.id,
            'level': 'upload'})
        locks._reset_tracker(self.env.cr)
        source = self.env['ir.attachment'].sudo().create({
            'name': 'readable.txt', 'type': 'binary', 'raw': b'readable-content-' * 4000,
            'res_model': 'res.partner', 'res_id': importer.partner_id.id})
        self.env.flush_all()
        usage = 1024 * 1024 * 95 // 100 - 1000
        with patch.object(quota_model, '_usage_bytes', return_value=usage):
            self.assertEqual(self._answer(importer, b'readable-content-' * 4000)[0], 'quota')
            wizard = self.env['woow.document.add.attachment.wizard'].with_user(importer).with_context(
                active_model='ir.attachment', active_ids=[source.id]).create(
                    {'folder_id': self.folder.id})
            result = wizard.action_add()
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(self.env['woow.document'].sudo().search_count([
            ('folder_id', '=', self.folder.id), ('source_res_id', '=', importer.partner_id.id)]), 1)


@tagged('woow_docmgmt_files')
class TestQuotaRuns(TransactionCase):
    """An upload measures the stored bytes once, last, and never waits behind its own account."""

    def setUp(self):
        super().setUp()
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.set_param('woow_document_management.quota_mb', '1000')
        self.quota = self.env['woow.document.quota']
        self.admin = self.env.ref('base.user_admin')
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Quota run folder', 'owner_id': self.admin.id})
        locks._reset_tracker(self.env.cr)

    def _files(self, count, prefix='run'):
        return [{'name': '%s-%d.txt' % (prefix, index), 'folder_id': self.folder.id,
                 'datas': base64.b64encode(b'%s %d' % (prefix.encode(), index))}
                for index in range(count)]

    def test_a_batch_is_validated_first_and_measures_the_stored_bytes_once(self):
        events = []
        quota_model = type(self.quota)
        policy_model = type(self.env['woow.document.upload.policy'])
        usage, validate = quota_model._usage_bytes, policy_model._validate

        def measuring(model):
            events.append('measure')
            return usage(model)

        def validating(model, name, data, **kwargs):
            events.append('validate')
            return validate(model, name, data, **kwargs)

        with patch.object(quota_model, '_usage_bytes', measuring), \
                patch.object(policy_model, '_validate', validating):
            created = self.env['woow.document'].with_user(self.admin).create(self._files(10))
        self.assertEqual(len(created), 10)
        self.assertEqual(events, ['validate'] * 10 + ['measure'])

    def test_the_quota_check_writes_no_row_that_other_transactions_could_collide_on(self):
        key = 'woow_document_management.quota_mb'
        self.env.flush_all()
        self.env.cr.execute('SELECT ctid::text FROM ir_config_parameter WHERE key = %s', (key,))
        before = self.env.cr.fetchone()
        self.env['woow.document'].with_user(self.admin).create(self._files(2, 'barrier'))
        self.env.flush_all()
        self.env.cr.execute('SELECT ctid::text FROM ir_config_parameter WHERE key = %s', (key,))
        self.assertEqual(self.env.cr.fetchone(), before)

    def test_the_stored_bytes_include_uploads_committed_after_the_snapshot_of_the_caller(self):
        """Every upload waits for the quota lock after its transaction began, so it must see
        what the uploads before it committed meanwhile: the count is read afresh."""
        self.env.cr.execute('SELECT 1')    # the snapshot of this transaction
        size = 7_000_003
        name = 'quota-committed-%s' % id(self)

        def remove():
            with self.env.registry.cursor() as cr:
                cr.execute('DELETE FROM ir_attachment WHERE name = %s', (name,))
        self.addCleanup(remove)
        before = self.quota._usage_bytes()
        with self.env.registry.cursor() as other:
            other.execute("""INSERT INTO ir_attachment (name, type, store_fname, file_size)
                             VALUES (%s, 'binary', %s, %s)""", (name, 'zz/' + name, size))
        self.assertEqual(self.quota._usage_bytes(), before + size)

    def test_one_account_waits_for_its_own_lock_and_other_accounts_do_not(self):
        other_user = self.env['res.users'].sudo().create({
            'name': 'Quota other account', 'login': 'wdoc-quota-other',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])]})
        real_lock = locks.lock

        def quick(cr, name, timeout_ms=50):
            return real_lock(cr, name, timeout_ms)

        with self.env.registry.cursor() as account, \
                patch.object(quota_module, 'lock', quick):
            self.assertTrue(try_lock(account, 'quota:u:%d' % self.admin.id))
            with mute_logger('odoo.sql_db'), self.assertRaises(LockBusy):
                self.quota.with_user(self.admin)._check_many([('checksum', 10)])
            self.quota.with_user(other_user)._check_many([('checksum', 10)])

    def test_files_of_one_run_count_the_bytes_the_run_has_already_accepted(self):
        self.params.set_param('woow_document_management.quota_mb', '1')
        capacity = 1024 * 1024
        accepted = capacity * 95 // 100 - 400_000
        run = self.quota.with_context(**{LEDGER_KEY: QuotaLedger()})
        with patch.object(type(self.quota), '_usage_bytes', return_value=accepted) as measure:
            run._check_many([('one', 300_000)])
            with self.assertRaises(WoowUploadRejected) as caught:
                run._check_many([('two', 300_000)])
            self.assertEqual(caught.exception.code, 'quota')
            run._check_many([('three', 50_000)])
            self.assertEqual(measure.call_count, 1)
            # A call that shares no ledger measures for itself and knows only what is stored.
            self.quota._check_many([('four', 300_000)])
            self.assertEqual(measure.call_count, 2)

    def _holds_the_quota_lock(self):
        """Whether this transaction holds the site's quota lock right now."""
        self.env.cr.execute("""SELECT 1 FROM pg_locks WHERE pid = pg_backend_pid()
                               AND locktype = 'advisory' AND granted AND objsubid = 1
                               AND ((classid::bigint << 32) | objid::bigint) = %s""",
                            (locks.advisory_key('quota'),))
        return self.env.cr.fetchone() is not None

    def test_a_refused_first_call_does_not_keep_a_measurement_that_no_lock_protects(self):
        """The measurement of a run is kept only while the run still holds the quota lock.

        The first call of a run takes the lock and measures. When it is refused its caller rolls
        it back to a savepoint, which releases the lock (the import wizard does this for every
        source), and other uploads may commit before the next call. That call has to measure
        again, after taking the lock: with the old measurement it would accept bytes that no
        longer fit, and the block limit could be passed by whatever others committed meanwhile.
        """
        self.params.set_param('woow_document_management.quota_mb', '1000')
        self.params.set_param('woow_document_management.quota_block_pct', '95')
        capacity = 1000 * MIB
        limit = capacity * 95 // 100
        committed = 100_000_000
        run = self.quota.with_context(**{LEDGER_KEY: QuotaLedger()})
        used = self.quota._usage_bytes()
        self.assertFalse(self._holds_the_quota_lock())
        with self.assertRaises(WoowUploadRejected):
            with self.env.cr.savepoint():
                run._check_many([('big', capacity)])
        self.assertFalse(self._holds_the_quota_lock(), 'the rolled back call kept the lock')
        name = 'quota-after-refusal-%s' % id(self)

        def remove():
            with self.env.registry.cursor() as cr:
                cr.execute('DELETE FROM ir_attachment WHERE name = %s', (name,))
        self.addCleanup(remove)
        with self.env.registry.cursor() as other:
            other.execute("""INSERT INTO ir_attachment (name, type, store_fname, file_size)
                             VALUES (%s, 'binary', %s, %s)""", (name, 'zz/' + name, committed))
        # It fits the bytes that were measured first, but not the ones stored now.
        size = limit - used - committed // 5
        self.assertLessEqual((used + size) * 100, capacity * 95)
        self.assertGreater((used + committed + size) * 100, capacity * 95)
        with self.assertRaises(WoowUploadRejected) as caught:
            run._check_many([('next', size)])
        self.assertEqual(caught.exception.code, 'quota')

    def test_a_missing_connection_for_the_measurement_asks_to_try_again(self):
        for error in (PoolError('the pool is full'), OperationalError('too many connections')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(self.env.registry, 'cursor', side_effect=error):
                with self.assertRaises(LockBusy):
                    self.quota._usage_bytes()
