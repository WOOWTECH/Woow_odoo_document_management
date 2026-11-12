# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Filename and ZIP path validation with traversal and Unicode edge cases."""

import random

from odoo.tests import BaseCase, tagged

from ..services import naming
from ..services.mime_map import acceptable_mimes, is_compressed
from ..services.naming import Deduper, dedupe, sanitize_filename, sanitize_segment


def _reference_dedupe(names):
    """The first form of ``dedupe``: every name tries each candidate in turn."""
    seen = set()
    result = []
    for name in names:
        stem, dot, extension = name.rpartition('.')
        if not stem or not extension:
            stem, dot, extension = name, '', ''
        suffix = dot + extension
        candidate = naming._fit_segment(stem, '', suffix)
        index = 2
        while candidate.casefold() in seen:
            candidate = naming._fit_segment(stem, ' (%d)' % index, suffix)
            index += 1
        seen.add(candidate.casefold())
        result.append(candidate)
    return result


def _reference_level(names, taken=()):
    """How a ZIP level named its entries before: the unique names so far, then the new one."""
    prior = list(taken)
    result = []
    for name in names:
        unique = _reference_dedupe([*prior, name])[-1]
        prior.append(unique)
        result.append(unique)
    return result


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

    def test_the_incremental_deduper_names_files_like_the_whole_list_form(self):
        rng = random.Random(20261112)
        stems = ['a', 'A', 'b', 'a (2)', 'A (2)', 'a (3)', '\u4e2d' * 83, 'x' * 250, 'e\u0301', '']
        suffixes = ['', '.txt', '.TXT', '.pdf', ' (2).txt', '.tar.gz', '.']
        for _round in range(400):
            names = [rng.choice(stems) + rng.choice(suffixes) for _n in range(rng.randint(1, 40))]
            names = [name for name in names if name and name != '.']
            expected = _reference_dedupe(names)
            with self.subTest(names=names):
                self.assertEqual(dedupe(names), expected)
                deduper = Deduper()
                self.assertEqual([deduper.add(name) for name in names], expected)
                # A level that already holds a name (the links file of a ZIP) keeps it.
                self.assertEqual(
                    [Deduper(['_links.txt']).add(name) for name in ['_links.txt']],
                    ['_links (2).txt'])
                taken = Deduper(['_links.txt'])
                self.assertEqual([taken.add(name) for name in names + ['_links.txt']],
                                 _reference_level(names + ['_links.txt'], ['_links.txt']))

    def test_mime_map_defaults(self):
        self.assertEqual(acceptable_mimes('.PDF'), frozenset({'application/pdf'}))
        self.assertFalse(acceptable_mimes('html'))
        self.assertTrue(is_compressed('image/png; charset=binary'))
        self.assertFalse(is_compressed('text/plain'))
