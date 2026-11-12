# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Public methods that any signed-in user can reach over RPC must not leak module data."""
import json
import secrets

from odoo import Command
from odoo.tests import HttpCase, TransactionCase, tagged

_PROXY = '203.0.113.77/32'
_FIELDS = ['woow_doc_trusted_proxies', 'woow_doc_denied_rate_per_min',
           'woow_doc_client_ip_header', 'woow_doc_zip_max_files']


def _settings_fixture(env):
    params = env['ir.config_parameter'].sudo()
    params.set_param('woow_document_management.trusted_proxies', _PROXY)
    params.set_param('woow_document_management.denied_rate_per_min', '17')
    params.set_param('woow_document_management.client_ip_header', 'X-Real-IP')


@tagged('woow_docmgmt_core')
class TestSettingsDefaultsOrm(TransactionCase):

    def setUp(self):
        super().setUp()
        _settings_fixture(self.env)
        partner = self.env['res.partner'].sudo().create({'name': 'Settings portal contact'})
        self.users = {
            'portal': self.env['res.users'].sudo().create({
                'name': 'Settings portal', 'login': 'wdoc-settings-portal',
                'partner_id': partner.id,
                'groups_id': [Command.set([self.env.ref('base.group_portal').id])]}),
            'internal': self.env['res.users'].sudo().create({
                'name': 'Settings internal', 'login': 'wdoc-settings-internal',
                'groups_id': [Command.set([self.env.ref('base.group_user').id])]}),
            'document user': self.env['res.users'].sudo().create({
                'name': 'Settings document user', 'login': 'wdoc-settings-docuser',
                'groups_id': [Command.set([
                    self.env.ref('woow_document_management.group_woow_document_user').id])]}),
        }

    def test_only_system_administrators_read_the_settings_through_defaults(self):
        admin = self.env.ref('base.user_admin')
        settings = self.env['res.config.settings']
        module_fields = [name for name in settings._fields if name.startswith('woow_doc_')]
        self.assertGreaterEqual(len(module_fields), 30)
        values = settings.with_user(admin).default_get(_FIELDS)
        self.assertEqual(values['woow_doc_trusted_proxies'], _PROXY)
        self.assertEqual(values['woow_doc_denied_rate_per_min'], 17)
        for name, user in self.users.items():
            with self.subTest(user=name):
                as_user = settings.with_user(user)
                self.assertEqual(as_user.default_get(_FIELDS), {})
                self.assertEqual(as_user.default_get(_FIELDS[:1]), {})
                # Naming no field, or every field of the module, answers none of them.
                self.assertFalse([key for key in as_user.default_get([]) if key.startswith('woow_doc_')])
                everything = as_user.default_get(module_fields)
                self.assertEqual(everything, {})
                self.assertNotIn(_PROXY, json.dumps(everything, default=str))

    def test_the_settings_form_defaults_stay_complete_for_the_administrator(self):
        admin = self.env.ref('base.user_admin')
        settings = self.env['res.config.settings']
        values = settings.with_user(admin).default_get(
            [name for name in settings._fields if name.startswith('woow_doc_')])
        for name in _FIELDS:
            self.assertIn(name, values)
        self.assertEqual(values['woow_doc_trusted_proxies'], _PROXY)


@tagged('woow_docmgmt_http')
class TestSettingsDefaultsRpc(HttpCase):

    def _call_kw(self, model, method, args, kwargs=None):
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 'defaults',
                              'params': {'model': model, 'method': method,
                                         'args': args, 'kwargs': kwargs or {}}})
        response = self.url_open('/web/dataset/call_kw/%s/%s' % (model, method),
                                 data=payload, headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_signed_in_users_cannot_read_the_settings_over_rpc(self):
        _settings_fixture(self.env)
        password = secrets.token_urlsafe(16)
        partner = self.env['res.partner'].sudo().create({'name': 'Settings rpc contact'})
        groups = {
            'portal': 'base.group_portal', 'internal': 'base.group_user',
            'document user': 'woow_document_management.group_woow_document_user'}
        spec = {name: {} for name in _FIELDS}
        for label, group in groups.items():
            user = self.env['res.users'].sudo().create({
                'name': 'Settings rpc %s' % label, 'login': 'wdoc-settings-rpc-%s' % label.replace(' ', '-'),
                'password': password, 'groups_id': [Command.set([self.env.ref(group).id])],
                **({'partner_id': partner.id} if label == 'portal' else {})})
            self.env.flush_all()
            self.authenticate(user.login, password)
            with self.subTest(user=label):
                answer = self._call_kw('res.config.settings', 'default_get', [_FIELDS])
                self.assertEqual(answer.get('result'), {}, answer)
                first = self._call_kw('res.config.settings', 'onchange', [[], {}, [], spec])
                self.assertNotIn(_PROXY, json.dumps(first))
        self.authenticate('admin', 'admin')
        answer = self._call_kw('res.config.settings', 'default_get', [_FIELDS])
        self.assertEqual(answer['result']['woow_doc_trusted_proxies'], _PROXY)
