# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""Additional executable Odoo core assumptions for the integration gate.

This module is intentionally separate until the parent consolidates it into the
canonical core assumptions test module.
"""

import base64
import hmac
import json
import re
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from werkzeug.test import EnvironBuilder

from odoo import Command, fields, http
from odoo.addons.portal.models.mail_thread import MailThread as PortalMailThread
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..services.client_ip import parse_trusted_proxies, request_client_ip
from ..services.constants import SESSION_GRANTS_KEY


@tagged('woow_docmgmt_core_assumptions')
class TestCoreAssumptionsOrm(TransactionCase):

    def test_ca14_http_request_retains_original_proxy_environ(self):
        """CA-14: the real WSGI entrypoint applies ProxyFix only with both gates."""
        class StopAfterProxyFix(Exception):
            pass

        settings = SimpleNamespace(
            trusted_proxies=parse_trusted_proxies('10.0.0.0/8'),
            client_ip_header='X-Real-IP',
        )
        for proxy_mode, forwarded_host, expected_fixed in (
            (False, True, False),
            (True, False, False),
            (True, True, True),
        ):
            with self.subTest(proxy_mode=proxy_mode, forwarded_host=forwarded_host):
                headers = {
                    'X-Forwarded-For': '198.51.100.7',
                    'X-Forwarded-Proto': 'https',
                    'X-Real-IP': '203.0.113.9',
                }
                if forwarded_host:
                    headers['X-Forwarded-Host'] = 'forwarded.invalid'
                environ = EnvironBuilder(path='/probe', base_url='http://origin.invalid',
                                         headers=headers).get_environ()
                environ['REMOTE_ADDR'] = '10.0.0.8'
                # Stop after the real WSGI proxy branch and HTTPRequest creation,
                # before routing or database access can affect this core probe.
                with patch.dict(http.odoo.tools.config.options, {'proxy_mode': proxy_mode}), \
                     patch.object(http, 'Request', side_effect=StopAfterProxyFix):
                    with self.assertRaises(StopAfterProxyFix):
                        http.root(environ, lambda status, headers: None)

                request = http.HTTPRequest(environ)
                if expected_fixed:
                    self.assertEqual(environ['REMOTE_ADDR'], '198.51.100.7')
                    self.assertEqual(environ['HTTP_HOST'], 'forwarded.invalid')
                    self.assertEqual(environ['wsgi.url_scheme'], 'https')
                    original = environ['werkzeug.proxy_fix.orig']
                    self.assertEqual(original['REMOTE_ADDR'], '10.0.0.8')
                    self.assertEqual(original['HTTP_HOST'], 'origin.invalid')
                    self.assertEqual(request.headers.environ['werkzeug.proxy_fix.orig'],
                                     original)
                else:
                    self.assertEqual(environ['REMOTE_ADDR'], '10.0.0.8')
                    self.assertEqual(environ['HTTP_HOST'], 'origin.invalid')
                    self.assertEqual(environ['wsgi.url_scheme'], 'http')
                    self.assertNotIn('werkzeug.proxy_fix.orig', environ)
                self.assertEqual(request_client_ip(request, settings), '203.0.113.9')

    def test_ca22_searchpanel_no_hidden_ancestor_names(self):
        """CA-22: a folder visible alone never exposes an invisible ancestor."""
        folder_model = self.env['woow.document.folder'].sudo()
        root = folder_model.create({'name': 'CA public root'})
        hidden = folder_model.create({'name': 'CA hidden parent', 'parent_id': root.id})
        child = folder_model.create({
            'name': 'CA visible child', 'parent_id': hidden.id,
            'inherit_access': False,
        })
        document = self.env['woow.document'].sudo().create({
            'name': 'ca-panel.txt', 'folder_id': child.id,
            'datas': base64.b64encode(b'panel'),
        })
        user = self.env['res.users'].sudo().create({
            'name': 'CA panel reader', 'login': 'ca-panel-reader',
            'groups_id': [Command.set([
                self.env.ref('woow_document_management.group_woow_document_user').id])],
        })
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': child.id, 'subject_type': 'user',
            'user_id': user.id, 'level': 'read',
        })
        self.env.flush_all()
        self.assertFalse(hidden.with_user(user).has_access('read'))
        self.assertTrue(child.with_user(user).has_access('read'))
        self.assertTrue(document.with_user(user).has_access('read'))
        self.assertEqual(self.env['woow.document'].with_user(user).search([
            ('id', '=', document.id)]).ids, [document.id])
        view = self.env.ref('woow_document_management.view_woow_document_document_search')
        self.assertIn('hierarchize="0"', view.arch_db)
        result = self.env['woow.document'].with_user(user).search_panel_select_range(
            'folder_id', search_domain=[('id', '=', document.id)],
            hierarchize=False,
        )
        self.assertNotIn('CA hidden parent', json.dumps(result, ensure_ascii=False))
        self.assertIn(child.id, [value['id'] for value in result['values']])

    def test_ca29_clean_session_is_not_persisted(self):
        """CA-29: _save_session saves only after a session mutation."""
        session = http.Session({'grant': {'id': 1}}, 'ca-session')
        store = Mock()
        future = Mock()
        request = SimpleNamespace(
            session=session, cookies={'session_id': session.sid},
            future_response=future, env=self.env,
        )
        with patch.object(http.root, 'session_store', store), patch.object(
            http, 'get_session_max_inactivity', return_value=3600,
        ):
            http.Request._save_session(request)
            store.save.assert_not_called()
            future.set_cookie.assert_not_called()
            session['grant'] = {'id': 2}
            http.Request._save_session(request)
            store.save.assert_called_once_with(session)
            future.set_cookie.assert_called_once()

    def test_ca34_portal_thread_fallback_exists(self):
        """T-CA-34: core portal thread access accepts valid token or hash/pid."""
        folder = self.env['woow.document.folder'].sudo().create({'name': 'CA fallback'})
        document = self.env['woow.document'].sudo().create({
            'name': 'ca-fallback.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'fallback'),
        })
        portal = self.env['res.users'].sudo().create({
            'name': 'CA fallback portal', 'login': 'ca-fallback-portal',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        model = self.env['woow.document'].with_user(portal)
        self.assertFalse(model.browse(document.id).has_access('read'))
        # Call the core portal implementation directly: the addon deliberately
        # overrides this method to deny these fallback credentials.
        with patch.object(type(model), '_mail_post_token_field', 'name'):
            get_thread = PortalMailThread._get_thread_with_access
            self.assertFalse(get_thread(model, document.id, token='wrong'))
            self.assertEqual(get_thread(model, document.id,
                                        token=document.name).id, document.id)
            signed = document._sign_token(portal.partner_id.id)
            self.assertFalse(get_thread(model, document.id,
                                        hash='wrong', pid=portal.partner_id.id))
            self.assertEqual(get_thread(model, document.id, hash=signed,
                                        pid=portal.partner_id.id).id, document.id)

    def test_ca31_read_group_formats_many2one_label(self):
        """CA-31: read_group returns the category's display label."""
        partner = self.env['res.partner'].sudo().create({'name': 'CA group parent'})
        child = self.env['res.partner'].sudo().create({
            'name': 'CA group child', 'parent_id': partner.id,
        })
        groups = self.env['res.partner'].sudo().read_group(
            [('id', '=', child.id)], ['parent_id'], ['parent_id'], lazy=False,
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['parent_id'], (partner.id, partner.display_name))


