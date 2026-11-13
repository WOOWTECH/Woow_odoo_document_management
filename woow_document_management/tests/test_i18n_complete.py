# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Check that the shipped Traditional Chinese catalog covers the exported terms."""
import re
from pathlib import Path

from babel.messages import pofile
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_i18n')
class TestTraditionalChineseCatalog(TransactionCase):

    def test_catalog_is_complete_and_keeps_interpolation_fields(self):
        root = Path(__file__).resolve().parents[1] / 'i18n'
        with (root / 'woow_document_management.pot').open('rb') as stream:
            template = pofile.read_po(stream, abort_invalid=True)
        with (root / 'zh_TW.po').open('rb') as stream:
            translated = pofile.read_po(stream, locale='zh_TW', abort_invalid=True)
        source = {message.id for message in template if isinstance(message.id, str) and message.id}
        localized = {message.id: message for message in translated
                     if isinstance(message.id, str) and message.id}
        self.assertGreaterEqual(len(source), 350)
        self.assertEqual(source, set(localized))
        placeholder = re.compile(r'%\([A-Za-z_][A-Za-z_0-9]*\)[a-z]|(?<!%)%[sd]')
        for msgid, message in localized.items():
            self.assertTrue(message.string.strip(), msgid)
            self.assertNotIn('fuzzy', message.flags, msgid)
            self.assertEqual(sorted(placeholder.findall(msgid)),
                             sorted(placeholder.findall(message.string)), msgid)
        self.assertEqual(localized['Documents'].string, '文件')
        self.assertEqual(localized['Woow Document Management'].string,
                         'WoowTech 文件管理')
