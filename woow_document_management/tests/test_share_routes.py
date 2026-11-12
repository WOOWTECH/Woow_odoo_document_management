# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import base64
import io
import os
import re
import secrets
import sys
import threading
import time
import zipfile
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

import requests
from psycopg2 import errors
from psycopg2.pool import PoolError

from odoo import Command, SUPERUSER_ID, api, fields, http, sql_db
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import TEST_CURSOR_COOKIE_NAME, Opener
from odoo.tools import mute_logger

from ..models import document_log as document_log_module
from ..services import tokens
from ..services.client_ip import hmac_ip, source_key
from ..services.constants import SESSION_GRANTS_KEY, SHARE_UNLOCK_FAIL_PER_HOUR
from ..services.zip_builder import ZipLimitExceeded
from ..controllers import share as share_controller
from ..controllers import streaming

CSRF_RE = re.compile(rb'name="csrf_token"[^>]*value="([^"]+)"')


def _deleted_creator_link(test, password=None):
    """A folder link whose creator was deleted afterwards, and a signed-in visitor.

    The visitor manages one subfolder of the link's folder and cannot read the other
    one, which holds the document that only the creator may read.
    """
    group = test.env.ref('woow_document_management.group_woow_document_user')
    creator = test.env['res.users'].sudo().create({
        'name': 'Vanishing creator', 'login': 'wdoc-vanishing-creator',
        'groups_id': [Command.set([group.id])]})
    visitor_vals = {
        'name': 'Signed-in visitor', 'login': 'wdoc-signed-in-visitor',
        'groups_id': [Command.set([group.id])]}
    if password:
        visitor_vals['password'] = password
    visitor = test.env['res.users'].sudo().create(visitor_vals)
    folders = test.env['woow.document.folder'].sudo()
    root = folders.create({'name': 'Vanishing creator folder', 'owner_id': creator.id})
    shared = folders.create({'name': 'Visitor managed', 'parent_id': root.id,
                             'owner_id': creator.id})
    hidden = folders.create({'name': 'Creator only', 'parent_id': root.id,
                             'owner_id': creator.id})
    test.env['woow.document.folder.access'].sudo().create({
        'folder_id': shared.id, 'subject_type': 'user', 'user_id': visitor.id,
        'level': 'manage'})
    documents = test.env['woow.document'].sudo()
    documents.create({'name': 'visitor-managed.txt', 'folder_id': shared.id,
                      'datas': base64.b64encode(b'the visitor may read this')})
    documents.create({'name': 'creator-only.txt', 'folder_id': hidden.id,
                      'datas': base64.b64encode(b'only the creator may read this')})
    share, code = test.env['woow.document.share'].with_user(creator)._issue({
        'share_type': 'folder', 'folder_id': root.id,
        'expire_at': fields.Datetime.now() + timedelta(days=1)})
    test.env.flush_all()
    creator.sudo().unlink()
    test.env.invalidate_all()
    test.assertFalse(share.sudo().create_uid)
    return share, code, visitor


@tagged('woow_docmgmt_share_scope')
class TestRefusalsOfOneSourceDoNotCollide(TransactionCase):
    """Refusals of one source that overlap are counted exactly and none fails or is retried.

    Each request takes its snapshot before any of them has written: under Repeatable Read a
    request that locks or updates a row another one changed after its snapshot fails with
    a serialization error and the framework runs it again after a pause, which keeps a
    worker busy (T-SHARE-48 shows it over real HTTP).
    """

    def _clean(self, digest):
        with self.registry.cursor() as cr:
            cr.execute('DELETE FROM woow_document_log WHERE ip_hash = %s', (digest,))
            cr.execute('DELETE FROM woow_document_denied_counter WHERE ip_hash = %s', (digest,))
            cr.execute('DELETE FROM woow_document_denied_budget')

    def test_overlapping_refusals_of_one_source_are_counted_exactly(self):
        source = '198.51.100.201'
        digest = hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, source_key(source))
        self.addCleanup(self._clean, digest)
        self.env.cr.execute('SELECT EXTRACT(SECOND FROM clock_timestamp())')
        second = self.env.cr.fetchone()[0]
        if second > 50:
            time.sleep(61 - float(second))      # keep the requests inside one minute
        controller = share_controller.WoowDocShareController()
        public = self.env.ref('base.public_user')
        requests = 13
        answers = []
        cursors = [self.registry.cursor() for _index in range(requests)]
        try:
            for cr in cursors:
                cr.execute('SELECT 1')          # every request has its snapshot already
            with patch.object(streaming, 'uniform_404', return_value='404'), \
                    patch.object(streaming, 'too_many_requests', return_value='429'):
                for cr in cursors:
                    env = api.Environment(cr, public.id, {})
                    with patch.object(share_controller, 'request', SimpleNamespace(env=env)):
                        answers.append(controller._denied(source))
                    cr.commit()                 # the end of the request
        finally:
            for cr in cursors:
                cr.close()
        limit = 10                              # denied_rate_per_min
        self.assertEqual(answers, ['404'] * limit + ['429'] * (requests - limit))
        with self.registry.cursor() as cr:
            cr.execute('SELECT event, count FROM woow_document_denied_counter '
                       'WHERE ip_hash = %s', (digest,))
            self.assertEqual(cr.fetchall(), [('share_access', limit)])
            cr.execute("SELECT event, count FROM woow_document_log WHERE ip_hash = %s "
                       "AND result = 'denied' ORDER BY event", (digest,))
            self.assertEqual(cr.fetchall(), [('rate_limited', requests - limit),
                                             ('share_access', limit)])
            cr.execute('SELECT COALESCE(SUM(count), 0) FROM woow_document_denied_budget')
            self.assertEqual(cr.fetchone()[0], 3)   # the counter row and the two buckets


