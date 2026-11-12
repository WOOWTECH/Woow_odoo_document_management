# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Filename and ZIP path validation with traversal and Unicode edge cases."""

import random
from unittest.mock import patch

from odoo.tests import BaseCase, tagged

from ..services import naming
from ..services.mime_map import acceptable_mimes, is_compressed
from ..services.naming import Deduper, dedupe, sanitize_filename, sanitize_segment
from .common import (names_cut_alike, names_cut_alike_from_ten, names_differing_in_case,
                     names_holding_the_numbers)


class _ReferenceDeduper:
    """The first form of ``dedupe``: every name tries each candidate in turn, from number 2."""

    def __init__(self, taken=()):
        self._seen = {name.casefold() for name in taken}

    def add(self, name):
        stem, dot, extension = name.rpartition('.')
        if not stem or not extension:
            stem, dot, extension = name, '', ''
        suffix = dot + extension
        candidate = naming._fit_segment(stem, '', suffix)
        index = 2
        while candidate.casefold() in self._seen:
            candidate = naming._fit_segment(stem, ' (%d)' % index, suffix)
            index += 1
        self._seen.add(candidate.casefold())
        return candidate


def _reference_dedupe(names):
    reference = _ReferenceDeduper()
    return [reference.add(name) for name in names]


def _reference_level(names, taken=()):
    """How a ZIP level named its entries before: the unique names so far, then the new one."""
    prior = list(taken)
    result = []
    for name in names:
        unique = _reference_dedupe([*prior, name])[-1]
        prior.append(unique)
        result.append(unique)
    return result


def _outcome(deduper, names):
    """The names a deduper gives, and whether it refused one (a suffix too long for a number)."""
    result = []
    for name in names:
        try:
            result.append(deduper.add(name))
        except ValueError:
            return result, True
    return result, False


def _stem_near(rng, size, alphabet):
    """A stem of about ``size`` bytes (a few more or fewer) made of the given characters."""
    target = max(1, size + rng.randint(-8, 6))
    stem, total = [], 0
    while total < target:
        letter = rng.choice(alphabet)
        stem.append(letter)
        total += len(letter.encode('utf-8'))
    return ''.join(stem)


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

    def test_names_that_share_candidates_are_numbered_like_the_whole_list_form(self):
        """Names that differ in case, in what the cut drops, or in what is cut from 10 on.

        The characters have other widths or lengths when folded (sharp s, Kelvin sign, dotted
        capital I, final sigma, ...), the stems share long prefixes and end near the point where
        they are cut, lists run past 10 and 100 names of one name, and some suffixes leave room
        for one or two digits only (the name that has no room is refused, as the first form
        refuses it).
        """
        rng = random.Random(20261113)
        letters = ['a', 'A', 'b', 'B', '\u00df', '\u1e9e', 'ss', 'SS', '\u0130', 'i', 'I', '\u212a',
                   'k', 'K', '\u017f', 's', 'S', '\u03c2', '\u03c3', '\u03a3', '\u00e9', '\u00c9',
                   '\u01c5', '\u4e2d', '\U0001f600', '(', ')', ' ', '2', '10']
        suffixes = ['', '.txt', '.TXT', '.', ' (2).txt', '.\u212a', '.k', '.\u00df', '.ss',
                    '.' + 'x' * 30, '.' + '\u00e9' * 20, '.' + 'x' * 248, '.' + 'x' * 249,
                    '.' + '\u00e9' * 123]
        refused = 0
        for _round in range(250):
            suffix = rng.choice(suffixes)
            room = max(1, 255 - len(suffix.encode('utf-8')))
            alphabet = rng.sample(letters, rng.randint(2, 7))
            if rng.random() < 0.6:
                prefix = _stem_near(rng, room - rng.randint(2, 9), alphabet)
                stems = [prefix + ''.join(rng.choice(alphabet) for _n in range(rng.randint(0, 4)))
                         for _n in range(rng.randint(2, 9))]
            else:
                sizes = [room, room - 4, room - 5, room - 6, room + 2, 20]
                stems = [_stem_near(rng, rng.choice(sizes), alphabet)
                         for _n in range(rng.randint(1, 6))]
            pool = [stem + suffix for stem in stems + [s.swapcase() for s in stems]
                    + [s.upper() for s in stems]]
            pool += [stem + ' (%d)' % rng.choice([2, 3, 9, 10, 11, 99, 100]) + suffix
                     for stem in stems[:2]]
            names = [rng.choice(pool) for _n in range(rng.choice([5, 12, 25, 60, 130]))]
            taken = rng.sample(pool, 2) if rng.random() < 0.3 else []
            expected = _outcome(_ReferenceDeduper(taken), names)
            refused += expected[1]
            with self.subTest(round=_round):
                self.assertEqual(_outcome(Deduper(taken), names), expected)
        self.assertGreater(refused, 0)       # the refusals are part of what is compared
        # The same lists as the cost test below, small enough for the first form.
        for label, names in (
                ('cut to the same text', names_cut_alike(40)),
                ('differing in case', names_differing_in_case(100)),
                ('cut alike from 10 on', names_cut_alike_from_ten(6, 25)),
                ('numbers taken as names', names_holding_the_numbers(60))):
            with self.subTest(case=label):
                self.assertEqual(_outcome(Deduper(), names),
                                 (_reference_dedupe(names), False))

    def test_numbering_costs_a_few_fits_per_name_whatever_the_names_share(self):
        """Each name costs a few fits of its stem, however many names before it look like it.

        Names are fitted again for every candidate that an earlier name took, so what is
        remembered must be shared by every name that has the same candidates, not only by
        names that are spelled the same: stems that are cut to the same text (long names of
        two-byte letters), names that differ in case, stems that are cut alike only from the
        number 10 on, and a name whose numbers were taken as names of their own.
        """
        real = naming._fit_segment
        for label, names in (
                ('cut to the same text, each twice', names_cut_alike(1000)),
                ('differing in case', names_differing_in_case(1500)),
                ('cut alike from 10 on', names_cut_alike_from_ten(50, 20)),
                ('numbers taken as names', names_holding_the_numbers(700))):
            calls = []

            def counting(*args, calls=calls, count=len(names)):
                calls.append(1)
                if len(calls) > 3 * count:
                    raise AssertionError('names are fitted again for every name before them')
                return real(*args)

            with self.subTest(case=label):
                with patch.object(naming, '_fit_segment', counting):
                    deduper = Deduper()
                    result = [deduper.add(name) for name in names]
                self.assertEqual(len({name.casefold() for name in result}), len(names))
                self.assertTrue(all(len(name.encode()) <= 255 for name in result))

    def test_mime_map_defaults(self):
        self.assertEqual(acceptable_mimes('.PDF'), frozenset({'application/pdf'}))
        self.assertFalse(acceptable_mimes('html'))
        self.assertTrue(is_compressed('image/png; charset=binary'))
        self.assertFalse(is_compressed('text/plain'))
