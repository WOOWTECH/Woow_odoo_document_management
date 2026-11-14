# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The workspace screen: favourites, sections, views, the upload wizard and the menu."""
import base64
import json
from datetime import timedelta

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..services import locks
from ..services.constants import MIB, RECENT_DAYS

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_PARAMS = 'woow_document_management.'


def _b64(data):
    return base64.b64encode(data)


class _WorkspaceCase(TransactionCase):
    """One folder with members of every level, a portal contact and a user without any access."""

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = env.ref('base.user_admin')
        self.reader = new_test_user(env, login='ws_reader', groups=_USER)
        self.uploader = new_test_user(env, login='ws_uploader', groups=_USER)
        self.editor = new_test_user(env, login='ws_editor', groups=_USER)
        self.outsider = new_test_user(env, login='ws_outsider', groups=_USER)
        self.manager = new_test_user(env, login='ws_manager', groups=_MANAGER)
        self.plain = new_test_user(env, login='ws_plain', groups='base.group_user')
        self.portal = new_test_user(env, login='ws_portal', groups='base.group_portal')
        self.folder = env['woow.document.folder'].sudo().create({
            'name': 'Workspace folder', 'owner_id': self.admin.id})
        access = env['woow.document.folder.access'].sudo()
        for user, level in ((self.reader, 'read'), (self.uploader, 'upload'),
                            (self.editor, 'edit')):
            access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                           'user_id': user.id, 'level': level})
        access.create({'folder_id': self.folder.id, 'subject_type': 'partner',
                       'partner_id': self.portal.partner_id.id, 'level': 'read'})
        self.document = env['woow.document'].sudo().create({
            'name': 'workspace.txt', 'folder_id': self.folder.id, 'owner_id': self.editor.id,
            'datas': _b64(b'workspace body')})
        env.flush_all()
        self.documents = env['woow.document']
        self.favorites = env['woow.document.favorite']
        self.params = env['ir.config_parameter'].sudo()

    def _star(self, user, document=None):
        (document or self.document).with_user(user).action_toggle_favorite()


