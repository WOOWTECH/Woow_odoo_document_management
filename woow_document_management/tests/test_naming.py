# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Filename and ZIP path validation with traversal and Unicode edge cases."""

from odoo.tests import BaseCase, tagged

from ..services.mime_map import acceptable_mimes, is_compressed
from ..services.naming import dedupe, sanitize_filename, sanitize_segment


@tagged('woow_docmgmt_files')
class TestNaming(BaseCase):

    def test_filename_basename_unicode_and_reserved(self):
        self.assertEqual(sanitize_filename('..\\secret\\CON.pdf'), '_CON.pdf')
        self.assertEqual(sanitize_filename('e\u0301.txt'), '\u00e9.txt')
        self.assertEqual(sanitize_filename(' - .report\u202e.txt'), 'report.txt')
        self.assertEqual(sanitize_filename('a' * 250 + '.pdf'), 'a' * 196 + '.pdf')
        with self.assertRaises(ValueError):
            sanitize_filename('../...')

    def test_zip_segment_removes_traversal_and_limits_utf8(self):
        self.assertEqual(sanitize_segment('../folder\\name', 'Untitled'), 'foldername')
        self.assertEqual(sanitize_segment('\u202eevil', 'Untitled'), 'evil')
        self.assertEqual(sanitize_segment('..', 'Untitled'), 'Untitled')
        self.assertLessEqual(len(sanitize_segment('\u4e2d' * 100, 'Untitled').encode()), 255)

    def test_dedupe_casefold_suffix_before_extension(self):
        self.assertEqual(dedupe(['A.pdf', 'a.pdf', 'A (2).pdf', 'A.pdf']),
                         ['A.pdf', 'a (2).pdf', 'A (2) (2).pdf', 'A (3).pdf'])
        long = '中' * 83 + '.txt'
        result = dedupe([long, long, long])
        self.assertEqual(len({x.casefold() for x in result}), 3)
        self.assertTrue(all(len(x.encode()) <= 255 and x.endswith('.txt') for x in result))

    def test_mime_map_defaults(self):
        self.assertEqual(acceptable_mimes('.PDF'), frozenset({'application/pdf'}))
        self.assertFalse(acceptable_mimes('html'))
        self.assertTrue(is_compressed('image/png; charset=binary'))
        self.assertFalse(is_compressed('text/plain'))
