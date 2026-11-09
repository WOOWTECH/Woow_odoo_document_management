# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

from odoo.tests import TransactionCase, tagged

from ..services import url_policy


@tagged('woow_docmgmt_core')
class TestURLPolicy(TransactionCase):

    def test_normal_http_urls(self):
        self.assertEqual(url_policy.validate('HTTPS://example.com/a?q=1'),
                         'https://example.com/a?q=1')
        self.assertTrue(url_policy.is_safe('https://example.com/a'))

    def test_script_and_encoded_schemes_rejected(self):
        for value in ('javascript:alert(1)', 'JaVaScRiPt:alert(1)',
                      'java%73cript:alert(1)', 'data:text/html,x',
                      'https%3A//example.com', 'file:///etc/passwd'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                url_policy.validate(value)

    def test_credentials_controls_and_relative_url_rejected(self):
        for value in ('https://user:pass@example.com/', 'https://example.com/\r\nX:1',
                      'https://example.com/%0d%0aX:1', 'https://%40example.com/',
                      'https://example.com\\evil.test/', '/docs/1',
                      'https://example.com:invalid/', 'https://example.com:0/'):
            with self.subTest(value=value):
                self.assertFalse(url_policy.is_safe(value))

    def test_mailto_requires_explicit_opt_in(self):
        self.assertFalse(url_policy.is_safe('mailto:user@example.com'))
        self.assertEqual(url_policy.validate('mailto:user@example.com', allow_mailto=True),
                         'mailto:user@example.com')