@tagged('woow_docmgmt_core')
class TestFavorites(_WorkspaceCase):

    def test_a_reader_can_star_without_any_write_right(self):
        self.assertFalse(self.document.with_user(self.reader).has_access('write'))
        self.assertFalse(self.document.with_user(self.reader).is_favorite)
        self._star(self.reader)
        rows = self.favorites.sudo().search([('document_id', '=', self.document.id)])
        self.assertEqual(rows.user_id, self.reader)
        self.assertEqual(rows.company_id, self.document.company_id)
        self.assertTrue(self.document.with_user(self.reader).is_favorite)

    def test_the_toggle_takes_the_star_away_again(self):
        self._star(self.reader)
        self._star(self.reader)
        self.assertFalse(self.favorites.sudo().search([('document_id', '=', self.document.id)]))
        self.assertFalse(self.document.with_user(self.reader).is_favorite)

    def test_the_stars_of_another_user_stay_invisible(self):
        self._star(self.reader)
        self._star(self.editor)
        rows = self.favorites.sudo().search([('document_id', '=', self.document.id)])
        mine = rows.filtered(lambda row: row.user_id == self.reader)
        for user in (self.editor, self.manager, self.outsider):
            with self.subTest(user=user.login):
                self.assertNotIn(mine, self.favorites.with_user(user).search([]))
                with self.assertRaises(AccessError):
                    mine.with_user(user).read(['document_id'])
                with self.assertRaises(AccessError):
                    mine.with_user(user).unlink()
        self.assertEqual(len(self.favorites.with_user(self.reader).search([])), 1)
        self.assertEqual(len(self.favorites.with_user(self.manager).search([])), 0)
        self.assertFalse(self.document.with_user(self.manager).is_favorite)
        self.assertEqual(self.documents.with_user(self.manager).search(
            [('is_favorite', '=', True)]), self.documents)
        self.assertEqual(self.documents.with_user(self.reader).search(
            [('is_favorite', '=', True)]), self.document)

    def test_a_star_is_not_edited_or_made_for_someone_else(self):
        self._star(self.reader)
        row = self.favorites.with_user(self.reader).search([])
        with self.assertRaises(AccessError):
            row.write({'user_id': self.editor.id})
        with self.assertRaises(AccessError):
            self.favorites.with_user(self.reader).create({
                'document_id': self.document.id, 'user_id': self.editor.id})
        with self.assertRaises(AccessError):
            self.favorites.with_user(self.editor).create({
                'document_id': self.document.id, 'company_id': self.env.company.id})
        with self.assertRaises(AccessError):
            self.document.with_user(self.editor).write({'favorite_ids': [Command.clear()]})
        self.assertEqual(len(self.favorites.sudo().search([])), 1)

    def test_a_duplicate_is_refused(self):
        self._star(self.reader)
        with self.assertRaises(UserError):
            self.favorites.with_user(self.reader).create({'document_id': self.document.id})
        with self.assertRaises(UserError):
            self.favorites.with_user(self.editor).create([
                {'document_id': self.document.id}, {'document_id': self.document.id}])
        # and the database holds the same rule
        self.env.cr.execute(
            "SELECT 1 FROM pg_constraint WHERE conrelid = 'woow_document_favorite'::regclass "
            "AND contype = 'u'")
        self.assertTrue(self.env.cr.fetchone())

    def test_a_document_that_cannot_be_read_cannot_be_starred(self):
        with self.assertRaises(AccessError) as hidden:
            self.favorites.with_user(self.outsider).create({'document_id': self.document.id})
        with self.assertRaises(AccessError) as missing:
            self.favorites.with_user(self.outsider).create({'document_id': 999999999})
        with self.assertRaises(AccessError) as bogus:
            self.favorites.with_user(self.outsider).create({'document_id': 'x'})
        self.assertEqual(str(hidden.exception), str(missing.exception))
        self.assertEqual(str(hidden.exception), str(bogus.exception))
        with self.assertRaises(AccessError):
            self.document.with_user(self.outsider).action_toggle_favorite()
        self.assertFalse(self.favorites.sudo().search([]))

    def test_a_document_in_the_trash_cannot_be_starred(self):
        self.document.with_user(self.editor).action_trash()
        with self.assertRaises(AccessError):
            self.favorites.with_user(self.reader).create({'document_id': self.document.id})

    def test_a_stored_default_does_not_choose_the_user(self):
        self.env['ir.default'].with_user(self.reader).set(
            'woow.document.favorite', 'user_id', self.editor.id, user_id=True)
        self._star(self.reader)
        self.assertEqual(self.favorites.sudo().search([]).user_id, self.reader)

    def test_a_batch_is_bounded(self):
        with self.assertRaises(UserError):
            self.favorites.with_user(self.reader).create(
                [{'document_id': self.document.id}] * 101)

    def test_portal_and_public_users_are_refused(self):
        self.assertTrue(self.document.with_user(self.portal).has_access('read'))
        public = self.env.ref('base.public_user')
        for user in (self.portal, public, self.plain):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self.document.with_user(user).action_toggle_favorite()
                with self.assertRaises(AccessError):
                    self.favorites.with_user(user).create({'document_id': self.document.id})
                with self.assertRaises(AccessError):
                    self.favorites.with_user(user).search([])
        for name in ('is_favorite', 'woow_section', 'favorite_ids'):
            with self.subTest(field=name), self.assertRaises(AccessError):
                self.documents.with_user(self.portal).search([(name, '!=', False)])
        with self.assertRaises(AccessError):
            self.document.with_user(self.portal).read(['is_favorite'])
        self.assertFalse(self.favorites.sudo().search([]))

    def test_the_star_columns_are_set_by_the_server(self):
        values = {'name': 'star.txt', 'folder_id': self.folder.id, 'datas': _b64(b'star')}
        for name in ('is_favorite', 'woow_section', 'woow_icon', 'favorite_ids'):
            with self.subTest(field=name):
                with self.assertRaises(AccessError):
                    self.documents.with_user(self.editor).create(dict(values, **{name: False}))
                with self.assertRaises(AccessError):
                    self.document.with_user(self.editor).write({name: False})

    def test_the_stars_go_with_the_document_or_the_user(self):
        self._star(self.reader)
        self.env.flush_all()
        self.env.invalidate_all()
        extra = new_test_user(self.env, login='ws_extra', groups=_USER)
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': extra.id,
            'level': 'read'})
        self._star(extra)
        self.assertEqual(len(self.favorites.sudo().search([])), 2)
        extra.sudo().unlink()
        self.assertEqual(self.favorites.sudo().search([]).user_id, self.reader)
        self.document.sudo().with_context(active_test=False).unlink()
        self.assertFalse(self.favorites.sudo().search([]))

    def test_the_search_of_is_favorite_refuses_odd_operators(self):
        documents = self.documents.with_user(self.reader)
        self._star(self.reader)
        other = self.env['woow.document'].sudo().create({
            'name': 'other.txt', 'folder_id': self.folder.id, 'owner_id': self.editor.id,
            'datas': _b64(b'other')})
        self.assertEqual(documents.search([('is_favorite', '=', True)]), self.document)
        self.assertEqual(documents.search([('is_favorite', '!=', False)]), self.document)
        self.assertEqual(documents.search([('is_favorite', '=', False)]), other)
        self.assertEqual(documents.search([('is_favorite', '!=', True)]), other)
        for operator, value in (('in', [True]), ('like', 'x'), ('=', 'yes'), ('=', 1)):
            with self.subTest(operator=operator), self.assertRaises(UserError):
                documents.search([('is_favorite', operator, value)])