@tagged('woow_docmgmt_share_scope')
class TestShareScope(TransactionCase):

    def test_old_folder_share_stays_out_of_scope_after_folder_restore(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Restored share scope', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'private.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'private')})
        service = self.env['woow.document.share'].with_user(admin)
        old_share, token = service._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        found, scope_env = service._resolve_request_access(token=token)
        self.assertEqual(found, old_share)
        self.assertTrue(found._validate_for_access(scope_env))
        self.assertEqual(found._scope_documents(scope_env).ids, [doc.id])

        self.assertTrue(folder.with_user(admin).action_trash())
        self.assertFalse(found._validate_for_access(scope_env))
        with self.assertRaises(UserError):
            folder.with_user(admin).with_context(active_test=False).write({'active': True})
        self.assertFalse(folder.active)
        self.assertFalse(folder.restored_date)
        self.assertFalse(found._validate_for_access(scope_env))

        # Odoo stores restored_date to whole seconds; create_date can retain
        # microseconds. Restoring in the same second must still invalidate a
        # link that predates the trash transition.
        restore_at = old_share.create_date.replace(microsecond=0)
        with patch.object(fields.Datetime, 'now', return_value=restore_at):
            self.assertTrue(folder.with_user(admin).with_context(active_test=False).action_restore())
        self.assertTrue(folder.active)
        self.assertTrue(folder.restored_date)
        self.assertEqual(doc.effective_restored_at, folder.restored_date)
        self.assertEqual(doc.effective_restored_at, restore_at)
        self.assertLess(doc.effective_restored_at, old_share.create_date)
        self.assertFalse(old_share.revoked_at)
        self.assertTrue(folder.with_user(admin)._woow_has_rank(4))
        self.assertFalse(found._validate_for_access(scope_env))
        self.assertFalse(found._scope_documents(scope_env))

    def test_public_scope_validation_keeps_document_search_bounded(self):
        group = self.env.ref('woow_document_management.group_woow_document_user')
        owner = self.env['res.users'].sudo().create({
            'name': 'Bounded owner', 'login': 'wdoc-bounded-owner',
            'groups_id': [Command.set([group.id])]})
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Large share fixture', 'owner_id': owner.id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user',
            'user_id': owner.id, 'level': 'manage'})
        for index in range(4):
            self.env['woow.document'].sudo().create({
                'name': 'file-%d.txt' % index, 'folder_id': folder.id,
                'datas': base64.b64encode(b'content')})
        share, _code = self.env['woow.document.share'].with_user(owner)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        scope_env = share.with_user(owner).env
        document_class = type(scope_env['woow.document'])
        original_search = document_class.search

        def bounded_search(records, domain, *args, **kwargs):
            self.assertEqual(kwargs.get('limit'), 1)
            return original_search(records, domain, *args, **kwargs)

        with patch.object(document_class, 'search', bounded_search):
            self.assertTrue(share._validate_for_access(scope_env))

    def test_folder_share_rejects_scope_before_loading_more_than_limit(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Bounded share', 'owner_id': admin.id})
        for index in range(3):
            self.env['woow.document'].sudo().create({
                'name': 'bounded-%d.txt' % index, 'folder_id': folder.id,
                'datas': base64.b64encode(b'content')})
        share, _code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        with self.assertRaises(ZipLimitExceeded):
            share._scope_documents(self.env['woow.document'].with_user(admin).env,
                                   max_candidates=2)
        self.assertEqual(len(share._scope_documents(
            self.env['woow.document'].with_user(admin).env, max_candidates=3)), 3)

    def test_link_of_a_deleted_creator_has_no_scope_for_a_signed_in_visitor(self):
        """Without its creator a link must not run in an environment without record rules.

        ``with_user`` of no user keeps the superuser environment, whose user is the
        visitor of the request: the checks would then take the visitor for the creator
        and list the documents of the link's whole scope.
        """
        share, code, visitor = _deleted_creator_link(self)
        service = self.env['woow.document.share'].with_user(visitor)
        for arguments in ({'token': code}, {'share_id': share.id}):
            with self.subTest(**arguments):
                found, scope_env = service._resolve_request_access(**arguments)
                self.assertFalse(found)
                self.assertIsNone(scope_env)
        visitor_su = share.with_user(visitor).sudo().env
        self.assertTrue(visitor_su.su)
        self.assertFalse(share.sudo()._validate_for_access(visitor_su))
        self.assertFalse(share.sudo()._validate_for_access(None))

    def test_scope_checks_the_management_rank_of_folders_with_one_search(self):
        """The queries of a share's scope must not grow with the number of its documents."""
        group = self.env.ref('woow_document_management.group_woow_document_user')
        owner = self.env['res.users'].sudo().create({
            'name': 'Rank batch owner', 'login': 'wdoc-rank-batch-owner',
            'groups_id': [Command.set([group.id])]})
        admin = self.env.ref('base.user_admin')
        folders = self.env['woow.document.folder'].sudo()
        documents = self.env['woow.document'].sudo()
        root = folders.create({'name': 'Rank batch root', 'owner_id': owner.id})
        managed = [folders.create({'name': 'Rank batch %d' % index, 'parent_id': root.id,
                                   'owner_id': owner.id}) for index in range(4)]
        # The creator only reads this folder: its documents are not in the link's scope.
        reading = folders.create({'name': 'Rank batch read only', 'parent_id': root.id,
                                  'owner_id': admin.id, 'inherit_access': False})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': reading.id, 'subject_type': 'user', 'user_id': owner.id,
            'level': 'read'})
        share, code = self.env['woow.document.share'].with_user(owner)._issue({
            'share_type': 'folder', 'folder_id': root.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        counter = {'index': 0}

        def add(per_folder):
            for folder in managed + [reading]:
                for _unused in range(per_folder):
                    counter['index'] += 1
                    documents.create({
                        'name': 'rank-%d.txt' % counter['index'], 'folder_id': folder.id,
                        'datas': base64.b64encode(b'rank batch')})

        def measure():
            self.env.flush_all()
            self.env.invalidate_all()
            found, scope_env = self.env['woow.document.share'].with_user(
                owner)._resolve_request_access(token=code)
            self.env.flush_all()
            before = self.cr.sql_log_count
            result = found._scope_documents(scope_env)
            return result, self.cr.sql_log_count - before

        add(1)
        measure()   # warm the caches that the first call fills, so that the counts compare
        few, few_queries = measure()
        add(4)
        many, many_queries = measure()
        self.assertEqual((len(few), len(many)), (4, 20))
        self.assertFalse(many.filtered(lambda doc: doc.folder_id == reading))
        self.assertEqual(many_queries, few_queries)

    def test_unlock_counter_is_creator_scoped_and_counts_only_matching_ip(self):
        group = self.env.ref('woow_document_management.group_woow_document_user')
        owner = self.env['res.users'].sudo().create({
            'name': 'Counter owner', 'login': 'wdoc-counter-owner',
            'groups_id': [Command.set([group.id])]})
        stranger = self.env['res.users'].sudo().create({
            'name': 'Counter stranger', 'login': 'wdoc-counter-stranger',
            'groups_id': [Command.set([group.id])]})
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Counter folder', 'owner_id': owner.id})
        self.env['woow.document'].sudo().create({
            'name': 'counter.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'counter')})
        share, _code = self.env['woow.document.share'].with_user(owner)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
            'password': secrets.token_urlsafe(16)})
        log = self.env['woow.document.log'].with_user(owner)
        log._log_event('share_unlock_failed', result='denied', channel='share',
                       share_id=share.id, ip='192.0.2.1')
        digest = hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, '192.0.2.1')
        # Buckets use the transaction's minute; a wide window keeps the wall clock of a
        # slow test run from passing them.
        since = fields.Datetime.now() - timedelta(hours=1)
        self.assertEqual(log._count_share_unlock_failures(share.id, since=since,
                                                          ip_hash=digest), 1)
        self.assertEqual(log._count_share_unlock_failures(share.id, since=since,
                                                          ip_hash='0' * 64), 0)
        self.assertEqual(log._count_share_unlock_failures(share.id, since=since), 1)
        with self.assertRaises(AccessError):
            self.env['woow.document.log'].with_user(stranger)._count_share_unlock_failures(
                share.id, since=since)

    def test_share_token_resolves_into_creator_scope_and_expires_with_content(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Share scope', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'private.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'only-in-scope')})
        service = self.env['woow.document.share'].with_user(admin)
        share, token = service._issue({
            'name': 'Review', 'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
        })
        self.assertNotEqual(share.token_hash, token)
        found, scope_env = self.env['woow.document.share']._resolve_request_access(token=token)
        self.assertEqual(found.id, share.id)
        self.assertFalse(scope_env.su)
        self.assertEqual(scope_env.uid, admin.id)
        self.assertTrue(found._validate_for_access(scope_env))
        self.assertIn(doc.id, found._scope_documents(scope_env).ids)
        missing, missing_env = self.env['woow.document.share']._resolve_request_access(token='x' * 43)
        self.assertFalse(missing)
        self.assertIsNone(missing_env)
        doc.sudo().write({'active': False})
        self.assertFalse(found._validate_for_access(scope_env))
        doc.sudo().write({'active': True})
        self.assertTrue(found._validate_for_access(scope_env))
        self.assertTrue(share.action_revoke())
        self.assertFalse(found._validate_for_access(scope_env))

    def _committed_link(self, **vals):
        """A share row other real transactions can see; its rows are removed afterwards."""
        admin = self.env.ref('base.user_admin')
        with self.registry.cursor() as cr:
            env = api.Environment(cr, admin.id, {}, su=True)
            share_id = env['woow.document.share'].create({
                'share_type': 'documents', 'company_id': env.company.id,
                'token_hash': secrets.token_hex(32),
                'expire_at': fields.Datetime.now() + timedelta(days=1), **vals}).id
            company_id = env.company.id

        def drop_committed_rows():
            with self.registry.cursor() as cr:
                cr.execute('DELETE FROM woow_document_log WHERE share_id = %s', (share_id,))
                cr.execute('DELETE FROM woow_document_denied_counter WHERE ip_hash = ANY(%s)',
                           (list(self._committed_sources),))
                cr.execute('DELETE FROM woow_document_share WHERE id = %s', (share_id,))
        self.addCleanup(drop_committed_rows)

        def scope(cr):
            share = api.Environment(cr, SUPERUSER_ID, {})['woow.document.share'].browse(share_id)
            return share, api.Environment(cr, admin.id, {'allowed_company_ids': [company_id]})
        return share_id, scope

    def _wrong_password(self, share, scope_env, ip):
        """What share_unlock commits for a counted wrong password, after the lock."""
        share._count_password_failure(scope_env)
        scope_env['woow.document.log']._log_event(
            'share_unlock_failed', result='denied', channel='share', share_id=share.id, ip=ip)

    def test_password_attempt_barrier_rejects_a_stale_snapshot(self):
        """Two real Repeatable Read transactions on one protected link.

        B takes its snapshot, then A takes the share lock, records a wrong password
        from another source and commits. The advisory lock alone would let B read
        the old failure count after its wait; the counted failure also wrote a new
        share row version, so B's row lock fails with serialization_failure instead
        and the retry sees A's failure before any password comparison. An attempt
        that counted nothing (a correct password) wrote no row version: a stale
        snapshot after it passes the row lock.
        """
        self._committed_sources = set()
        share_id, scope = self._committed_link()
        k_ip = self.env['woow.document.settings']._share_keys().k_ip
        sources = ['2001:db8:%x:%x::1' % (secrets.randbelow(0xffff), n) for n in (1, 2)]
        self._committed_sources.update(hmac_ip(k_ip, source_key(ip)) for ip in sources)
        since = fields.Datetime.now() - timedelta(hours=1)
        with self.registry.cursor() as cr_b, self.registry.cursor() as cr_a:
            cr_b.execute('SELECT 1')   # B's first statement takes its snapshot.
            share_a, scope_a = scope(cr_a)
            self.assertTrue(share_a._lock_password_attempts(scope_a))
            self._wrong_password(share_a, scope_a, sources[0])
            cr_a.commit()
            share_b, scope_b = scope(cr_b)
            with mute_logger('odoo.sql_db'), self.assertRaises(errors.SerializationFailure):
                share_b._lock_password_attempts(scope_b)
            cr_b.rollback()
            self.assertTrue(share_b._lock_password_attempts(scope_b))
            self.assertEqual(scope_b['woow.document.log']._count_share_unlock_failures(
                share_id, since=since), 1)
            cr_b.rollback()
        with self.registry.cursor() as cr_d, self.registry.cursor() as cr_c:
            cr_d.execute('SELECT 1')
            share_c, scope_c = scope(cr_c)
            self.assertTrue(share_c._lock_password_attempts(scope_c))
            cr_c.commit()
            share_d, scope_d = scope(cr_d)
            self.assertTrue(share_d._lock_password_attempts(scope_d))
            self.assertEqual(scope_d['woow.document.log']._count_share_unlock_failures(
                share_id, since=since), 1)
            cr_d.rollback()

    def test_owner_revoke_survives_public_attempts_that_count_nothing(self):
        """Password attempts and download attempts that count nothing write no share row.

        The owner's transaction takes its snapshot first. A password attempt that
        took the share lock and row lock but counted no failure, and a download of
        the used-up link that was refused, both commit. The owner's revoke must then
        commit: no public request made the share row change after its snapshot.
        """
        self._committed_sources = set()
        share_id, scope = self._committed_link(max_downloads=1)
        with self.registry.cursor() as cr:
            share, scope_env = scope(cr)
            share._record_download(scope_env, scope_env, '192.0.2.10', {'files': 1})
        with self.registry.cursor() as cr_o, self.registry.cursor() as cr_x:
            cr_o.execute('SELECT 1')   # the owner's snapshot
            share_x, scope_x = scope(cr_x)
            self.assertTrue(share_x._lock_password_attempts(scope_x))
            self.assertFalse(share_x._download_slot_available(scope_x))
            cr_x.commit()
            owner = scope(cr_o)[1]
            self.assertTrue(owner['woow.document.share'].browse(share_id).action_revoke())
            owner.flush_all()
            cr_o.commit()
        with self.registry.cursor() as cr:
            cr.execute('SELECT state FROM woow_document_share WHERE id = %s', (share_id,))
            self.assertEqual(cr.fetchone(), ('revoked',))

    def test_download_barrier_rejects_a_stale_snapshot(self):
        """A counted download writes a share row version that stale snapshots cannot pass."""
        self._committed_sources = set()
        share_id, scope = self._committed_link(max_downloads=1)
        with self.registry.cursor() as cr_b, self.registry.cursor() as cr_a:
            cr_b.execute('SELECT 1')   # B's first statement takes its snapshot.
            share_a, scope_a = scope(cr_a)
            self.assertTrue(share_a._download_slot_available(scope_a))
            share_a._record_download(scope_a, scope_a, '192.0.2.11', {'files': 1})
            cr_a.commit()
            share_b, scope_b = scope(cr_b)
            with mute_logger('odoo.sql_db'), self.assertRaises(errors.SerializationFailure):
                share_b._download_slot_available(scope_b)
            cr_b.rollback()
            self.assertFalse(share_b._download_slot_available(scope_b))
            cr_b.rollback()

    def test_unlock_takes_the_link_barrier_before_it_compares_a_password(self):
        """share_unlock compares no password from a snapshot older than the link's last failure.

        The link has used 49 of its 50 wrong passwords of the hour, all from a third source, so
        that no counter row of a source stands between the two requests. B, from one source,
        takes its snapshot. A, from another source, sends a wrong password and commits it as the
        50th. B's snapshot still shows 49: a comparison of B's password would be allowed, and a
        correct one would unlock the link although its hourly cap is used up. The route takes
        the lock and the row lock of the link before it compares, and a counted failure writes
        the share row, so B fails with serialization_failure with nothing compared; its retry
        sees the 50th failure and is answered 429, again without a comparison. The barrier is
        also tested on its own (test_password_attempt_barrier_rejects_a_stale_snapshot): this
        test pins that the route calls it, and that a counted failure writes the row.
        """
        self._committed_sources = set()
        password = secrets.token_urlsafe(16)
        share_id, scope = self._committed_link(
            password_hash=tokens.hash_password(secrets.token_urlsafe(16)))
        k_ip = self.env['woow.document.settings']._share_keys().k_ip
        ips = ['2001:db8:%x:%x::1' % (secrets.randbelow(0xffff), number) for number in (1, 2, 3)]
        digests = [hmac_ip(k_ip, source_key(ip)) for ip in ips]
        self._committed_sources.update(digests)
        with self.registry.cursor() as cr:
            cr.execute("""INSERT INTO woow_document_log
                              (create_uid, create_date, write_uid, write_date, event, result,
                               channel, share_id, link_ref, ip_hash, bucket_minute, count, detail)
                          VALUES (1, now() AT TIME ZONE 'UTC', 1, now() AT TIME ZONE 'UTC',
                                  'share_unlock_failed', 'denied', 'share', %s, %s, %s,
                                  date_trunc('minute', now() AT TIME ZONE 'UTC'), %s, '{}'::jsonb)""",
                       (share_id, share_id, digests[2], SHARE_UNLOCK_FAIL_PER_HOUR - 1))
        controller = share_controller.WoowDocShareController()
        public = self.env.ref('base.public_user')
        compared = []

        def compare(given, _stored):
            compared.append(given)
            return False

        def unlock(cr, ip, attempt):
            """The route up to its answer, for a request of ``ip`` that holds a grant on the link."""
            share, scope_env = scope(cr)
            access = share_controller.ShareAccess(
                share, scope_env, ip, '', {'exp': 0, 'h': '', 'pw': False})
            env = api.Environment(cr, public.id, {})
            with patch.object(share_controller, 'request', SimpleNamespace(env=env)), \
                    patch.object(controller, '_share_entry', return_value=access), \
                    patch.object(streaming, 'uniform_404', return_value='404'), \
                    patch.object(streaming, 'too_many_requests', return_value='429'), \
                    patch.object(tokens, 'check_password', side_effect=compare):
                return controller.share_unlock.original_endpoint(
                    controller, share_id, password=attempt)

        with self.registry.cursor() as cr_b, self.registry.cursor() as cr_a:
            cr_b.execute('SELECT 1')   # B's first statement takes its snapshot.
            self.assertEqual(unlock(cr_a, ips[0], 'wrong password A'), '404')
            cr_a.commit()
            with mute_logger('odoo.sql_db'), self.assertRaises(errors.SerializationFailure):
                unlock(cr_b, ips[1], 'wrong password B')
            self.assertEqual(compared, ['wrong password A'])
            cr_b.rollback()
            self.assertEqual(unlock(cr_b, ips[1], password), '429')
            self.assertEqual(compared, ['wrong password A'])
            cr_b.rollback()
        since = fields.Datetime.now() - timedelta(hours=1)
        with self.registry.cursor() as cr:
            share, scope_env = scope(cr)
            self.assertEqual(scope_env['woow.document.log']._count_share_unlock_failures(
                share_id, since=since), SHARE_UNLOCK_FAIL_PER_HOUR)

    def test_password_attempt_lock_needs_the_creator_scope(self):
        admin = self.env.ref('base.user_admin')
        share, _code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'expire_at': fields.Datetime.now() + timedelta(days=1),
            'folder_id': self.env['woow.document.folder'].sudo().create({
                'name': 'Creator scope fixture', 'owner_id': admin.id}).id,
            'password': secrets.token_urlsafe(16)})
        link = share.sudo()
        self.assertTrue(link._lock_password_attempts(share.with_user(admin).env))
        stranger = self.env['res.users'].sudo().create({
            'name': 'Scope stranger', 'login': 'wdoc-scope-stranger',
            'groups_id': [Command.set([
                self.env.ref('woow_document_management.group_woow_document_user').id])]})
        # A superuser environment and another user's environment are both refused.
        for env in (self.env, share.with_user(stranger).env):
            with self.assertRaises(AccessError):
                link._lock_password_attempts(env)


