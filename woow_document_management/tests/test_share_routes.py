# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import base64
import io
import re
import secrets
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

import requests
from psycopg2 import errors

from odoo import Command, SUPERUSER_ID, api, fields, sql_db
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger

from ..models import document_log as document_log_module
from ..services import tokens
from ..services.client_ip import hmac_ip, source_key
from ..services.zip_builder import ZipLimitExceeded
from ..controllers import streaming

CSRF_RE = re.compile(rb'name="csrf_token"[^>]*value="([^"]+)"')


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
            share._record_download(scope_env, '192.0.2.10', {'files': 1})
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
            share_a._record_download(scope_a, '192.0.2.11', {'files': 1})
            cr_a.commit()
            share_b, scope_b = scope(cr_b)
            with mute_logger('odoo.sql_db'), self.assertRaises(errors.SerializationFailure):
                share_b._download_slot_available(scope_b)
            cr_b.rollback()
            self.assertFalse(share_b._download_slot_available(scope_b))
            cr_b.rollback()

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
        with patch.object(tokens, 'check_password', side_effect=AssertionError(
                'a password was compared after the source limit')) as check:
            for share in (second, first):
                response = self.url_open('/woow-docs/s/%d/unlock' % share.id, data={
                    'password': password, 'csrf_token': csrf}, allow_redirects=False)
                self.assertEqual(response.status_code, 429)
        check.assert_not_called()
        # Refused attempts take no share lock and write no share row version.
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
        """T-SHARE-33: a used-up minute capacity only changes refused requests.

        Valid tokens and unlocked sessions are answered as usual. A source that is
        new in the minute gets 429 for every refusal and nothing is written. A
        source with a counter row gets the same answer for an unknown and an
        existing share id, decided by its count alone.
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
        # The bucket of the existing id found no capacity; its refusal still counted.
        self.assertEqual(self._refusals_of(counted['X-Real-IP']),
                         ([('share_access', None, 2)], [('share_access', 3)]))
        self.assertEqual(self._capacity_used(), document_log_module.MAX_DENIED_ROWS_PER_MIN + 1)

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
            self.assertEqual(self._capacity_used(), 3)

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
            self.assertEqual(self._capacity_used(), 3)

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