@tagged('woow_docmgmt_core')
class TestSections(_WorkspaceCase):

    def setUp(self):
        super().setUp()
        self.old = self.env['woow.document'].sudo().create({
            'name': 'old.txt', 'folder_id': self.folder.id, 'owner_id': self.admin.id,
            'datas': _b64(b'old body')})
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE woow_document SET write_date = %s WHERE id = %s',
            (fields.Datetime.now() - timedelta(days=RECENT_DAYS + 1), self.old.id))
        self.env.invalidate_all()

    def _section(self, user, value, operator='='):
        return self.documents.with_user(user).search([('woow_section', operator, value)])

    def test_mine_is_the_documents_i_own(self):
        self.assertEqual(self._section(self.editor, 'mine'), self.document)
        self.assertEqual(self._section(self.reader, 'mine'), self.documents)
        self.assertEqual(self._section(self.manager, 'mine'), self.documents)

    def test_shared_is_the_documents_shared_with_me_directly(self):
        self.document.sudo().write({'shared_user_ids': [Command.set([self.reader.id])]})
        self.assertEqual(self._section(self.reader, 'shared'), self.document)
        self.assertEqual(self._section(self.editor, 'shared'), self.documents)

    def test_recent_is_what_changed_within_the_period(self):
        self.assertEqual(self._section(self.reader, 'recent'), self.document)
        self.document.sudo().with_context(active_test=False).write({'name': 'renamed.txt'})
        self.old.sudo().write({'name': 'old renamed.txt'})
        self.assertEqual(self._section(self.reader, 'recent'), self.document | self.old)

    def test_starred_is_my_own_stars(self):
        self._star(self.reader, self.old)
        self._star(self.editor)
        self.assertEqual(self._section(self.reader, 'starred'), self.old)
        self.assertEqual(self._section(self.editor, 'starred'), self.document)
        self.assertEqual(self._section(self.uploader, 'starred'), self.documents)

    def test_two_sections_are_joined_by_or(self):
        self._star(self.reader, self.old)
        self.assertEqual(self._section(self.reader, ['recent', 'starred'], 'in'),
                         self.document | self.old)
        self.assertEqual(self._section(self.reader, ['starred', 'starred'], 'in'), self.old)
        self.assertEqual(self._section(self.reader, [], 'in'), self.documents)

    def test_the_record_rules_always_apply(self):
        # The outsider owns and starred a document of a folder that nobody opened for them.
        hidden = self.env['woow.document'].sudo().create({
            'name': 'hidden.txt', 'folder_id': self.folder.id, 'owner_id': self.outsider.id,
            'datas': _b64(b'hidden')})
        self.assertFalse(hidden.with_user(self.outsider).has_access('read'))
        for value in ('mine', 'recent'):
            with self.subTest(section=value):
                self.assertNotIn(hidden, self._section(self.outsider, value))
        self.assertEqual(self._section(self.manager, 'mine'), self.documents)
        self.assertEqual(self.documents.with_user(self.outsider).search_count([]), 0)

    def test_other_operators_and_values_are_refused(self):
        for operator, value in (('!=', 'mine'), ('not in', ['mine']), ('like', 'mi'),
                                ('=', 'unknown'), ('=', False), ('in', ['mine', 'unknown']),
                                ('in', 'mine'), ('in', [['mine']])):
            with self.subTest(operator=operator, value=value), self.assertRaises(UserError):
                self._section(self.reader, value, operator)

    def test_a_section_is_only_a_search(self):
        field = self.documents._fields['woow_section']
        self.assertFalse(field.store)
        self.assertTrue(field.search)
        self.assertEqual(self.documents.with_user(self.reader).browse(self.document.id).woow_section,
                         False)


