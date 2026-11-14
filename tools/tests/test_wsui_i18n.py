# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The zh_TW fragment of the workspace screen lane covers every text of the screen.

Run with ``python3 -I -m unittest discover tools/tests`` from the repository root.
"""
import os
import re
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(TOOLS)
SRC = os.path.join(REPO, 'woow_document_management', 'static', 'src', 'backend', 'wsui')
VIEWS = os.path.join(REPO, 'woow_document_management', 'views', 'backend', 'wsui_views.xml')
CATALOG = os.path.join(REPO, 'dev', 'i18n', 'wsui.zh_TW.po')


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


class TestWorkspaceScreenTranslations(unittest.TestCase):

    def test_every_text_of_the_screen_has_a_zh_tw_term(self):
        catalog = _read(CATALOG)
        sources = set()
        for name in sorted(os.listdir(SRC)):
            text = _read(os.path.join(SRC, name))
            if name.endswith('.js'):
                sources.update(re.findall(r'_t\(\s*"([^"]+)"', text))
            elif name.endswith('.xml'):
                sources.update(item.strip() for item in re.findall(r'>([A-Z][A-Za-z ]+)<', text))
                sources.update(re.findall(r'(?:title|aria-label)="([A-Z][A-Za-z ]+)"', text))
                sources.update(re.findall(r'<div>([A-Z][^<]+)</div>', text))
        sources.update(re.findall(r'title="([A-Z][^"]+)"', _read(VIEWS)))
        self.assertGreater(len(sources), 30)
        for source in sorted(sources):
            with self.subTest(text=source):
                self.assertIn('msgid "%s"\n' % source, catalog)

    def test_the_terms_keep_their_placeholders_and_are_filled(self):
        catalog = _read(CATALOG)
        pairs = re.findall(r'^msgid "(.+)"\nmsgstr "(.*)"$', catalog, re.M)
        self.assertGreater(len(pairs), 30)
        placeholder = re.compile(r'%\([A-Za-z_][A-Za-z_0-9]*\)[a-z]|(?<!%)%[sd]')
        for source, target in pairs:
            with self.subTest(text=source):
                self.assertTrue(target.strip())
                self.assertEqual(sorted(placeholder.findall(source)),
                                 sorted(placeholder.findall(target)))


if __name__ == '__main__':
    unittest.main()