@tagged('woow_docmgmt_share_scope')
class TestShareReplacementIsForTheCreator(TransactionCase):
    """Only the creator replaces a link, because a link has no scope of its own.

    A link stores no list of documents: every request works the scope out again in the
    environment of the link's creator. The replacement belongs to whoever makes it, so one made
    by a document administrator would be worked out in the environment of the administrator,
    who sees every subfolder and document, and could open what the link never did. The
    administrator revokes the link and makes a new one through the share wizard instead.
    """

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.share_link_base_url', 'https://docs.example.test')
        users = self.env['res.users'].sudo()
        self.creator = users.create({
            'name': 'Replace creator', 'login': 'wdoc-replace-creator',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_user').id])]})
        self.administrator = users.create({
            'name': 'Replace administrator', 'login': 'wdoc-replace-administrator',
            'groups_id': [Command.set([self.env.ref(
                'woow_document_management.group_woow_document_manager').id])]})
        self.assertFalse(self.administrator.has_group('base.group_system'))
        folders = self.env['woow.document.folder'].sudo()
        documents = self.env['woow.document'].sudo()
        self.root = folders.create({'name': 'Replace root', 'owner_id': self.creator.id})
        # The creator owns the root but is no member of this subfolder, which does not take
        # the members of its parent: only the administrator reads what it holds.
        self.restricted = folders.create({
            'name': 'Replace restricted', 'parent_id': self.root.id,
            'owner_id': self.administrator.id, 'inherit_access': False})
        self.open_doc = documents.create({
            'name': 'open.txt', 'folder_id': self.root.id,
            'datas': base64.b64encode(b'open to the creator')})
        self.restricted_doc = documents.create({
            'name': 'restricted.txt', 'folder_id': self.restricted.id,
            'datas': base64.b64encode(b'for the administrator only')})
        self.env.flush_all()    # the record rules read the grants of the folders from the database
        self.shares = self.env['woow.document.share']

    def _issue(self, user, **vals):
        values = {'share_type': 'folder', 'folder_id': self.root.id,
                  'expire_at': fields.Datetime.now() + timedelta(days=2), **vals}
        return self.shares.with_user(user)._issue(values)

    def _scope(self, share):
        """The documents a visitor of the link gets, worked out the way a request works them out."""
        found, scope_env = self.shares._resolve_request_access(share_id=share.id)
        self.assertEqual(found.id, share.id)
        return found._scope_documents(scope_env)

    def _open(self, token):
        """Whether the link of this code opens anything now: (valid, the documents it lists)."""
        found, scope_env = self.shares._resolve_request_access(token=token)
        return found._validate_for_access(scope_env), found._scope_documents(scope_env)

    def _assert_unchanged(self, share, token, valid=True):
        """The link is still the one that was made: not revoked, and nothing replaced it."""
        share.invalidate_recordset()
        self.assertEqual((share.state, bool(share.revoked_at)), ('active', False))
        self.assertEqual(self._open(token)[0], valid)
        self.assertFalse(self.shares.sudo().search([('replaced_share_id', '=', share.id)]))

    def test_an_administrator_cannot_replace_the_link_of_somebody_else(self):
        share, token = self._issue(self.creator)
        # The link opens what its creator reads. In the environment of the administrator the
        # same link would open the restricted document too: a replacement that the
        # administrator made would not be the same link.
        self.assertEqual(self._scope(share), self.open_doc)
        administrator_env = self.shares.with_user(self.administrator).env
        self.assertEqual(share.sudo()._scope_documents(administrator_env),
                         self.open_doc | self.restricted_doc)
        before = self.shares.sudo().search_count([])
        with self.assertRaises(AccessError):
            share.with_user(self.administrator).action_reissue()
        self._assert_unchanged(share, token)
        self.assertEqual(self.shares.sudo().search_count([]), before)
        self.assertEqual(self._open(token)[1], self.open_doc)

    def test_the_creator_replaces_the_link_and_its_scope_stays_the_same(self):
        share, token = self._issue(self.creator)
        scope = self._scope(share)
        self.assertEqual(scope, self.open_doc)
        result = share.with_user(self.creator).action_reissue()
        self.assertEqual(result['tag'], 'woow_document_share_result')
        (link,) = result['params']['links']
        fresh_token = link.split('#', 1)[1]
        fresh = self.shares.sudo().search([('replaced_share_id', '=', share.id)])
        self.assertEqual((len(fresh), fresh.create_uid), (1, self.creator))
        self.assertEqual(self._scope(fresh), scope)
        valid, listed = self._open(fresh_token)
        self.assertTrue(valid)
        self.assertEqual(listed, scope)
        self.assertNotIn(self.restricted_doc, listed)
        # The link that was replaced is revoked and opens nothing any more.
        share.invalidate_recordset()
        self.assertEqual(share.state, 'revoked')
        self.assertFalse(self._open(token)[0])

    def test_an_administrator_replaces_a_link_of_their_own(self):
        share, token = self._issue(self.administrator)
        scope = self._scope(share)
        self.assertEqual(scope, self.open_doc | self.restricted_doc)
        result = share.with_user(self.administrator).action_reissue()
        fresh = self.shares.sudo().search([('replaced_share_id', '=', share.id)])
        self.assertEqual((len(fresh), fresh.create_uid), (1, self.administrator))
        self.assertEqual(self._scope(fresh), scope)
        self.assertEqual(self._open(result['params']['links'][0].split('#', 1)[1]),
                         (True, scope))
        self.assertFalse(self._open(token)[0])

    def test_a_documents_link_of_a_creator_who_lost_the_rank_is_not_replaced(self):
        """The creator no longer manages the folder: the link lists nothing, and a replacement
        made by the administrator would list the document, which the creator may not share."""
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Replace documents folder', 'owner_id': self.creator.id})
        document = self.env['woow.document'].sudo().create({
            'name': 'listed.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'listed')})
        share, token = self._issue(
            self.creator, share_type='documents', folder_id=False,
            document_ids=[Command.set([document.id])])
        self.assertEqual(self._scope(share), document)
        folder.sudo().write({'owner_id': self.administrator.id})
        self.env.flush_all()
        self.assertFalse(self._scope(share))
        administrator_env = self.shares.with_user(self.administrator).env
        self.assertEqual(share.sudo()._scope_documents(administrator_env), document)
        before = self.shares.sudo().search_count([])
        with self.assertRaises(AccessError):
            share.with_user(self.administrator).action_reissue()
        self._assert_unchanged(share, token, valid=False)
        # The creator may not make the replacement either (none of the listed documents is
        # one that they read any more), and the refusal keeps the link as it is.
        with self.assertRaises(UserError):     # which AccessError is too
            share.with_user(self.creator).action_reissue()
        self._assert_unchanged(share, token, valid=False)
        self.assertEqual(self.shares.sudo().search_count([]), before)

    def test_a_link_whose_creator_was_deleted_is_not_replaced_by_anybody(self):
        share, _code, _visitor = _deleted_creator_link(self)
        before = self.shares.sudo().search_count([])
        with self.assertRaises(AccessError):
            share.with_user(self.administrator).action_reissue()
        self.assertEqual(self.shares.sudo().search_count([]), before)
        self.assertEqual(share.sudo().state, 'active')
        self.assertTrue(share.with_user(self.administrator).action_revoke())
        self.assertEqual(share.sudo().state, 'revoked')

    def test_an_administrator_can_still_revoke_the_link_of_somebody_else(self):
        share, token = self._issue(self.creator)
        self.assertTrue(share.with_user(self.administrator).action_revoke())
        share.invalidate_recordset()
        self.assertEqual((share.state, share.revoked_by_id), ('revoked', self.administrator))
        self.assertFalse(self._open(token)[0])
        events = self.env['woow.document.log'].sudo().search([
            ('share_id', '=', share.id), ('event', '=', 'share_revoke')])
        self.assertEqual(events.mapped('user_id'), self.administrator)
        # A new link is then made through the share wizard, with the scope chosen by its maker.
        wizard = self.env['woow.document.share.wizard'].with_user(self.administrator).create({
            'share_type': 'folder', 'folder_id': self.root.id,
            'expire_at': fields.Datetime.now() + timedelta(days=2)})
        (link,) = wizard.action_create_links()['params']['links']
        valid, listed = self._open(link.split('#', 1)[1])
        self.assertTrue(valid)
        self.assertEqual(listed, self.open_doc | self.restricted_doc)


@tagged('woow_docmgmt_share_http')
class TestShareHttp(HttpCase):

    def _link(self, name, **vals):
        """A folder link with one text file; returns (share, token, document)."""
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': name, 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'fixture.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'content')})
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1), **vals})
        return share, code, doc

    def _sign_in(self, user, password):
        """Sign in with one cookie per name.

        Opening a link rotates the session, and the cookie that ``authenticate`` sets has
        no domain, so it would shadow the rotated cookie the server sets afterwards.
        """
        self.opener = Opener(self.cr)
        self.opener.cookies[TEST_CURSOR_COOKIE_NAME] = self.http_request_key
        self.authenticate(user.login, password)
        values = {cookie.name: cookie.value for cookie in self.opener.cookies}
        self.opener.cookies.clear()
        for name, value in values.items():
            self.opener.cookies.set(name, value, domain='127.0.0.1', path='/')

    def _open(self, code, headers=None):
        # Opening a link rotates the session, so every POST needs a fresh token.
        landing = self.url_open('/woow-docs/s', headers=headers)
        csrf = CSRF_RE.search(landing.content).group(1).decode()
        return self.url_open('/woow-docs/s/open', data={'token': code, 'csrf_token': csrf},
                             headers=headers, allow_redirects=False)

    def _unlock(self, share, password, headers=None):
        page = self.url_open('/woow-docs/s/%d' % share.id, headers=headers)
        csrf = CSRF_RE.search(page.content).group(1).decode()
        return self.url_open('/woow-docs/s/%d/unlock' % share.id, data={
            'password': password, 'csrf_token': csrf}, headers=headers, allow_redirects=False)

    def _trust_forwarded_sources(self):
        """Let requests name their source in X-Real-IP (the test server is the proxy)."""
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_document_management.trusted_proxies', '127.0.0.1/32')
        params.set_param('woow_document_management.client_ip_header', 'X-Real-IP')

    def _sql(self, query, params=()):
        self.env.flush_all()
        self.env.cr.execute(query, params)
        return self.env.cr.fetchall()

    def _source(self, ip):
        return hmac_ip(self.env['woow.document.settings']._share_keys().k_ip, source_key(ip))

    def _denied_rows(self):
        return self._sql("""SELECT (SELECT COUNT(*) FROM woow_document_log WHERE result = 'denied'),
                                   (SELECT COUNT(*) FROM woow_document_denied_counter)""")[0]

    def _capacity_rows(self):
        return self._sql('SELECT bucket_minute, count FROM woow_document_denied_budget '
                         'ORDER BY bucket_minute')

    def _capacity_used(self):
        """Slots used in the minute of the shared test transaction."""
        rows = self._sql("""SELECT count FROM woow_document_denied_budget
                            WHERE bucket_minute = date_trunc('minute', now() AT TIME ZONE 'UTC')""")
        return rows[0][0] if rows else 0

    def _refusals_of(self, ip):
        """Denied audit buckets and counter rows of one rate-limit source."""
        source = self._source(ip)
        return (self._sql("SELECT event, share_id, count FROM woow_document_log "
                          "WHERE result = 'denied' AND ip_hash = %s ORDER BY event, share_id",
                          (source,)),
                self._sql('SELECT event, count FROM woow_document_denied_counter '
                          'WHERE ip_hash = %s ORDER BY event', (source,)))

    def _use_capacity(self, used):
        """Set the slots used in the minute of the shared test transaction."""
        self._sql("""INSERT INTO woow_document_denied_budget (bucket_minute, count)
                     VALUES (date_trunc('minute', now() AT TIME ZONE 'UTC'), %s)
                     ON CONFLICT (bucket_minute) DO UPDATE SET count = EXCLUDED.count
                     RETURNING count""", (used,))

    def _ctid(self, share):
        return self._sql('SELECT ctid FROM woow_document_share WHERE id = %s', (share.id,))[0][0]

    def _unlock_failures(self, share):
        """Wrong passwords counted for one link (all sources)."""
        return self._sql("""SELECT COALESCE(SUM(count), 0) FROM woow_document_log
                            WHERE share_id = %s AND event = 'share_unlock_failed'
                              AND result = 'denied'""", (share.id,))[0][0]

    def test_link_of_a_deleted_creator_is_refused_to_a_signed_in_visitor(self):
        """The page must not list documents that neither the visitor nor the creator may read."""
        password = secrets.token_urlsafe(16)
        share, code, visitor = _deleted_creator_link(self, password)
        self._sign_in(visitor, password)
        opened = self._open(code)
        self.assertEqual(opened.status_code, 404)
        page = self.url_open('/woow-docs/s/%d' % share.id)
        self.assertEqual(page.status_code, 404)
        self.assertNotIn(b'creator-only.txt', page.content)
        self.assertNotIn(b'visitor-managed.txt', page.content)

    def _fresh_client(self, sid=None):
        """A client with its own session cookie (one cookie, named for the test server)."""
        self.opener = Opener(self.cr)
        self.opener.cookies[TEST_CURSOR_COOKIE_NAME] = self.http_request_key
        values = {cookie.name: cookie.value for cookie in self.opener.cookies}
        if sid:
            values['session_id'] = sid
        self.opener.cookies.clear()
        for name, value in values.items():
            self.opener.cookies.set(name, value, domain='127.0.0.1', path='/')

    def _csrf(self):
        landing = self.url_open('/woow-docs/s')
        return CSRF_RE.search(landing.content).group(1).decode()

    def test_closing_a_link_without_a_grant_stores_no_session(self):
        """A close request that removes nothing must not make the server save a session file.

        The form token comes from the public landing page and belongs to the session id
        of the request, which is not stored until the session changes: two requests
        (GET landing, POST close) would otherwise buy a session file each time.
        """
        share, code, _doc = self._link('Close session fixture')
        store = http.root.session_store
        sid = store.generate_key()
        path = store.get_session_filename(sid)
        self._fresh_client(sid)
        csrf = self._csrf()
        self.assertFalse(os.path.exists(path))
        closed = self.url_open('/woow-docs/s/%d/close' % share.id,
                               data={'csrf_token': csrf}, allow_redirects=False)
        self.assertEqual((closed.status_code, closed.headers['Location']), (303, '/woow-docs/s'))
        self.assertFalse(os.path.exists(path))
        # A session that holds the grant is still updated: the grant is removed.
        self._fresh_client(sid)
        self.assertEqual(self._open(code).status_code, 303)
        held = self.opener.cookies.get('session_id')
        self.assertNotEqual(held, sid)
        self.assertIn(str(share.id), store.get(held).get(SESSION_GRANTS_KEY, {}))
        self.assertEqual(self.url_open('/woow-docs/s/%d' % share.id).status_code, 200)
        closed = self.url_open('/woow-docs/s/%d/close' % share.id,
                               data={'csrf_token': self._csrf()}, allow_redirects=False)
        self.assertEqual(closed.status_code, 303)
        self.assertNotIn(str(share.id), store.get(held).get(SESSION_GRANTS_KEY, {}))
        self.assertEqual(self.url_open('/woow-docs/s/%d' % share.id).status_code, 404)

    def test_refusal_records_do_not_tell_whether_a_share_exists(self):
        """A live id without a grant and an unknown id use the same records and capacity.

        A counted source already has its bucket of the minute. With one slot left,
        its refusal of a live link must leave the capacity exactly as its refusal of
        an unknown id does, so that the next new source's refusal gets the same
        answer after either: the capacity cannot be used to test share ids.
        """
        self._trust_forwarded_sources()
        live, _code, _doc = self._link('Liveness fixture')
        unknown = '/woow-docs/s/%d' % (live.id + 1000000)
        maximum = document_log_module.MAX_DENIED_ROWS_PER_MIN
        outcomes = []
        for path, counted, new in ((unknown, '198.51.100.70', '198.51.100.71'),
                                   ('/woow-docs/s/%d' % live.id, '198.51.100.72',
                                    '198.51.100.73')):
            # The counted source's first refusal of the minute: its counter and bucket.
            self._use_capacity(0)
            self.assertEqual(self.url_open(unknown, headers={
                'X-Real-IP': counted}).status_code, 404)
            self._use_capacity(maximum - 1)
            self.assertEqual(self.url_open(path, headers={'X-Real-IP': counted}).status_code, 404)
            used = self._capacity_used()
            status = self.url_open(unknown, headers={'X-Real-IP': new}).status_code
            outcomes.append((used, status, self._refusals_of(counted), self._capacity_used()))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0], (
            maximum - 1, 404, ([('share_access', None, 2)], [('share_access', 2)]), maximum))

    def test_refused_id_routes_do_the_same_work_for_unknown_revoked_and_live_ids(self):
        """Without a session grant an id route is refused before the share is looked up.

        An unknown id, a revoked link and a valid link get the same status and body
        and run the same number of queries, so neither the answer nor the time it
        takes tells whether an id names a live share.
        """
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.denied_rate_per_min', '100')
        live, _code, doc = self._link('Probe live fixture')
        revoked, _code, _doc = self._link('Probe revoked fixture')
        revoked.sudo().write({'revoked_at': fields.Datetime.now(), 'state': 'revoked'})
        unknown = live.id + 1000000
        landing = self.url_open('/woow-docs/s')
        csrf = CSRF_RE.search(landing.content).group(1).decode()

        def get(path):
            return self.url_open(path, allow_redirects=False)

        guess = secrets.token_urlsafe(12)

        def unlock(path):
            return self.url_open(path, data={'password': guess, 'csrf_token': csrf},
                                 allow_redirects=False)

        routes = ((get, '/woow-docs/s/%d'), (get, '/woow-docs/s/%d/file/' + str(doc.id)),
                  (get, '/woow-docs/s/%d/zip'), (unlock, '/woow-docs/s/%d/unlock'))
        for send, template in routes:
            # Warm caches and this source's counter row of the minute first.
            send(template % (unknown + 1))
            results = {}
            for name, share_id in (('unknown', unknown), ('revoked', revoked.id),
                                   ('live', live.id)):
                self.env.flush_all()
                before = self.cr.sql_log_count
                response = send(template % share_id)
                results[name] = (response.status_code, response.content,
                                 self.cr.sql_log_count - before)
            with self.subTest(route=template):
                self.assertEqual(results['unknown'][0], 404)
                self.assertEqual(results['revoked'], results['unknown'])
                self.assertEqual(results['live'], results['unknown'])

    def test_file_route_refuses_a_document_id_beyond_the_database_range_like_any_other(self):
        """The holder of a grant cannot make the file route fail with a database error.

        The document id comes from the URL. A value above PostgreSQL's integer range
        used to fail the document search, so the request answered 500 with a
        traceback, was never counted against its source and could be repeated
        without limit. Every id the link does not cover now gets the refusal of a
        missing document: 404, kept in the source's records and stopped by the
        source's minute limit.
        """
        self._trust_forwarded_sources()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.denied_rate_per_min', '7')
        share, code, doc = self._link('Range fixture')
        source = {'X-Real-IP': '198.51.100.140'}
        self.assertEqual(self._open(code, headers=source).status_code, 303)
        route = '/woow-docs/s/%d/file/%s' % (share.id, '%s')
        self.assertEqual(self.url_open(route % doc.id, headers=source).status_code, 200)
        def counted():
            return dict(self._refusals_of(source['X-Real-IP'])[1]).get('share_access', 0)

        # Missing, the largest and the first id above the range, past 64 bits, and zero.
        ids = (doc.id + 1000000, 2**31 - 1, 2**31, 2**63, 2**64, 99999999999999999999999, 0)
        for document_id in ids:
            with self.subTest(document_id=document_id):
                before = counted()
                response = self.url_open(route % document_id, headers=source)
                self.assertEqual(response.status_code, 404, response.text[:300])
                self.assertEqual(counted(), before + 1)
        limited = self.url_open(route % 99999999999999999999999, headers=source)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(self._refusals_of(source['X-Real-IP']), (
            [('rate_limited', None, 1), ('share_access', None, len(ids))],
            [('share_access', len(ids))]))
        # Refusals never stop a valid request of the same source and session.
        self.assertEqual(self.url_open(route % doc.id, headers=source).status_code, 200)

    def test_wrong_passwords_count_with_nul_in_the_user_agent(self):
        """Control characters in the User-Agent never keep a wrong password from counting.

        PostgreSQL text cannot hold NUL. Written as is, the failure record would make
        the request fail and roll back with its counts, so the cooldown and the
        hourly cap would never be reached. Every wrong password answers 404 and
        counts; at the cooldown limit the next one and the correct password get 429.
        """
        password = secrets.token_urlsafe(16)
        share, code, _doc = self._link('User agent fixture', password=password)
        self.assertEqual(self._open(code).status_code, 303)
        agent = {'User-Agent': 'fixture\x00agent\x1b[0m'}
        limit = self.env['woow.document.settings']._get_settings().share_unlock_max_fail
        for counted in range(1, limit + 1):
            self.assertEqual(self._unlock(share, password + 'x', headers=agent).status_code, 404)
            self.assertEqual(self._unlock_failures(share), counted)
        with patch.object(tokens, 'check_password', side_effect=AssertionError(
                'a password was compared during the cooldown')) as check:
            self.assertEqual(self._unlock(share, password + 'x', headers=agent).status_code, 429)
            self.assertEqual(self._unlock(share, password, headers=agent).status_code, 429)
        check.assert_not_called()
        self.assertEqual(self._unlock_failures(share), limit)
        self.assertEqual(self._sql("""SELECT DISTINCT user_agent FROM woow_document_log
                                      WHERE share_id = %s AND result = 'denied'""",
                                   (share.id,)), [('fixtureagent[0m',)])

    def test_rate_limit_and_valid_token_never_limited(self):
        """T-SHARE-33: the per-source limit refuses only refused requests.

        One source's 11th refused request of the minute is answered 429, whichever
        link it names. Another source behind the trusted proxy (X-Real-IP) is not
        affected. The limited source still opens a valid link (303), unlocks it with
        the correct password (303) and uses the session (200), and its refused
        requests stay limited afterwards.
        """
        self._trust_forwarded_sources()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.denied_rate_per_min', '10')
        password = secrets.token_urlsafe(16)
        protected, protected_code, doc = self._link('Source limit protected', password=password)
        plain, plain_code, _doc = self._link('Source limit plain')
        unknown = '/woow-docs/s/%d' % (plain.id + 1000000)
        limited = {'X-Real-IP': '198.51.100.60'}
        refusals = (
            lambda headers: self._open('x' * 43, headers=headers),              # unknown token
            lambda headers: self.url_open(unknown, headers=headers),            # unknown id
            lambda headers: self.url_open('/woow-docs/s/%d' % plain.id,        # no grant
                                          headers=headers),
        )

        def counted(source):
            """Refusals of one source per event: audit buckets summed, and counter rows."""
            buckets, counters = self._refusals_of(source['X-Real-IP'])
            totals = {}
            for event, _share, count in buckets:
                totals[event] = totals.get(event, 0) + count
            return totals, dict(counters)

        for index in range(10):
            self.assertEqual(refusals[index % 3](limited).status_code, 404)
        for refuse in refusals:
            self.assertEqual(refuse(limited).status_code, 429)
        self.assertEqual(counted(limited), ({'rate_limited': 3, 'share_access': 10},
                                            {'share_access': 10}))
        other = {'X-Real-IP': '198.51.100.61'}
        for refuse in refusals:
            self.assertEqual(refuse(other).status_code, 404)

        self.assertEqual(self._open(protected_code, headers=limited).status_code, 303)
        self.assertEqual(self._unlock(protected, password, headers=limited).status_code, 303)
        page = self.url_open('/woow-docs/s/%d' % protected.id, headers=limited)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'fixture.txt', page.content)
        self.assertEqual(self.url_open('/woow-docs/s/%d/file/%d' % (protected.id, doc.id),
                                       headers=limited).status_code, 200)
        self.assertEqual(self._open(plain_code, headers=limited).status_code, 303)
        self.assertEqual(self.url_open('/woow-docs/s/%d' % plain.id,
                                       headers=limited).status_code, 200)
        self.assertEqual(self.url_open(unknown, headers=limited).status_code, 429)
        self.assertEqual(counted(limited), ({'rate_limited': 4, 'share_access': 10},
                                            {'share_access': 10}))
        self.assertEqual(counted(other), ({'share_access': 3}, {'share_access': 3}))

    def test_link_cooldown_counts_an_ipv6_network_as_one_source(self):
        """The per-link cooldown looks failures up by the source the buckets use (/64).

        Wrong passwords from one IPv6 /64, from one address or from several, use up
        the link's cooldown for every address of that network: a correct password
        from another address of it is answered 429 without a comparison. Another
        /64 still unlocks the link.
        """
        self._trust_forwarded_sources()
        params = self.env['ir.config_parameter'].sudo()
        password = secrets.token_urlsafe(16)
        cases = (
            (None, ['2001:db8:1:2::1'], '2001:db8:1:2::99', '2001:db8:1:3::1'),
            (3, ['2001:db8:5:6::1', '2001:db8:5:6::2', '2001:db8:5:6::3'],
             '2001:db8:5:6::9', '2001:db8:5:7::9'),
        )
        for index, (max_fail, wrong, same_network, other_network) in enumerate(cases):
            if max_fail:
                params.set_param('woow_document_management.share_unlock_max_fail', str(max_fail))
            limit = self.env['woow.document.settings']._get_settings().share_unlock_max_fail
            share, code, _doc = self._link('IPv6 cooldown fixture %d' % index, password=password)
            self.assertEqual(self._open(code).status_code, 303)
            for attempt in range(limit):
                source = {'X-Real-IP': wrong[attempt % len(wrong)]}
                self.assertEqual(self._unlock(share, password + 'x', headers=source).status_code,
                                 404)
            self.assertEqual(self._refusals_of(same_network)[0],
                             [('share_unlock_failed', share.id, limit)])
            with patch.object(tokens, 'check_password', side_effect=AssertionError(
                    'a password was compared during the cooldown of its network')) as check:
                self.assertEqual(self._unlock(share, password, headers={
                    'X-Real-IP': same_network}).status_code, 429)
            check.assert_not_called()
            self.assertEqual(self._unlock(share, password, headers={
                'X-Real-IP': other_network}).status_code, 303)

    def test_hourly_link_cap_refuses_a_new_source_before_comparing(self):
        """Each link counts at most SHARE_UNLOCK_FAIL_PER_HOUR wrong passwords an hour.

        Wrong passwords use no minute capacity, so this cap is what bounds their
        records over many sources. With the cap reached by other sources, a correct
        password from a source the link has never seen is answered 429 without a
        comparison; one failure fewer still lets it unlock.
        """
        self._trust_forwarded_sources()
        password = secrets.token_urlsafe(16)
        log = self.env['woow.document.log']
        cases = ((SHARE_UNLOCK_FAIL_PER_HOUR - 1, '203.0.113.81', 303, 1),
                 (SHARE_UNLOCK_FAIL_PER_HOUR, '203.0.113.82', 429, 0))
        for failures, fresh, expected, compared in cases:
            share, code, _doc = self._link('Hourly cap fixture %d' % failures, password=password)
            for index in range(failures):
                log._log_event('share_unlock_failed', result='denied', channel='share',
                               share_id=share.id, ip='198.51.100.%d' % (index + 1))
            self.assertEqual(self._unlock_failures(share), failures)
            self.assertEqual(self._open(code, headers={'X-Real-IP': fresh}).status_code, 303)
            with patch.object(tokens, 'check_password', wraps=tokens.check_password) as check:
                self.assertEqual(self._unlock(share, password, headers={
                    'X-Real-IP': fresh}).status_code, expected)
            self.assertEqual(check.call_count, compared)

    def test_source_limit_stops_unlock_on_every_link_before_password_check(self):
        """A source that used its denied minute limit on one link cannot try another."""
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_document_management.denied_rate_per_min', '3')
        params.set_param('woow_document_management.share_unlock_max_fail', '10')
        admin = self.env.ref('base.user_admin')
        password = secrets.token_urlsafe(16)
        shares = []
        for index in range(2):
            folder = self.env['woow.document.folder'].sudo().create({
                'name': 'Source limit fixture %d' % index, 'owner_id': admin.id})
            self.env['woow.document'].sudo().create({
                'name': 'fixture.txt', 'folder_id': folder.id,
                'datas': base64.b64encode(b'content')})
            share, code = self.env['woow.document.share'].with_user(admin)._issue({
                'share_type': 'folder', 'folder_id': folder.id,
                'expire_at': fields.Datetime.now() + timedelta(days=1), 'password': password})
            # Opening a link rotates the session, so every POST needs a fresh token.
            csrf = CSRF_RE.search(self.url_open('/woow-docs/s').content).group(1).decode()
            opened = self.url_open('/woow-docs/s/open', data={'token': code, 'csrf_token': csrf},
                                   allow_redirects=False)
            self.assertEqual(opened.status_code, 303)
            shares.append(share)
        first, second = shares
        locked = self.url_open('/woow-docs/s/%d' % first.id)
        csrf = CSRF_RE.search(locked.content).group(1).decode()
        for _ in range(3):
            self.assertEqual(self.url_open('/woow-docs/s/%d/unlock' % first.id, data={
                'password': password + 'x', 'csrf_token': csrf},
                allow_redirects=False).status_code, 404)
        versions = [self._ctid(share) for share in shares]
        log_model = type(self.env['woow.document.log'])
        with patch.object(tokens, 'check_password', side_effect=AssertionError(
                'a password was compared after the source limit')) as check, \
                patch.object(share_controller.WoowDocShareController, '_denied',
                             side_effect=AssertionError('the refusal was counted again')), \
                patch.object(log_model, '_count_denied_across_shares', side_effect=AssertionError(
                    'the source counter row was locked')), \
                patch.object(log_model, '_log_event', side_effect=AssertionError(
                    'the refusal was written in the request transaction')):
            for share in (second, first):
                response = self.url_open('/woow-docs/s/%d/unlock' % share.id, data={
                    'password': password, 'csrf_token': csrf}, allow_redirects=False)
                self.assertEqual(response.status_code, 429)
        check.assert_not_called()
        # Refused attempts take no share lock and write no share row version. The source
        # limit is proved by the request snapshot, so the refusal locks and recounts nothing
        # (a recount in the request transaction is what made bursts of them retry).
        self.assertEqual([self._ctid(share) for share in shares], versions)
        self.env.invalidate_all()
        log = self.env['woow.document.log'].sudo()
        failed = log.search([('event', '=', 'share_unlock_failed'), ('result', '=', 'denied')])
        self.assertEqual([(row.share_id, row.count) for row in failed], [(first, 3)])
        self.assertEqual(log.search([('event', '=', 'rate_limited')]).mapped('count'), [2])
        counter = self.env['woow.document.denied.counter'].sudo().search([
            ('event', '=', 'share_unlock_failed')])
        self.assertEqual(counter.mapped('count'), [3])

    def test_refused_unlock_leaves_the_share_row_unchanged(self):
        """An attempt refused by the link cooldown compares nothing and writes nothing."""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.share_unlock_max_fail', '3')
        password = secrets.token_urlsafe(16)
        share, code, _doc = self._link('Cooldown fixture', password=password)
        self.assertEqual(self._open(code).status_code, 303)
        version = self._ctid(share)
        for _ in range(3):
            self.assertEqual(self._unlock(share, password + 'x').status_code, 404)
        # A counted failure writes a share row version: the barrier of stale attempts.
        counted = self._ctid(share)
        self.assertNotEqual(counted, version)
        with patch.object(tokens, 'check_password', side_effect=AssertionError(
                'a password was compared during the cooldown')) as check:
            self.assertEqual(self._unlock(share, password).status_code, 429)
        check.assert_not_called()
        self.assertEqual(self._ctid(share), counted)

    def test_valid_links_and_sessions_ignore_a_used_up_capacity(self):
        """A used-up minute capacity only changes refused requests (HD-06).

        Valid tokens and unlocked sessions are answered as usual. A source that is
        new in the minute gets 429 for every refusal and nothing is written. A
        source with a counter row gets the same answer for an unknown and an
        existing share id, decided by its count alone, and both refusals are kept
        in its bucket of the minute without another slot.
        """
        self._trust_forwarded_sources()
        password = secrets.token_urlsafe(16)
        protected, protected_code, doc = self._link('Capacity protected', password=password)
        plain, plain_code, _doc = self._link('Capacity plain')
        other, _other_code, _doc = self._link('Capacity other')
        unknown_id = other.id + 1000000
        self.assertEqual(self._open(protected_code).status_code, 303)
        self.assertEqual(self._unlock(protected, password).status_code, 303)
        counted = {'X-Real-IP': '198.51.100.8'}
        self.assertEqual(self.url_open('/woow-docs/s/%d' % unknown_id,
                                       headers=counted).status_code, 404)
        self._use_capacity(document_log_module.MAX_DENIED_ROWS_PER_MIN)
        before = self._denied_rows()

        page = self.url_open('/woow-docs/s/%d' % protected.id)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'fixture.txt', page.content)
        self.assertEqual(self.url_open('/woow-docs/s/%d/file/%d' % (
            protected.id, doc.id)).status_code, 200)
        self.assertEqual(self.url_open('/woow-docs/s/%d/zip' % protected.id).status_code, 200)
        self.assertEqual(self.url_open('/woow-docs/s').status_code, 200)
        self.assertEqual(self._open(plain_code).status_code, 303)
        self.assertEqual(self.url_open('/woow-docs/s/%d' % plain.id).status_code, 200)

        new = {'X-Real-IP': '198.51.100.9'}
        self.assertEqual(self._open('x' * 43, headers=new).status_code, 429)
        self.assertEqual(self.url_open('/woow-docs/s/%d' % unknown_id,
                                       headers=new).status_code, 429)
        self.assertEqual(self.url_open('/woow-docs/s/%d' % other.id,
                                       headers=new).status_code, 429)
        self.assertEqual(self._denied_rows(), before)

        self.assertEqual(self.url_open('/woow-docs/s/%d' % unknown_id,
                                       headers=counted).status_code, 404)
        self.assertEqual(self.url_open('/woow-docs/s/%d' % other.id,
                                       headers=counted).status_code, 404)
        # Both refusals joined the source's bucket of the minute: no slot was needed,
        # and the full minute was neither written nor passed.
        self.assertEqual(self._refusals_of(counted['X-Real-IP']),
                         ([('share_access', None, 3)], [('share_access', 3)]))
        self.assertEqual(self._capacity_used(), document_log_module.MAX_DENIED_ROWS_PER_MIN)

    def _capacity_row_is_free(self, minute):
        """True when another connection can update the minute's capacity row at once."""
        with closing(sql_db.db_connect(self.env.cr.dbname).cursor()) as probe:
            try:
                with mute_logger('odoo.sql_db'):
                    probe.execute("SET LOCAL lock_timeout = '300ms'")
                    probe.execute("""INSERT INTO woow_document_denied_budget (bucket_minute, count)
                                     VALUES (%s, 0) ON CONFLICT (bucket_minute)
                                     DO UPDATE SET count = woow_document_denied_budget.count""",
                                  (minute,))
                return True
            except errors.LockNotAvailable:
                return False
            finally:
                probe.rollback()

    def test_correct_unlock_uses_no_capacity(self):
        """A correct password from a new source reserves no slot and leaves no row.

        While the password is compared, another connection can update the minute's
        capacity row at once: the unlock holds no lock on it.
        """
        password = secrets.token_urlsafe(16)
        share, code, _doc = self._link('Capacity free unlock', password=password)
        self.assertEqual(self._open(code).status_code, 303)
        minute = self._sql("SELECT date_trunc('minute', now() AT TIME ZONE 'UTC')")[0][0]
        capacity = self._capacity_rows()
        refusals = self._denied_rows()
        probes = []
        compare = tokens.check_password

        def compare_and_probe(*args, **kwargs):
            probes.append(self._capacity_row_is_free(minute))
            return compare(*args, **kwargs)

        with patch.object(tokens, 'check_password', side_effect=compare_and_probe) as check:
            self.assertEqual(self._unlock(share, password).status_code, 303)
        check.assert_called_once()
        self.assertEqual(probes, [True])
        self.assertEqual(self._capacity_rows(), capacity)
        self.assertEqual(self._denied_rows(), refusals)
        self.assertEqual(self._refusals_of('127.0.0.1'), ([], []))
        self.assertEqual(self.url_open('/woow-docs/s/%d' % share.id).status_code, 200)

    def test_refusals_and_passwords_with_one_and_no_capacity_slot_left(self):
        """The capacity only decides whether a new source's refusal can be recorded.

        With one slot left, a new source's first refusal takes it for its counter
        row and is answered by its count (404); its audit bucket finds no room.
        With no slot left, a new source's refusal gets 429 and nothing is written.
        A wrong password needs no slot: it is compared once and counted for the
        link and the source, so the per-link limits never depend on the capacity.
        """
        self._trust_forwarded_sources()
        password = secrets.token_urlsafe(16)
        share, code, _doc = self._link('One slot fixture', password=password)
        unknown = '/woow-docs/s/%d' % (share.id + 1000000)
        with patch.object(document_log_module, 'MAX_DENIED_ROWS_PER_MIN', 2):
            self._use_capacity(1)
            self.assertEqual(self.url_open(unknown, headers={
                'X-Real-IP': '198.51.100.31'}).status_code, 404)
            self.assertEqual(self._refusals_of('198.51.100.31'), ([], [('share_access', 1)]))
            self.assertEqual(self._capacity_used(), 2)

            before = self._denied_rows()
            self.assertEqual(self.url_open(unknown, headers={
                'X-Real-IP': '198.51.100.32'}).status_code, 429)
            self.assertEqual(self._denied_rows(), before)

            guesser = {'X-Real-IP': '198.51.100.33'}
            self.assertEqual(self._open(code, headers=guesser).status_code, 303)
            with patch.object(tokens, 'check_password', wraps=tokens.check_password) as check:
                self.assertEqual(self._unlock(share, password + 'x', headers=guesser).status_code,
                                 404)
                self.assertEqual(check.call_count, 1)
                self.assertEqual(self._unlock(share, password, headers=guesser).status_code, 303)
                self.assertEqual(check.call_count, 2)
            self.assertEqual(self._refusals_of('198.51.100.33'), (
                [('share_unlock_failed', share.id, 1)], [('share_unlock_failed', 1)]))
            self.assertEqual(self._capacity_used(), 2)

    def test_refusals_without_a_connection_for_a_slot_answer_429_not_500(self):
        """A full connection pool never turns a refusal into a server error.

        Counting a refusal borrows a second connection, for a short transaction of its
        own. While none can be had, refusals answer 429 on every route and write
        nothing, whether or not the source was counted before. Valid links, wrong
        passwords (which are counted in the request) and unlocked sessions are served as
        usual, and the source is counted again once a connection can be had.
        """
        self._trust_forwarded_sources()
        password = secrets.token_urlsafe(16)
        share, code, _doc = self._link('No connection fixture', password=password)
        unknown = '/woow-docs/s/%d' % (share.id + 1000000)
        counted = {'X-Real-IP': '198.51.100.120'}
        self.assertEqual(self.url_open(unknown, headers=counted).status_code, 404)
        original = self.registry.cursor

        def cursor(*args, **kwargs):
            if sys._getframe(1).f_code.co_name in ('_take_slot', '_short_transaction'):
                raise PoolError('The Connection Pool Is Full')
            return original(*args, **kwargs)

        capacity, refusals = self._capacity_rows(), self._denied_rows()
        with patch.object(self.registry, 'cursor', cursor):
            sends = (lambda headers: self._open('x' * 43, headers=headers),
                     lambda headers: self.url_open(unknown, headers=headers),
                     lambda headers: self.url_open('/woow-docs/s/%d' % share.id, headers=headers))
            for number, send in enumerate(sends):
                headers = {'X-Real-IP': '198.51.100.%d' % (121 + number)}
                self.assertEqual(send(headers).status_code, 429)
                self.assertEqual(self._refusals_of(headers['X-Real-IP']), ([], []))
            self.assertEqual(self._capacity_rows(), capacity)
            self.assertEqual(self._denied_rows(), refusals)
            self.assertEqual(self.url_open(unknown, headers=counted).status_code, 429)
            self.assertEqual(self._refusals_of('198.51.100.120'),
                             ([('share_access', None, 1)], [('share_access', 1)]))
            guesser = {'X-Real-IP': '198.51.100.130'}
            self.assertEqual(self._open(code, headers=guesser).status_code, 303)
            self.assertEqual(self._unlock(share, password + 'x', headers=guesser).status_code,
                             404)
            self.assertEqual(self._refusals_of('198.51.100.130'), (
                [('share_unlock_failed', share.id, 1)], [('share_unlock_failed', 1)]))
            self.assertEqual(self._unlock(share, password, headers=guesser).status_code, 303)
            self.assertEqual(self.url_open('/woow-docs/s/%d' % share.id,
                                           headers=guesser).status_code, 200)
        self.assertEqual(self.url_open(unknown, headers=counted).status_code, 404)
        self.assertEqual(self._refusals_of('198.51.100.120'),
                         ([('share_access', None, 2)], [('share_access', 2)]))

    def test_refusals_from_one_ipv6_network_and_many_links_stay_bounded(self):
        """A /64 is one source, and refusals on many links share the source's records."""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.denied_rate_per_min', '3')
        self._trust_forwarded_sources()
        unknown = self._link('Network fixture')[0].id + 1000000
        before = self._denied_rows()
        statuses = [self.url_open('/woow-docs/s/%d' % unknown, headers={
            'X-Real-IP': '2001:db8:0:1::%x' % number}).status_code for number in range(1, 33)]
        self.assertEqual(statuses, [404] * 3 + [429] * 29)
        self.assertEqual(self._refusals_of('2001:db8:0:1::1'), (
            [('rate_limited', None, 29), ('share_access', None, 3)], [('share_access', 3)]))
        after = self._denied_rows()
        self.assertEqual((after[0] - before[0], after[1] - before[1]), (2, 1))
        links = [self._link('Fan-out fixture %d' % number)[0] for number in range(5)]
        statuses = [self.url_open('/woow-docs/s/%d' % link.id, headers={
            'X-Real-IP': '198.51.100.77'}).status_code for _round in range(2) for link in links]
        self.assertEqual(statuses, [404] * 3 + [429] * 7)
        self.assertEqual(self._refusals_of('198.51.100.77'), (
            [('rate_limited', None, 7), ('share_access', None, 3)], [('share_access', 3)]))
        self.assertFalse(self._sql("""SELECT 1 FROM woow_document_log WHERE result = 'denied'
                                      AND share_id = ANY(%s)""", ([link.id for link in links],)))

    def test_forwarded_address_from_an_untrusted_peer_is_ignored(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_document_management.denied_rate_per_min', '3')
        params.set_param('woow_document_management.client_ip_header', 'X-Real-IP')
        unknown = self._link('Untrusted peer fixture')[0].id + 1000000
        for trusted in ('', '10.0.0.0/8'):
            with self.subTest(trusted=trusted):
                params.set_param('woow_document_management.trusted_proxies', trusted)
                self.env.flush_all()
                self.env.cr.execute('DELETE FROM woow_document_denied_counter')
                statuses = [self.url_open('/woow-docs/s/%d' % unknown, headers={
                    'X-Real-IP': '203.0.113.%d' % number}).status_code for number in range(1, 5)]
                self.assertEqual(statuses, [404, 404, 404, 429])
                self.assertEqual(self._refusals_of('127.0.0.1')[1], [('share_access', 3)])
                self.assertEqual(self._refusals_of('203.0.113.1')[1], [])

    def test_denied_rate_limit_is_shared_by_unknown_and_valid_link_ids(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.denied_rate_per_min', '3')
        self.env['woow.document.log'].sudo().search([
            ('event', '=', 'share_access'), ('result', '=', 'denied')]).unlink()
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Rate limit fixture', 'owner_id': admin.id})
        share, _code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        unknown = share.id + 1000000
        for _ in range(3):
            self.assertEqual(self.url_open('/woow-docs/s/%d' % unknown).status_code, 404)
        # A valid ID without a session grant must use the same IP counter.
        self.assertEqual(self.url_open('/woow-docs/s/%d' % share.id).status_code, 429)
        self.env.invalidate_all()
        self.assertEqual(self.env['woow.document.log'].sudo().search_count([
            ('event', '=', 'rate_limited'), ('result', '=', 'denied')]), 1)

    def test_folder_share_zip_hides_ancestor_folder_name(self):
        admin = self.env.ref('base.user_admin')
        ancestor = self.env['woow.document.folder'].sudo().create({
            'name': 'Private ancestor marker', 'owner_id': admin.id})
        root = self.env['woow.document.folder'].sudo().create({
            'name': 'Shared root', 'parent_id': ancestor.id, 'owner_id': admin.id})
        self.env['woow.document'].sudo().create({
            'name': 'shared.txt', 'folder_id': root.id,
            'datas': base64.b64encode(b'content')})
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': root.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        landing = self.url_open('/woow-docs/s')
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content)
        self.assertIsNotNone(csrf)
        opened = self.url_open('/woow-docs/s/open', data={
            'token': code, 'csrf_token': csrf.group(1).decode()},
            allow_redirects=False)
        self.assertEqual(opened.status_code, 303)
        response = self.url_open('/woow-docs/s/%d/zip' % share.id)
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ['Shared root/shared.txt'])
            self.assertEqual(archive.read('Shared root/shared.txt'), b'content')

    def test_document_list_share_zip_has_no_folder_path(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Unshared ancestor marker', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'shared.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'content')})
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'documents', 'document_ids': [(6, 0, [doc.id])],
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        landing = self.url_open('/woow-docs/s')
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content)
        self.assertIsNotNone(csrf)
        opened = self.url_open('/woow-docs/s/open', data={
            'token': code, 'csrf_token': csrf.group(1).decode()},
            allow_redirects=False)
        self.assertEqual(opened.status_code, 303)
        response = self.url_open('/woow-docs/s/%d/zip' % share.id)
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ['shared.txt'])
            self.assertEqual(archive.read('shared.txt'), b'content')

    def test_concurrent_zip_downloads_respect_one_use_limit(self):
        """T-SHARE-32: two separate sessions race for one remaining ZIP download."""
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'One use fixture', 'owner_id': admin.id})
        self.env['woow.document'].sudo().create({
            'name': 'once.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'only-once')})
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
            'max_downloads': 1})
        base = self.base_url()
        clients = []
        for _ in range(2):
            client = requests.Session()
            landing = client.get(base + '/woow-docs/s', timeout=12)
            csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content)
            self.assertIsNotNone(csrf)
            opened = client.post(base + '/woow-docs/s/open', data={
                'token': code, 'csrf_token': csrf.group(1).decode()},
                allow_redirects=False, timeout=12)
            self.assertEqual(opened.status_code, 303, opened.text)
            clients.append(client)

        barrier = threading.Barrier(2)

        def download(client):
            barrier.wait(timeout=12)
            response = client.get(base + '/woow-docs/s/%d/zip' % share.id, timeout=20)
            return response.status_code, response.content

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(download, clients))
        self.assertEqual([status for status, _ in results].count(200), 1, results)
        self.assertTrue({status for status, _ in results} <= {200, 404, 503}, results)
        successful = next(content for status, content in results if status == 200)
        with zipfile.ZipFile(io.BytesIO(successful)) as archive:
            self.assertEqual(archive.read('One use fixture/once.txt'), b'only-once')
        self.env.invalidate_all()
        self.assertEqual(self.env['woow.document.log'].sudo()._sum_count([
            ('share_id', '=', share.id), ('event', '=', 'share_download'),
            ('result', '=', 'ok')]), 1)

    def test_oversized_share_scope_refuses_page_and_zip(self):
        admin = self.env.ref('base.user_admin')
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.zip_max_files', '2')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Scope limit', 'owner_id': admin.id})
        for index in range(3):
            self.env['woow.document'].sudo().create({
                'name': 'scope-%d.txt' % index, 'folder_id': folder.id,
                'datas': base64.b64encode(b'content')})
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        landing = self.url_open('/woow-docs/s')
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content)
        self.assertIsNotNone(csrf)
        opened = self.url_open('/woow-docs/s/open', data={
            'token': code, 'csrf_token': csrf.group(1).decode()}, allow_redirects=False)
        self.assertEqual(opened.status_code, 303)
        self.assertEqual(self.url_open('/woow-docs/s/%d' % share.id).status_code, 400)
        self.assertEqual(self.url_open('/woow-docs/s/%d/zip' % share.id).status_code, 400)

    def test_simultaneous_wrong_passwords_respect_share_limit(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Concurrent unlock', 'owner_id': admin.id})
        self.env['woow.document'].sudo().create({
            'name': 'race.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'content')})
        password = secrets.token_urlsafe(16)
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
            'password': password})
        base = self.base_url()
        self.assertEqual(self.url_open('/woow-docs/s').status_code, 200)
        clients = []
        # Each client obtains a separate rotated session grant before racing.
        for _ in range(8):
            client = requests.Session()
            client.cookies.update(self.opener.cookies)
            landing = client.get(base + '/woow-docs/s', timeout=12)
            csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content)
            self.assertIsNotNone(csrf)
            opened = client.post(base + '/woow-docs/s/open', data={
                'token': code, 'csrf_token': csrf.group(1).decode()},
                allow_redirects=False, timeout=12)
            self.assertEqual(opened.status_code, 303)
            locked = client.get(base + '/woow-docs/s/%d' % share.id, timeout=12)
            csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', locked.content)
            self.assertIsNotNone(csrf)
            clients.append((client, csrf.group(1).decode()))

        def guess(item):
            client, csrf = item
            return client.post(base + '/woow-docs/s/%d/unlock' % share.id,
                               data={'password': password + 'x', 'csrf_token': csrf},
                               allow_redirects=False, timeout=15).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(guess, clients))
        self.assertTrue(set(statuses) <= {404, 429, 503}, statuses)
        limit = self.env['woow.document.settings']._get_settings().share_unlock_max_fail
        self.assertLessEqual(statuses.count(404), limit, statuses)
        self.env.invalidate_all()
        denied = self.env['woow.document.log'].sudo().search([
            ('share_id', '=', share.id), ('event', '=', 'share_unlock_failed'),
            ('result', '=', 'denied')])
        self.assertLessEqual(sum(denied.mapped('count')), limit)

    def test_non_admin_creator_password_failure_is_bounded(self):
        group = self.env.ref('woow_document_management.group_woow_document_user')
        creator = self.env['res.users'].sudo().create({
            'name': 'Document owner', 'login': 'wdoc-share-owner',
            'groups_id': [Command.set([group.id])]})
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Owner folder', 'owner_id': creator.id})
        self.env['woow.document'].sudo().create({
            'name': 'owned.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'owner-content')})
        password = secrets.token_urlsafe(16)
        share, code = self.env['woow.document.share'].with_user(creator)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
            'password': password})
        landing = self.url_open('/woow-docs/s')
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content).group(1).decode()
        opened = self.url_open('/woow-docs/s/open', data={'token': code, 'csrf_token': csrf},
                               allow_redirects=False)
        self.assertEqual(opened.status_code, 303)
        locked = self.url_open('/woow-docs/s/%d' % share.id)
        self.assertEqual(locked.status_code, 200)
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', locked.content).group(1).decode()
        wrong = self.url_open('/woow-docs/s/%d/unlock' % share.id,
                              data={'password': password + 'x', 'csrf_token': csrf},
                              allow_redirects=False)
        self.assertEqual(wrong.status_code, 404, wrong.text)

    def test_public_token_exchange_download_and_revocation(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Public fixture', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'review.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'limited-download')})
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'name': 'Review', 'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
            'max_downloads': 1,
        })
        landing = self.url_open('/woow-docs/s')
        self.assertEqual(landing.status_code, 200)
        self.assertNotIn(code.encode(), landing.content)
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content)
        self.assertIsNotNone(csrf)
        opened = self.url_open('/woow-docs/s/open',
                               data={'link': 'https://example.test/woow-docs/s#' + code,
                                     'csrf_token': csrf.group(1).decode()},
                               allow_redirects=False)
        self.assertEqual(opened.status_code, 303, opened.text)
        self.assertEqual(opened.headers['Location'], '/woow-docs/s/%d' % share.id)
        page = self.url_open('/woow-docs/s/%d' % share.id)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn(b'review.txt', page.content)
        version = self._ctid(share)
        downloaded = self.url_open('/woow-docs/s/%d/file/%d' % (share.id, doc.id))
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b'limited-download')
        self.assertEqual(downloaded.headers['Referrer-Policy'], 'no-referrer')
        # A counted download of a limited link writes a share row version, the
        # barrier that makes requests with an older snapshot fail and retry.
        self.assertNotEqual(self._ctid(share), version)
        version = self._ctid(share)
        blocked = self.url_open('/woow-docs/s/%d/file/%d' % (share.id, doc.id))
        self.assertEqual(blocked.status_code, 404)
        with patch.object(streaming, 'build_zip', side_effect=AssertionError(
                'exhausted share must not build an archive')) as build:
            blocked_zip = self.url_open('/woow-docs/s/%d/zip' % share.id)
        self.assertEqual(blocked_zip.status_code, 404)
        build.assert_not_called()
        # Refused downloads of a used-up link write no share row version.
        self.assertEqual(self._ctid(share), version)
        share.sudo().write({'revoked_at': fields.Datetime.now(), 'state': 'revoked'})
        revoked = self.url_open('/woow-docs/s/%d' % share.id)
        self.assertEqual(revoked.status_code, 404)

    def _visitor_events(self, share):
        """(event, user, created by) of the events of a link, oldest first."""
        return self._sql("""SELECT event, user_id, create_uid FROM woow_document_log
                            WHERE share_id = %s AND result = 'ok' ORDER BY id""", (share.id,))

    def _use_link(self, share, code, doc):
        """Open a link, view its page, download one file and the whole link as a ZIP."""
        self.assertEqual(self._open(code).status_code, 303)
        self.assertEqual(self.url_open('/woow-docs/s/%d' % share.id).status_code, 200)
        self.assertEqual(self.url_open(
            '/woow-docs/s/%d/file/%d' % (share.id, doc.id)).status_code, 200)
        self.assertEqual(self.url_open('/woow-docs/s/%d/zip' % share.id).status_code, 200)

    def test_events_of_a_link_are_recorded_as_the_visitor_not_the_creator(self):
        """The creator's environment decides the scope; it must not sign what visitors do."""
        admin = self.env.ref('base.user_admin')
        share, code, doc = self._link('Visitor identity fixture', max_downloads=2)
        self.env.flush_all()
        self._use_link(share, code, doc)
        events = self._visitor_events(share)
        self.assertEqual([event[0] for event in events],
                         ['share_create', 'share_open', 'share_access', 'share_download',
                          'share_download'])
        created, visits = events[0], events[1:]
        self.assertEqual(created[1:], (admin.id, admin.id))
        for event, user_id, creator in visits:
            with self.subTest(event=event):
                self.assertIsNone(user_id)           # an anonymous visitor names nobody
                self.assertNotEqual(creator, admin.id)
        # The creator reads them (R-L1), and the limited link still counts its downloads.
        log = self.env['woow.document.log'].with_user(admin)
        self.assertEqual(log.search_count([
            ('share_id', '=', share.id), ('event', '=', 'share_download')]), 2)
        self.assertEqual(share.with_user(admin).download_count, 2)
        self.assertEqual(self.url_open('/woow-docs/s/%d/file/%d' % (share.id, doc.id)
                                       ).status_code, 404)

        # A signed-in visitor is recorded as that visitor.
        password = secrets.token_urlsafe(16)
        visitor = self.env['res.users'].sudo().create({
            'name': 'Identity visitor', 'login': 'wdoc-identity-visitor', 'password': password,
            'groups_id': [Command.set([self.env.ref('base.group_user').id])]})
        other, other_code, other_doc = self._link('Visitor identity second link')
        self.env.flush_all()
        self._sign_in(visitor, password)
        self._use_link(other, other_code, other_doc)
        for event, user_id, creator in self._visitor_events(other)[1:]:
            with self.subTest(event=event, visitor='signed in'):
                self.assertEqual((user_id, creator), (visitor.id, visitor.id))

    def test_log_retention_never_reopens_an_exhausted_limited_share(self):
        """the weekly audit retention keeps the counted download of a live share."""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.log_retention_days', '30')
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Retention http fixture', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'retention.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'counted-once')})
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=60),
            'max_downloads': 1})
        landing = self.url_open('/woow-docs/s')
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content)
        self.assertIsNotNone(csrf)
        opened = self.url_open('/woow-docs/s/open', data={
            'token': code, 'csrf_token': csrf.group(1).decode()},
            allow_redirects=False)
        self.assertEqual(opened.status_code, 303)
        path = '/woow-docs/s/%d/file/%d' % (share.id, doc.id)
        self.assertEqual(self.url_open(path).status_code, 200)
        row = self.env['woow.document.log'].sudo().search([
            ('share_id', '=', share.id), ('event', '=', 'share_download'),
            ('result', '=', 'ok')])
        self.assertEqual(len(row), 1)
        self.env.cr.execute('UPDATE woow_document_log SET create_date = %s WHERE id = %s',
                            (fields.Datetime.now() - timedelta(days=31), row.id))
        self.env.invalidate_all()
        self.env['woow.document'].sudo()._cron_log_retention()
        self.assertTrue(row.exists())
        self.assertEqual(self.url_open(path).status_code, 404)

    def test_partial_file_download_consumes_one_share_slot(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Partial download fixture', 'owner_id': admin.id})
        doc = self.env['woow.document'].sudo().create({
            'name': 'partial.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'partial-content')})
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
            'max_downloads': 1})
        landing = self.url_open('/woow-docs/s')
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content)
        self.assertIsNotNone(csrf)
        opened = self.url_open('/woow-docs/s/open', data={
            'token': code, 'csrf_token': csrf.group(1).decode()},
            allow_redirects=False)
        self.assertEqual(opened.status_code, 303)

        path = '/woow-docs/s/%d/file/%d' % (share.id, doc.id)
        partial = self.url_open(path, headers={'Range': 'bytes=0-3'})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b'part')
        self.assertEqual(self.env['woow.document.log'].sudo().search_count([
            ('share_id', '=', share.id), ('event', '=', 'share_download'),
            ('result', '=', 'ok')]), 1)
        blocked = self.url_open(path)
        self.assertEqual(blocked.status_code, 404)

    def test_password_unlock_and_zip_use_the_same_bounded_grant(self):
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Protected fixture', 'owner_id': admin.id})
        self.env['woow.document'].sudo().create({
            'name': 'protected.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'protected-content')})
        password = secrets.token_urlsafe(16)
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'name': 'Protected', 'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1), 'password': password})
        landing = self.url_open('/woow-docs/s')
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content).group(1).decode()
        opened = self.url_open('/woow-docs/s/open', data={'token': code, 'csrf_token': csrf},
                               allow_redirects=False)
        self.assertEqual(opened.status_code, 303)
        locked = self.url_open('/woow-docs/s/%d' % share.id)
        self.assertEqual(locked.status_code, 200)
        self.assertIn(b'type="password"', locked.content)
        self.assertNotIn(b'protected.txt', locked.content)
        unlock_csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', locked.content).group(1).decode()
        wrong = self.url_open('/woow-docs/s/%d/unlock' % share.id,
                              data={'password': password + 'x', 'csrf_token': unlock_csrf},
                              allow_redirects=False)
        self.assertEqual(wrong.status_code, 404)
        unlocked = self.url_open('/woow-docs/s/%d/unlock' % share.id,
                                 data={'password': password, 'csrf_token': unlock_csrf},
                                 allow_redirects=False)
        self.assertEqual(unlocked.status_code, 303, unlocked.text)
        archive = self.url_open('/woow-docs/s/%d/zip' % share.id)
        self.assertEqual(archive.status_code, 200)
        self.assertEqual(archive.headers['Referrer-Policy'], 'no-referrer')
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            self.assertEqual(zipped.read('Protected fixture/protected.txt'), b'protected-content')
        # The session rotates again on unlock, so obtain a fresh CSRF token.
        landing_after_unlock = self.url_open('/woow-docs/s')
        close_csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"',
                               landing_after_unlock.content).group(1).decode()
        closed = self.url_open('/woow-docs/s/%d/close' % share.id,
                               data={'csrf_token': close_csrf}, allow_redirects=False)
        self.assertEqual(closed.status_code, 303)
        after_close = self.url_open('/woow-docs/s/%d' % share.id)
        self.assertEqual(after_close.status_code, 404)