@tagged('woow_docmgmt_core')
class TestWorkspaceViews(_WorkspaceCase):

    def _views(self, user):
        action = self.env.ref('woow_document_management.action_woow_document_workspace')
        wanted = [(line.view_id.id, line.view_mode) for line in action.sudo().view_ids]
        wanted.append((action.search_view_id.id, 'search'))
        return self.documents.with_user(user).get_views(wanted)

    def test_the_views_load_for_a_document_user_and_a_manager(self):
        for user in (self.reader, self.uploader, self.manager):
            with self.subTest(user=user.login):
                views = self._views(user)['views']
                self.assertEqual(set(views), {'kanban', 'list', 'activity', 'form', 'search'})
                self.assertIn('woow_icon', views['kanban']['arch'])
                self.assertIn('is_favorite', views['list']['arch'])
                self.assertIn('searchpanel', views['search']['arch'])

    def test_the_action_has_the_domain_of_the_documents_action_and_its_four_views(self):
        action = self.env.ref('woow_document_management.action_woow_document_workspace')
        classic = self.env.ref('woow_document_management.action_woow_document_document')
        self.assertEqual(action.res_model, 'woow.document')
        self.assertEqual(action.view_mode, 'kanban,list,activity,form')
        self.assertEqual(action.domain, classic.domain)
        self.assertEqual(
            [line.view_mode for line in action.sudo().view_ids.sorted('sequence')],
            ['kanban', 'list', 'activity', 'form'])
        form = action.sudo().view_ids.filtered(lambda line: line.view_mode == 'form').view_id
        self.assertEqual(form, self.env.ref(
            'woow_document_management.view_woow_document_document_form'))

    def test_the_buttons_of_the_control_panel_open_actions_without_a_selection(self):
        for xml_id in ('view_woow_document_workspace_kanban', 'view_woow_document_workspace_list'):
            view = self.env.ref('woow_document_management.' + xml_id)
            buttons = etree.fromstring(view.arch_db.encode()).xpath('//header/button')
            self.assertEqual([button.get('string') for button in buttons],
                             ['Upload', 'New link', 'New folder', 'Trash'])
            for button in buttons:
                with self.subTest(view=xml_id, button=button.get('string')):
                    self.assertEqual(button.get('type'), 'action')
                    self.assertEqual(button.get('display'), 'always')
                    action = self.env['ir.actions.act_window'].browse(int(button.get('name')))
                    self.assertTrue(action.exists())
        link = self.env.ref('woow_document_management.action_woow_document_new_link')
        self.assertEqual(link.res_model, 'woow.document')
        self.assertIn("'default_document_type': 'url'", link.context)
        folder = self.env.ref('woow_document_management.action_woow_document_new_folder')
        self.assertEqual(folder.res_model, 'woow.document.folder')

    def test_a_link_form_starts_as_a_link_and_is_created_through_the_normal_guard(self):
        defaults = self.documents.with_user(self.uploader).with_context(
            default_document_type='url').default_get(['document_type'])
        self.assertEqual(defaults['document_type'], 'url')
        created = self.documents.with_user(self.uploader).create({
            'name': 'a link', 'folder_id': self.folder.id, 'document_type': 'url',
            'url': 'https://example.test/page'})
        self.assertEqual(created.document_type, 'url')
        self.assertEqual(created.woow_icon, 'fa-link')

    def test_the_search_panel_answers_for_a_document_user(self):
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'Workspace child', 'owner_id': self.admin.id, 'parent_id': self.folder.id})
        self.env['woow.document'].sudo().create({
            'name': 'child.txt', 'folder_id': child.id, 'owner_id': self.editor.id,
            'datas': _b64(b'child body')})
        tag = self.env['woow.document.tag'].sudo().create({'name': 'Workspace tag'})
        self.document.sudo().write({'tag_ids': [Command.set([tag.id])]})
        documents = self.documents.with_user(self.reader)
        tree = documents.search_panel_select_range(
            'folder_id', enable_counters=True, hierarchize=True)
        by_id = {value['id']: value for value in tree['values']}
        self.assertEqual(tree['parent_field'], 'parent_id')
        self.assertEqual(by_id[self.folder.id]['__count'], 2)
        self.assertEqual(by_id[child.id]['parent_id'], self.folder.id)
        sections = documents.search_panel_select_range('woow_section', expand=True)
        self.assertEqual([value['id'] for value in sections['values']],
                         ['mine', 'shared', 'recent', 'starred'])
        tags = documents.search_panel_select_multi_range('tag_ids', enable_counters=True)
        self.assertEqual([(value['id'], value['__count']) for value in tags['values']],
                         [(tag.id, 1)])

    def _restricted_tree(self):
        """folder (outsider reads) > hidden (outsider may not) > alone (outsider reads again)."""
        folders = self.env['woow.document.folder'].sudo()
        access = self.env['woow.document.folder.access'].sudo()
        top = folders.create({'name': 'Visible top', 'owner_id': self.admin.id,
                              'inherit_access': False})
        access.create({'folder_id': top.id, 'subject_type': 'user', 'user_id': self.outsider.id,
                       'level': 'read'})
        hidden = folders.create({'name': 'Hidden ancestor name', 'owner_id': self.admin.id,
                                 'parent_id': top.id, 'inherit_access': False})
        alone = folders.create({'name': 'Visible alone', 'owner_id': self.admin.id,
                                'parent_id': hidden.id, 'inherit_access': False})
        access.create({'folder_id': alone.id, 'subject_type': 'user', 'user_id': self.outsider.id,
                       'level': 'read'})
        below = folders.create({'name': 'Below alone', 'owner_id': self.admin.id,
                                'parent_id': alone.id})
        for folder, name in ((top, 'top'), (alone, 'alone'), (below, 'below')):
            self.env['woow.document'].sudo().create({
                'name': name + '.txt', 'folder_id': folder.id, 'owner_id': self.admin.id,
                'datas': _b64(b'body of ' + name.encode())})
        self.env.flush_all()
        self.assertFalse(hidden.with_user(self.outsider).has_access('read'))
        return top, hidden, alone, below

    def test_the_tree_keeps_a_folder_whose_parent_the_user_cannot_read(self):
        top, hidden, alone, below = self._restricted_tree()
        documents = self.documents.with_user(self.outsider)
        result = documents.search_panel_select_range(
            'folder_id', enable_counters=True, hierarchize=True)
        by_id = {value['id']: value for value in result['values']}
        self.assertEqual(set(by_id), {top.id, alone.id, below.id})
        # the folder under the hidden one hangs from the nearest folder that the user can see
        self.assertFalse(by_id[top.id]['parent_id'])
        self.assertEqual(by_id[alone.id]['parent_id'], top.id)
        self.assertEqual(by_id[below.id]['parent_id'], alone.id)
        self.assertEqual((by_id[top.id]['__count'], by_id[alone.id]['__count'],
                          by_id[below.id]['__count']), (3, 2, 1))
        # a user who sees nothing above makes it a root
        self.env['woow.document.folder.access'].sudo().search([
            ('folder_id', '=', top.id), ('user_id', '=', self.outsider.id)]).unlink()
        self.env.flush_all()
        result = documents.search_panel_select_range(
            'folder_id', enable_counters=True, hierarchize=True)
        by_id = {value['id']: value for value in result['values']}
        self.assertEqual(set(by_id), {alone.id, below.id})
        self.assertFalse(by_id[alone.id]['parent_id'])
        self.assertEqual((by_id[alone.id]['__count'], by_id[below.id]['__count']), (2, 1))

    def test_the_tree_names_no_folder_the_user_cannot_read(self):
        top, hidden, alone, below = self._restricted_tree()
        documents = self.documents.with_user(self.outsider)
        self.assertEqual(documents.search([('id', '!=', 0)]).folder_id, top | alone | below)
        for hierarchize, counters, expand in ((True, True, False), (True, False, False),
                                              (False, True, False), (True, True, True)):
            with self.subTest(hierarchize=hierarchize, counters=counters, expand=expand):
                result = documents.search_panel_select_range(
                    'folder_id', enable_counters=counters, hierarchize=hierarchize,
                    expand=expand)
                text = json.dumps(result, ensure_ascii=False)
                self.assertNotIn('Hidden ancestor name', text)
                ids = {value['id'] for value in result['values']}
                parents = {value.get('parent_id') for value in result['values']}
                self.assertNotIn(hidden.id, ids | parents)
                self.assertTrue({top.id, alone.id, below.id} <= ids)

    def test_the_tree_of_a_manager_is_the_plain_tree(self):
        top, hidden, alone, below = self._restricted_tree()
        result = self.documents.with_user(self.manager).search_panel_select_range(
            'folder_id', enable_counters=True, hierarchize=True)
        by_id = {value['id']: value for value in result['values']}
        self.assertEqual(by_id[hidden.id]['parent_id'], top.id)
        self.assertEqual(by_id[alone.id]['parent_id'], hidden.id)

    def test_the_form_hides_the_field_of_the_other_kind(self):
        arch = self.documents.with_user(self.reader).get_views([(False, 'form')])[
            'views']['form']['arch']
        self.assertIn('action_toggle_favorite', arch)
        form = self.env.ref('woow_document_management.view_woow_document_document_form_workspace')
        self.assertIn("document_type == 'url'", form.arch_db)


