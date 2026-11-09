# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Proxy boundary tests for IF-IP (T-SET-20 through T-SET-25)."""

from types import SimpleNamespace

from odoo.tests import TransactionCase, tagged

from ..services.client_ip import (client_ip, hmac_ip, parse_trusted_proxies,
                                  request_client_ip, truncate_ip)


@tagged('woow_docmgmt_core')
class TestClientIp(TransactionCase):

    def test_untrusted_peer_ignores_spoofed_header(self):
        trusted = parse_trusted_proxies('10.0.0.0/8')
        env = {'REMOTE_ADDR': '198.51.100.10', 'HTTP_X_FORWARDED_FOR': '203.0.113.1'}
        self.assertEqual(client_ip(env, trusted, 'X-Forwarded-For'), '198.51.100.10')

    def test_trusted_peer_reads_single_value_header(self):
        trusted = parse_trusted_proxies('10.0.0.0/8, 2001:db8::/32')
        env = {'REMOTE_ADDR': '10.1.1.5', 'HTTP_CF_CONNECTING_IP': '2001:db8::123'}
        self.assertEqual(client_ip(env, trusted, 'CF-Connecting-IP'), '2001:db8::123')

    def test_xff_uses_first_untrusted_from_right(self):
        trusted = parse_trusted_proxies('10.0.0.0/8 172.16.0.0/12')
        env = {'REMOTE_ADDR': '10.0.0.1',
               'HTTP_X_FORWARDED_FOR': '198.51.100.7, 203.0.113.8, 172.16.2.1'}
        self.assertEqual(client_ip(env, trusted, 'X-Forwarded-For'), '203.0.113.8')

    def test_invalid_values_fall_back_to_peer(self):
        trusted = parse_trusted_proxies('10.0.0.0/8')
        env = {'REMOTE_ADDR': '10.0.0.1', 'HTTP_X_FORWARDED_FOR': 'bad, 203.0.113.9'}
        self.assertEqual(client_ip(env, trusted, 'X-Forwarded-For'), '10.0.0.1')
        env['HTTP_X_REAL_IP'] = '203.0.113.9, 203.0.113.10'
        self.assertEqual(client_ip(env, trusted, 'X-Real-IP'), '10.0.0.1')
        self.assertEqual(parse_trusted_proxies('10.0.0.0/8, invalid'), ())

    def test_request_uses_pre_proxyfix_peer(self):
        settings = SimpleNamespace(trusted_proxies=parse_trusted_proxies('10.0.0.0/8'),
                                   client_ip_header='X-Real-IP')
        request = SimpleNamespace(environ={
            'REMOTE_ADDR': '203.0.113.2',
            'HTTP_X_REAL_IP': '198.51.100.20',
            'werkzeug.proxy_fix.orig': {'REMOTE_ADDR': '10.0.0.2'},
        })
        self.assertEqual(request_client_ip(request, settings), '198.51.100.20')

    def test_truncate_and_hmac_ip(self):
        self.assertEqual(truncate_ip('192.0.2.42'), '192.0.2.0/24')
        self.assertEqual(truncate_ip('2001:db8::42'), '2001:db8::/64')
        self.assertEqual(truncate_ip('invalid'), '')
        self.assertEqual(len(hmac_ip(b'key', '192.0.2.42')), 64)
        self.assertNotEqual(hmac_ip(b'key', '192.0.2.42'), hmac_ip(b'key', '192.0.2.43'))