@tagged('-at_install', 'post_install', 'woow_docmgmt_core_assumptions')
class TestCoreAssumptionsHttp(HttpCase):

    def _rotated_session_response(self):
        self.authenticate('admin', 'admin')
        previous_sid = self.session.sid
        response = self.url_open(
            '/web/session/logout?redirect=/web/login', allow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        cookie = response.headers.get('Set-Cookie', '')
        self.assertIn('session_id=', cookie)
        return previous_sid, response, cookie

    def test_ca15_should_rotate_changes_sid(self):
        """CA-15: share grant sets the flag and the same response persists a new SID."""
        admin = self.env.ref('base.user_admin')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'CA rotation folder', 'owner_id': admin.id,
        })
        self.env['woow.document'].sudo().create({
            'name': 'ca-rotation.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'rotation'),
        })
        share, code = self.env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id,
            'expire_at': fields.Datetime.now() + timedelta(days=1),
        })
        self.authenticate('admin', 'admin')
        previous_sid = self.session.sid
        landing = self.url_open('/woow-docs/s')
        self.assertEqual(landing.status_code, 200)
        csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', landing.content)
        self.assertIsNotNone(csrf)

        seen = []
        save_session = http.Request._save_session

        def observe_save(request):
            if request.httprequest.path == '/woow-docs/s/open':
                seen.append((request.session.sid, request.session.should_rotate))
            return save_session(request)

        with patch.object(http.Request, '_save_session', observe_save):
            response = self.url_open('/woow-docs/s/open', data={
                'token': code, 'csrf_token': csrf.group(1).decode(),
            }, allow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(seen, [(previous_sid, True)])
        cookie = response.headers.get('Set-Cookie', '')
        match = re.search(r'(?:^|[, ])session_id=([^;]+)', cookie)
        self.assertIsNotNone(match, cookie)
        new_sid = match.group(1)
        self.assertNotEqual(new_sid, previous_sid)
        stored = http.root.session_store.get(new_sid)
        self.assertEqual(stored.sid, new_sid)
        self.assertEqual(stored[SESSION_GRANTS_KEY][str(share.id)]['h'],
                         share.token_hash[:16])
        self.assertFalse(http.root.session_store.get(previous_sid).get(SESSION_GRANTS_KEY))

    def test_ca16_session_cookie_flags(self):
        """T-CA-16: Odoo's session cookie is HttpOnly without Secure."""
        _previous_sid, _response, cookie = self._rotated_session_response()
        attributes = {part.strip().lower().split('=', 1)[0]
                      for part in cookie.split(';')[1:]}
        self.assertIn('httponly', attributes)
        self.assertNotIn('secure', attributes)

    def test_ca20_json_route_no_csrf_get_param_acl(self):
        """T-CA-20: JSON POST needs no CSRF; get_param enforces read ACL."""
        key = 'woow_document_management.ca20_probe'
        self.env['ir.config_parameter'].sudo().set_param(key, 'ca20-value')
        public = self.env.ref('base.public_user')
        parameters = self.env['ir.config_parameter'].with_user(public)
        with self.assertRaises(AccessError):
            parameters.get_param(key)
        self.assertEqual(parameters._get_param(key), 'ca20-value')

        self.authenticate('admin', 'admin')
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call',
                              'params': {}, 'id': 'ca20'})
        response = self.url_open('/web/session/get_session_info', data=payload,
                                 headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertNotIn('error', result)
        self.assertEqual(result['result']['uid'], self.env.ref('base.user_admin').id)

    def test_ca32_mail_upload_checks_thread_access_before_attachment(self):
        """CA-32: the mail upload route rejects an inaccessible thread."""
        folder = self.env['woow.document.folder'].sudo().create({'name': 'CA mail folder'})
        document = self.env['woow.document'].sudo().create({
            'name': 'ca-mail.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'mail'),
        })
        portal = self.env['res.users'].sudo().create({
            'name': 'CA portal user', 'login': 'ca-mail-portal',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })

        def csrf_token():
            secret = self.env['ir.config_parameter'].sudo().get_param('database.secret')
            expires = int(time.time() + 3600)
            message = ('%s%d' % (self.session.sid, expires)).encode()
            return hmac.new(secret.encode('ascii'), message, 'sha1').hexdigest() + 'o' + str(expires)

        self.authenticate('admin', 'admin')
        allowed = self.url_open('/mail/attachment/upload', data={
            'thread_model': 'woow.document', 'thread_id': str(document.id),
            'csrf_token': csrf_token(),
        }, files={'ufile': ('ca-allowed.txt', b'allowed', 'text/plain')})
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(self.env['ir.attachment'].sudo().search_count([
            ('res_model', '=', 'woow.document'), ('res_id', '=', document.id),
            ('name', '=', 'ca-allowed.txt'),
        ]), 1)

        self.authenticate(portal.login, portal.login)
        before = self.env['ir.attachment'].sudo().search_count([
            ('res_model', '=', 'woow.document'), ('res_id', '=', document.id),
            ('name', '=', 'ca-denied.txt'),
        ])
        response = self.url_open('/mail/attachment/upload', data={
            'thread_model': 'woow.document', 'thread_id': str(document.id),
            'csrf_token': csrf_token(),
        }, files={'ufile': ('ca-denied.txt', b'denied', 'text/plain')})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.env['ir.attachment'].sudo().search_count([
            ('res_model', '=', 'woow.document'), ('res_id', '=', document.id),
            ('name', '=', 'ca-denied.txt'),
        ]), before)

# Keep the separate CA-21 runner discoverable without changing tests/__init__.py.
from . import test_ca21_tour  # noqa: E402,F401