@tagged('woow_docmgmt_core')
class TestMenu(_WorkspaceCase):

    def test_the_documents_entry_opens_the_workspace_first(self):
        menu = self.env.ref('woow_document_management.menu_woow_document_document')
        action = self.env.ref('woow_document_management.action_woow_document_workspace')
        root = self.env.ref('woow_document_management.menu_woow_document_root')
        self.assertEqual(menu.action, action)
        children = self.env['ir.ui.menu'].with_context(**{'ir.ui.menu.full_list': True}).search(
            [('parent_id', '=', root.id)])
        self.assertEqual(children.sorted(lambda item: (item.sequence, item.id))[0], menu)
        self.assertEqual(menu.parent_id, root)
        # every other entry of the application is still there
        for name in ('folder', 'attachment_overview', 'trash', 'deleted_files', 'config'):
            self.assertIn(self.env.ref('woow_document_management.menu_woow_document_' + name),
                          children)

    def test_the_menu_loads_for_a_document_user(self):
        menu = self.env.ref('woow_document_management.menu_woow_document_document')
        action = self.env.ref('woow_document_management.action_woow_document_workspace')
        loaded = self.env['ir.ui.menu'].with_user(self.reader).load_menus(False)
        self.assertEqual(loaded[menu.id]['action'], 'ir.actions.act_window,%d' % action.id)
        root = self.env.ref('woow_document_management.menu_woow_document_root')
        self.assertEqual(loaded[root.id]['children'][0], menu.id)


