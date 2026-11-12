# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import secrets
from unittest.mock import patch

from odoo.tests import BaseCase, tagged

from ..services import tokens
from ..services.constants import PASSWORD_ROUNDS_MAX


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

    def test_a_stored_hash_with_more_rounds_than_the_module_makes_is_not_worked_on(self):
        """The rounds are part of the hash and passlib runs all of them when it checks a password.

        The limits of a crypt context apply when a hash is made, so the check of the rounds is the
        module's own: a hash beyond them is refused without a call to passlib, however many rounds
        it names (billions of them take minutes), while what the module makes is checked as before.
        """
        password = secrets.token_urlsafe(24)
        _empty, scheme, _rounds, salt, checksum = tokens.hash_password(password).split('$')

        def hashed_with(rounds):
            return '$'.join(['', scheme, str(rounds), salt, checksum])

        self.assertEqual(tokens.hash_password(password).split('$')[2], '25000')
        with patch.object(tokens._PASSWORD_CONTEXT, 'verify', return_value=True) as verify:
            for rounds in (1, 25000, PASSWORD_ROUNDS_MAX):
                with self.subTest(rounds=rounds):
                    verify.reset_mock()
                    self.assertTrue(tokens.check_password(password, hashed_with(rounds)))
                    verify.assert_called_once()
            for rounds in (PASSWORD_ROUNDS_MAX + 1, 10 ** 6, 2 ** 31 - 1, 2 ** 31, 2 ** 32 - 1,
                           2 ** 32, 10 ** 30, 0, -1, '07', 'x', ''):
                with self.subTest(rounds=rounds):
                    verify.reset_mock()
                    self.assertFalse(tokens.check_password(password, hashed_with(rounds)))
                    verify.assert_not_called()
            for malformed in ('', 'x', '$pbkdf2-sha512$', '$pbkdf2-sha256$25000$%s$%s' % (salt, checksum)):
                with self.subTest(hash=malformed):
                    verify.reset_mock()
                    self.assertFalse(tokens.check_password(password, malformed))
                    verify.assert_not_called()
        # without the stand-in the hash is still checked for real
        self.assertTrue(tokens.check_password(password, hashed_with(25000)))
        self.assertFalse(tokens.check_password(password, hashed_with(24999)))
        self.assertTrue(tokens.is_acceptable_hash(tokens.hash_password(password)))
        self.assertFalse(tokens.is_acceptable_hash(None))
