# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import base64
import io
import re
import secrets
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import requests
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..services.client_ip import hmac_ip
from ..services.zip_builder import ZipLimitExceeded
from ..controllers import streaming


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
        since = fields.Datetime.now() - timedelta(minutes=2)
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


@tagged('woow_docmgmt_share_http')
class TestShareHttp(HttpCase):

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
        downloaded = self.url_open('/woow-docs/s/%d/file/%d' % (share.id, doc.id))
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b'limited-download')
        self.assertEqual(downloaded.headers['Referrer-Policy'], 'no-referrer')
        blocked = self.url_open('/woow-docs/s/%d/file/%d' % (share.id, doc.id))
        self.assertEqual(blocked.status_code, 404)
        with patch.object(streaming, 'build_zip', side_effect=AssertionError(
                'exhausted share must not build an archive')) as build:
            blocked_zip = self.url_open('/woow-docs/s/%d/zip' % share.id)
        self.assertEqual(blocked_zip.status_code, 404)
        build.assert_not_called()
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