@tagged('woow_docmgmt_core')
class TestUploadWizard(_WorkspaceCase):

    def _wizard(self, user, lines, folder=None, tags=()):
        return self.env['woow.document.upload.wizard'].with_user(user).create({
            'folder_id': (folder or self.folder).id,
            'tag_ids': [Command.set([tag.id for tag in tags])],
            'line_ids': [Command.create({'name': name, 'datas': _b64(data)})
                         for name, data in lines]})

    def _made(self, *names):
        return self.documents.sudo().search([('folder_id', '=', self.folder.id),
                                             ('name', 'in', list(names))])

    def test_an_upload_makes_one_document_per_file_with_the_tags(self):
        tag = self.env['woow.document.tag'].sudo().create({'name': 'Upload tag'})
        wizard = self._wizard(self.uploader, [('one.txt', b'one'), ('two.txt', b'two')],
                              tags=tag)
        result = wizard.action_upload()
        made = self._made('one.txt', 'two.txt')
        self.assertEqual(sorted(made.mapped('name')), ['one.txt', 'two.txt'])
        self.assertEqual(made.owner_id, self.uploader)
        self.assertEqual(made.tag_ids, tag)
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(result['params']['type'], 'success')
        self.assertFalse(self.env['woow.document.upload.wizard.line'].sudo().search(
            [('wizard_id', '=', wizard.id)]))
        events = self.env['woow.document.log'].sudo().search([
            ('event', '=', 'upload'), ('document_id', 'in', made.ids)])
        self.assertEqual(len(events), 2)
        self.assertEqual(set(events.mapped('channel')), {'backend'})

    def test_a_folder_without_the_upload_level_is_refused(self):
        wizard = self._wizard(self.reader, [('read.txt', b'read')])
        with self.assertRaises(AccessError):
            wizard.action_upload()
        self.assertFalse(self._made('read.txt'))
        # the folder of another company or one the user cannot see is refused alike
        hidden = self.env['woow.document.folder'].sudo().create({
            'name': 'Not for the uploader', 'owner_id': self.admin.id, 'inherit_access': False})
        wizard = self._wizard(self.uploader, [('hidden.txt', b'x')], folder=hidden)
        with self.assertRaises(AccessError):
            wizard.action_upload()
        self.assertFalse(self.documents.sudo().search([('name', '=', 'hidden.txt')]))

    def test_a_folder_in_the_trash_is_refused(self):
        wizard = self._wizard(self.uploader, [('late.txt', b'late')])
        self.folder.action_trash()
        with self.assertRaises(AccessError):
            wizard.action_upload()

    def test_a_refused_file_is_named_and_does_not_stop_the_others(self):
        wizard = self._wizard(self.uploader, [('good.txt', b'good'), ('bad.exe', b'MZ bad'),
                                              ('fine.txt', b'fine')])
        result = wizard.action_upload()
        self.assertEqual(sorted(self._made('good.txt', 'fine.txt', 'bad.exe').mapped('name')),
                         ['fine.txt', 'good.txt'])
        self.assertEqual(result['params']['type'], 'warning')
        self.assertIn('bad.exe', result['params']['message'])
        self.assertIn('1 refused', result['params']['message'])
        self.assertFalse(wizard.line_ids)

    def test_when_every_file_is_refused_the_error_names_them(self):
        wizard = self._wizard(self.uploader, [('bad.exe', b'MZ one'), ('worse.exe', b'MZ two')])
        with self.assertRaises(UserError) as caught:
            wizard.action_upload()
        self.assertIn('bad.exe', str(caught.exception))
        self.assertIn('worse.exe', str(caught.exception))
        self.assertFalse(self._made('bad.exe', 'worse.exe'))

    def test_a_file_without_a_name_or_content_is_refused_by_name(self):
        wizard = self.env['woow.document.upload.wizard'].with_user(self.uploader).create({
            'folder_id': self.folder.id,
            'line_ids': [Command.create({'name': 'empty.txt'}),
                         Command.create({'name': 'ok.txt', 'datas': _b64(b'ok')})]})
        result = wizard.action_upload()
        self.assertIn('empty.txt', result['params']['message'])
        self.assertTrue(self._made('ok.txt'))

    def test_the_quota_refusal_is_reported_by_name(self):
        self.params.set_param(_PARAMS + 'quota_mb', '1')
        wizard = self._wizard(self.uploader, [('big.txt', b'a' * (2 * MIB))])
        locks._reset_tracker(self.env.cr)     # the fixture took folder locks of a higher rank
        with self.assertRaises(UserError) as caught:
            wizard.action_upload()
        self.assertIn('big.txt', str(caught.exception))
        self.assertFalse(self._made('big.txt'))

    def test_the_number_of_files_follows_the_setting(self):
        self.params.set_param(_PARAMS + 'max_files_per_upload', '2')
        with self.assertRaises(UserError):
            self._wizard(self.uploader, [('a.txt', b'a'), ('b.txt', b'b'), ('c.txt', b'c')])
        wizard = self._wizard(self.uploader, [('a.txt', b'a'), ('b.txt', b'b')])
        with self.assertRaises(UserError):
            wizard.with_user(self.uploader).write({
                'line_ids': [Command.create({'name': 'c.txt', 'datas': _b64(b'c')})]})
        # a setting that is lowered after the files were added is applied on confirmation
        self.params.set_param(_PARAMS + 'max_files_per_upload', '1')
        with self.assertRaises(UserError):
            wizard.action_upload()
        self.assertFalse(self._made('a.txt', 'b.txt'))

    def test_a_file_above_the_size_limit_is_refused_before_it_is_kept(self):
        self.params.set_param(_PARAMS + 'internal_max_upload_mb', '1')
        with self.assertRaises(UserError):
            self._wizard(self.uploader, [('huge.txt', b'a' * (2 * MIB))])
        self.assertFalse(self.env['woow.document.upload.wizard.line'].sudo().search(
            [('name', '=', 'huge.txt')]))

    def test_nothing_to_upload_is_refused(self):
        wizard = self._wizard(self.uploader, [])
        with self.assertRaises(UserError):
            wizard.action_upload()

    def test_the_tags_must_be_available_to_the_user(self):
        other = self.env['res.company'].sudo().create({'name': 'Upload company B'})
        tag = self.env['woow.document.tag'].sudo().create({
            'name': 'Foreign tag', 'company_id': other.id})
        wizard = self._wizard(self.uploader, [('tag.txt', b'tag')], tags=tag)
        try:
            wizard.action_upload()
        except AccessError:
            pass
        # refused, or made without the tag that the user cannot see: never with it
        self.assertNotIn(tag, self._made('tag.txt').tag_ids)

    def test_a_wizard_and_its_files_belong_to_their_creator(self):
        wizard = self._wizard(self.uploader, [('mine.txt', b'mine')])
        for user in (self.editor, self.manager, self.outsider):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    wizard.with_user(user).action_upload()
                with self.assertRaises(AccessError):
                    wizard.with_user(user).read(['folder_id'])
                self.assertFalse(self.env['woow.document.upload.wizard.line'].with_user(
                    user).search([('wizard_id', '=', wizard.id)]))
        self.assertFalse(self._made('mine.txt'))

    def test_portal_and_plain_internal_users_cannot_use_the_wizard(self):
        for user in (self.portal, self.plain, self.env.ref('base.public_user')):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.env['woow.document.upload.wizard'].with_user(user).create({
                    'folder_id': self.folder.id})

    def test_the_bytes_of_a_file_wait_in_the_wizard_table_not_in_an_attachment(self):
        before = self.env['ir.attachment'].sudo().search_count([
            ('res_model', 'like', 'woow.document.upload')])
        wizard = self._wizard(self.uploader, [('wait.txt', b'wait')])
        self.assertEqual(self.env['ir.attachment'].sudo().search_count([
            ('res_model', 'like', 'woow.document.upload')]), before)
        self.assertEqual(wizard.line_ids.datas, _b64(b'wait'))

    def test_the_action_and_the_view_of_the_wizard_exist(self):
        action = self.env.ref('woow_document_management.action_woow_document_upload_wizard')
        self.assertEqual(action.res_model, 'woow.document.upload.wizard')
        self.assertEqual(action.target, 'new')
        views = self.env['woow.document.upload.wizard'].with_user(self.uploader).get_views(
            [(action.view_id.id, 'form')])
        self.assertIn('action_upload', views['views']['form']['arch'])


@tagged('-at_install', 'post_install', 'woow_docmgmt_workspace_tour')
class TestWorkspaceTour(HttpCase):

    def test_the_workspace_screen_works_in_the_browser(self):
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Tour folder'})
        self.env['woow.document'].sudo().create({
            'name': 'tour-workspace.txt', 'folder_id': folder.id,
            'datas': _b64(b'tour workspace body')})
        self.start_tour(
            '/odoo/action-woow_document_management.action_woow_document_workspace',
            'tour_woow_workspace', login='admin')
