# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import secrets
from unittest.mock import patch

from odoo.tests import BaseCase, tagged

from ..services import tokens


@tagged('woow_docmgmt_tokens')
class TestShareTokens(BaseCase):

    def test_token_is_random_keyed_and_never_stored_plaintext(self):
        first, second = tokens.new_token(), tokens.new_token()
        self.assertRegex(first, tokens.TOKEN_RE)
        self.assertNotEqual(first, second)
        key, other_key = b'first-independent-key', b'second-independent-key'
        digest = tokens.token_hash(key, first)
        self.assertNotIn(first, digest)
        self.assertEqual(len(digest), 64)
        self.assertTrue(tokens.verify(key, first, digest))
        self.assertFalse(tokens.verify(other_key, first, digest))
        self.assertFalse(tokens.verify(key, second, digest))
        self.assertEqual(len(tokens.token_hint(other_key, first)), 6)

    def test_pasted_full_link_accepts_only_fragment_and_caps_length(self):
        code = tokens.new_token()
        self.assertEqual(tokens.extract_code('https://example.test/woow-docs/s#' + code), code)
        self.assertEqual(tokens.extract_code('  ' + code + '  '), code)
        self.assertIsNone(tokens.extract_code('https://example.test/woow-docs/s?code=' + code))
        self.assertIsNone(tokens.extract_code(('x' * 2049) + '#' + code))
        self.assertIsNone(tokens.extract_code(code + '\n' + code))

    def test_password_hash_is_salted_and_length_bounded(self):
        password = secrets.token_urlsafe(24)
        first = tokens.hash_password(password)
        second = tokens.hash_password(password)
        self.assertNotEqual(first, second)
        self.assertTrue(tokens.check_password(password, first))
        self.assertFalse(tokens.check_password(password + 'x', first))
        for bad in ('short', 'x' * 129):
            with self.assertRaises(ValueError):
                tokens.hash_password(bad)
        with patch.object(tokens._PASSWORD_CONTEXT, 'verify') as expensive:
            self.assertFalse(tokens.check_password('x' * 4096, first))
            expensive.assert_not_called()
