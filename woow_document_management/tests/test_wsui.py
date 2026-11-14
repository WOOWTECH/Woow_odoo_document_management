# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The workspace screen in the browser (lane wsui): its views, its unit tests and its tour.

The screen is a convenience; it adds no right. These tests pin what the server gives it (the
views it is attached to, the folder tree, the fields it reads) and run the browser tests.
"""
import base64
import re
from pathlib import Path

from lxml import etree

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user

_USER = 'woow_document_management.group_woow_document_user'
_XMLID = 'woow_document_management.'
# The fields that the cards, the rows and the menu of the screen read from each record.
_SCREEN_FIELDS = (
    'name', 'document_type', 'mimetype', 'url', 'folder_id', 'is_favorite', 'is_locked',
    'woow_locked_by_other', 'woow_user_can_write', 'woow_user_can_share', 'version_count')
_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z9DwHwAGBQKA3H7sNwAAAABJRU5ErkJggg==')
_SRC = Path(__file__).resolve().parents[1] / 'static' / 'src' / 'backend' / 'wsui'


def _hash(text):
    """The id that the web client test runner gives to a suite of this name."""
    value = 0
    for char in text:
        value = ((value << 5) - value + ord(char)) & 0xFFFFFFFF
    return '%08x' % value


@tagged('woow_docmgmt_core')
class TestWorkspaceBrowserViews(TransactionCase):
    """What the server gives the screen: the classes of the views, the tree and the fields."""

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = env.ref('base.user_admin')
        self.reader = new_test_user(env, login='wsui_reader', groups=_USER)
        self.outsider = new_test_user(env, login='wsui_outsider', groups=_USER)
        self.portal = new_test_user(env, login='wsui_portal', groups='base.group_portal')
        folders = env['woow.document.folder'].sudo()
        access = env['woow.document.folder.access'].sudo()
        self.top = folders.create({'name': 'Wsui top', 'owner_id': self.admin.id,
                                   'inherit_access': False})
        access.create({'folder_id': self.top.id, 'subject_type': 'user',
                       'user_id': self.reader.id, 'level': 'read'})
        self.empty = folders.create({'name': 'Wsui empty child', 'owner_id': self.admin.id,
                                     'parent_id': self.top.id})
        self.hidden = folders.create({'name': 'Wsui hidden', 'owner_id': self.admin.id,
                                      'inherit_access': False})
        env['woow.document'].sudo().create({
            'name': 'wsui.txt', 'folder_id': self.top.id, 'owner_id': self.admin.id,
            'datas': base64.b64encode(b'wsui')})
        env.flush_all()
        self.documents = env['woow.document']

    def _views(self, user):
        action = self.env.ref(_XMLID + 'action_woow_document_workspace')
        wanted = [(line.view_id.id, line.view_mode) for line in action.sudo().view_ids]
        wanted.append((action.search_view_id.id, 'search'))
        return self.documents.with_user(user).get_views(wanted)['views']

    def test_the_kanban_and_the_list_get_their_browser_class_by_inherited_views(self):
        for name, js_class in (('kanban', 'woow_workspace_kanban'), ('list', 'woow_workspace_list')):
            base = self.env.ref(_XMLID + 'view_woow_document_workspace_' + name)
            attached = self.env.ref(_XMLID + 'view_woow_document_workspace_%s_wsui_class' % name)
            with self.subTest(view=name):
                self.assertEqual(attached.inherit_id, base)
                # that record only names the class and changes nothing else
                changes = etree.fromstring(attached.arch_db.encode()).xpath('//*')
                self.assertEqual([node.tag for node in changes], [name, 'attribute'])
                self.assertNotIn('js_class', base.arch_db)
                for user in (self.reader, self.admin):
                    arch = self._views(user)[name]['arch']
                    self.assertEqual(etree.fromstring(arch.encode()).get('js_class'), js_class)

    def test_the_views_carry_the_fields_the_screen_reads(self):
        for user in (self.reader, self.admin):
            views = self._views(user)
            for name in ('kanban', 'list'):
                names = {node.get('name') for node in etree.fromstring(
                    views[name]['arch'].encode()).iter('field')}
                with self.subTest(user=user.login, view=name):
                    self.assertTrue(set(_SCREEN_FIELDS) <= names, set(_SCREEN_FIELDS) - names)

    def test_the_card_has_a_picture_area_a_name_and_the_marks(self):
        arch = etree.fromstring(self._views(self.reader)['kanban']['arch'].encode())
        card = arch.xpath("//t[@t-name='card']")[0]
        self.assertTrue(card.xpath(".//div[contains(@class, 'woow-card-preview')]/img"
                                   "[@loading='lazy']"))
        self.assertTrue(card.xpath(".//field[@name='name']"))
        self.assertTrue(card.xpath(".//button[@name='action_toggle_favorite']"))
        self.assertTrue(card.xpath(".//*[contains(@t-if, 'is_locked')]"))
        self.assertTrue(card.xpath(".//*[contains(@t-if, 'version_count')]"))
        self.assertTrue(card.xpath(".//field[@name='owner_id']"))
        self.assertTrue(card.xpath(".//field[@name='tag_ids']"))
        self.assertTrue(card.xpath(".//field[@name='activity_ids']"))

    def test_the_folder_tree_holds_every_folder_the_user_may_open_empty_ones_too(self):
        search = etree.fromstring(self._views(self.reader)['search']['arch'].encode())
        node = search.xpath("//searchpanel/field[@name='folder_id']")[0]
        self.assertEqual(node.get('expand'), '1')
        params = {'enable_counters': True, 'hierarchize': True, 'expand': True,
                  'comodel_domain': [('in_trash', '=', False)]}
        tree = self.documents.with_user(self.reader).search_panel_select_range('folder_id', **params)
        by_id = {value['id']: value for value in tree['values']}
        self.assertIn(self.top.id, by_id)
        self.assertIn(self.empty.id, by_id)
        self.assertEqual(by_id[self.empty.id]['__count'], 0)
        self.assertEqual(by_id[self.empty.id]['parent_id'], self.top.id)
        # a folder that the user may not open is not in the tree, whatever its documents
        self.assertNotIn(self.hidden.id, by_id)
        self.assertNotIn('Wsui hidden', repr(tree))

    def test_a_folder_in_the_trash_is_not_in_the_tree(self):
        self.empty.with_user(self.admin).action_trash()
        params = {'enable_counters': True, 'hierarchize': True, 'expand': True,
                  'comodel_domain': [('in_trash', '=', False)]}
        tree = self.documents.with_user(self.reader).search_panel_select_range('folder_id', **params)
        self.assertNotIn(self.empty.id, [value['id'] for value in tree['values']])

    def test_the_folder_cards_read_the_folders_as_the_user(self):
        folders = self.env['woow.document.folder']
        fields = ['name', 'document_count', 'subfolder_count', 'color']
        rows = folders.with_user(self.reader).search_read(
            [('parent_id', '=', self.top.id), ('in_trash', '=', False)], fields)
        self.assertEqual([row['name'] for row in rows], ['Wsui empty child'])
        outsider = folders.with_user(self.outsider).search_read(
            [('id', 'in', [self.top.id, self.hidden.id])], fields)
        self.assertEqual(outsider, [])
        self.assertEqual(folders.with_user(self.reader).browse(self.top.id).read(
            ['woow_user_rank'])[0]['woow_user_rank'], 1)

    def test_a_portal_user_reads_nothing_through_the_screen(self):
        document = self.documents.search([('name', '=', 'wsui.txt')])
        portal_documents = self.documents.with_user(self.portal)
        self.assertEqual(portal_documents.search_count([]), 0)
        with self.assertRaises(AccessError):
            document.with_user(self.portal).read(['name'])
        # the marks of the cards are read by internal users only
        for name in ('is_favorite', 'is_locked', 'woow_locked_by_other', 'version_count',
                     'folder_id'):
            if name == 'folder_id':
                continue
            with self.subTest(field=name):
                self.assertEqual(self.documents._fields[name].groups, 'base.group_user')
        self.assertEqual(self.env['woow.document.folder'].with_user(self.portal).search_count(
            [('id', 'in', [self.top.id, self.hidden.id])]), 0)

    def test_the_trash_entry_opens_an_existing_action(self):
        self.assertTrue(self.env.ref(_XMLID + 'action_woow_document_trash_documents'))
        source = (_SRC / 'workspace_search_panel.js').read_text(encoding='utf-8')
        self.assertIn('woow_document_management.action_woow_document_trash_documents', source)

    def test_the_screen_adds_no_model_no_route_and_no_server_method(self):
        root = Path(__file__).resolve().parents[1]
        routes = (root / 'controllers').glob('*.py')
        self.assertEqual(sorted(path.name for path in routes),
                         ['__init__.py', 'main.py', 'portal.py', 'streaming.py', 'versions.py'])
        self.assertFalse((root / 'models' / 'wsui.py').exists())
        self.assertNotIn('wsui', {model for model in self.env.registry
                                  if model.startswith('woow.document')})

    def test_the_screen_calls_only_routes_of_this_module_and_core(self):
        calls = set()
        for path in _SRC.glob('*.js'):
            text = path.read_text(encoding='utf-8')
            calls.update(re.findall(r'["`](/[a-z_]+/[^"`$]*)', text))
        for call in calls:
            with self.subTest(call=call):
                self.assertTrue(call.startswith(('/woow_document/', '/web/static/lib/pdfjs/')),
                                call)

    def test_the_screen_builds_no_markup_from_data(self):
        patterns = (r'\.innerHTML', r'\.outerHTML', r'insertAdjacentHTML', r'\bmarkup\s*\(',
                    r'\bt-raw\b', r'document\.write', r'\beval\s*\(', r'new\s+Function',
                    r'window\.open', r'dangerouslySetInnerHTML', r't-out="[^"]*[Mm]arkup')
        files = list(_SRC.glob('*.js')) + list(_SRC.glob('*.xml'))
        self.assertGreaterEqual(len(files), 10)
        for path in files:
            text = path.read_text(encoding='utf-8')
            for pattern in patterns:
                with self.subTest(file=path.name, pattern=pattern):
                    self.assertFalse(re.search(pattern, text))
        # a record value in a URL goes through encodeURIComponent
        for path in _SRC.glob('*.js'):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if re.search(r'`/woow_document/[^`]*\$\{', line):
                    with self.subTest(file=path.name, line=number):
                        self.assertIn('encodeURIComponent', line)


@tagged('-at_install', 'post_install', 'woow_docmgmt_wsui_js')
class TestWorkspaceBrowserUnits(HttpCase):

    def test_the_unit_tests_of_the_workspace_screen_pass(self):
        suite = _hash('@woow_document_management')
        self.browser_js(
            '/web/tests?headless&loglevel=2&preset=desktop&timeout=15000&id=%s' % suite,
            '', '', login='admin', timeout=900,
            success_signal='[HOOT] Test suite succeeded',
            error_checker=lambda message: '[HOOT]' not in message)


@tagged('-at_install', 'post_install', 'woow_docmgmt_wsui_tour')
class TestWorkspaceBrowserTour(HttpCase):

    def test_the_workspace_screen_works_in_the_browser_from_folder_to_viewer(self):
        folders = self.env['woow.document.folder'].sudo()
        top = folders.create({'name': 'Tour projects'})
        folders.create({'name': 'Tour sub folder', 'parent_id': top.id})
        documents = self.env['woow.document'].sudo()
        documents.create({'name': 'tour-picture.png', 'folder_id': top.id,
                          'datas': base64.b64encode(_PNG)})
        documents.create({'name': 'tour-notes.txt', 'folder_id': top.id,
                          'datas': base64.b64encode(b'tour notes body')})
        self.start_tour(
            '/odoo/action-woow_document_management.action_woow_document_workspace',
            'tour_woow_workspace_screen', login='admin')
