# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import io
import zipfile
from unittest.mock import patch

from odoo.tests import BaseCase, tagged

from ..services import naming
from ..services.zip_builder import ZipEntry, ZipLimits, ZipLimitExceeded, build_zip
from .common import (names_cut_alike, names_cut_alike_from_ten, names_differing_in_case,
                     names_holding_the_numbers)


@tagged('woow_docmgmt_zip')
class TestZipBuilder(BaseCase):

    def test_preflight_limits_do_not_open_streams(self):
        opened = []
        entry = ZipEntry(('folder', 'one.txt'), lambda: opened.append(1), 5, True)
        for limits, code in ((ZipLimits(0, 100, 2), 'files'),
                             (ZipLimits(1, 4, 2), 'bytes'),
                             (ZipLimits(1, 100, 1), 'depth')):
            destination = io.BytesIO()
            with self.assertRaises(ZipLimitExceeded) as caught:
                build_zip([entry], limits, destination)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(destination.getvalue(), b'')
        self.assertFalse(opened)

    def test_duplicate_names_links_and_compression(self):
        entries = [
            ZipEntry(('folder', 'note.txt'), lambda: io.BytesIO(b'first'), 5, True),
            ZipEntry(('folder', 'NOTE.txt'), lambda: io.BytesIO(b'second'), 6, True),
            ZipEntry(('_links.txt',), lambda: io.BytesIO(b'local'), 5, False),
        ]
        destination = io.BytesIO()
        stats = build_zip(entries, ZipLimits(4, 1000, 2), destination,
                          links=['https://example.test/document'])
        self.assertEqual((stats.files, stats.links), (4, 1))
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(len(archive.namelist()), 4)
            self.assertEqual(archive.read('folder/note.txt'), b'first')
            self.assertEqual(archive.read('folder/NOTE (2).txt'), b'second')
            self.assertEqual(archive.read('_links (2).txt'), b'local')
            self.assertEqual(archive.read('_links.txt'), b'https://example.test/document\n')
            self.assertEqual(archive.getinfo('_links (2).txt').compress_type,
                             zipfile.ZIP_STORED)
            self.assertEqual(archive.getinfo('folder/note.txt').compress_type,
                             zipfile.ZIP_DEFLATED)

    def test_unique_names_cost_a_fit_per_entry_not_one_per_pair_of_entries(self):
        """Naming n files of one level costs a few fits per file, whatever their names are.

        Names that are spelled the same are not the only ones whose candidates ("name (2)",
        "name (3)", ...) are the same text: so are names that are cut to the same text (long
        names of two-byte letters), names that differ in case, and names that are cut alike
        from the number 10 on. A level of such names must not make each file try again what
        the files before it took (the cases are in ``tests/common.py``).
        """
        real = naming._fit_segment
        for label, leaves in (
                ('distinct long names',
                 ['%s-%04d.txt' % ('d' * 150, index) for index in range(1500)]),
                ('one name repeated', ['report.txt'] * 1500),
                ('numbered names that collide',
                 ['a (%d).txt' % (index % 7) for index in range(1500)]),
                ('cut to the same text, each twice', names_cut_alike(1000)),
                ('differing in case', names_differing_in_case(1500)),
                ('cut alike from 10 on', names_cut_alike_from_ten(50, 20)),
                ('numbers taken as names', names_holding_the_numbers(700))):
            calls = []

            def counting(*args, calls=calls, count=len(leaves)):
                calls.append(1)
                if len(calls) > 3 * count:
                    raise AssertionError('names are fitted again for every entry')
                return real(*args)

            entries = [ZipEntry(('folder', leaf), lambda: io.BytesIO(b'x'), 1, True)
                       for leaf in leaves]
            destination = io.BytesIO()
            with self.subTest(case=label):
                with patch.object(naming, '_fit_segment', counting):
                    build_zip(entries, ZipLimits(5000, 10 ** 9, 2), destination)
                with zipfile.ZipFile(destination) as archive:
                    names = [name.rsplit('/', 1)[-1] for name in archive.namelist()]
                self.assertEqual(names, naming.dedupe(leaves))
                self.assertEqual(len({name.casefold() for name in names}), len(leaves))

    def test_actual_stream_overrun_and_deadline(self):
        opened = []

        def stream():
            result = io.BytesIO(b'too long')
            opened.append(result)
            return result

        entry = ZipEntry(('file.txt',), stream, 3, True)
        with self.assertRaises(ZipLimitExceeded) as caught:
            build_zip([entry], ZipLimits(1, 100, 1), io.BytesIO())
        self.assertEqual(caught.exception.code, 'bytes')
        self.assertTrue(opened[0].closed)
        with patch('odoo.addons.woow_document_management.services.zip_builder.time.monotonic',
                   side_effect=[0, 2]):
            with self.assertRaises(ZipLimitExceeded) as caught:
                build_zip([entry], ZipLimits(1, 100, 1, deadline_s=1), io.BytesIO())
        self.assertEqual(caught.exception.code, 'deadline')
        self.assertEqual(len(opened), 1)

    def test_rejects_traversal_and_link_newlines(self):
        entry = ZipEntry(('..', 'file.txt'), lambda: io.BytesIO(b'x'), 1, True)
        with self.assertRaises(ZipLimitExceeded):
            build_zip([entry], ZipLimits(1, 100, 2), io.BytesIO())
        with self.assertRaises(ValueError):
            build_zip([], ZipLimits(1, 100, 1), io.BytesIO(),
                      links=['https://example.test/\nsecond'])
