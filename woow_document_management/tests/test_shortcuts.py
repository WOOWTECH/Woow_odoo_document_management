# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Shortcuts: a pointer to a file or a link of another folder, and what it never gives away."""
import base64
import io
import zipfile
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.shortcuts import SHORTCUT_CTX_KEY

_USER_GROUP = 'woow_document_management.group_woow_document_user'
_UNAVAILABLE = 'The target of this shortcut is not available.'


class ShortcutFixture:
    """Folders, people and documents shared by the shortcut tests.

    ``alice`` reads the source folder and uploads to the destination folder; ``bob`` only uploads
    to the destination folder (he cannot read the target); ``carol`` only reads both; ``erin`` has
    no access at all; ``portal`` reads the destination folder as a portal contact.
    """

    def _grant(self, folder, user, level):
        field = 'partner_id' if user.share else 'user_id'
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'partner' if user.share else 'user',
            field: user.partner_id.id if user.share else user.id, 'level': level})

    def _fixture(self):
        self.admin = self.env.ref('base.user_admin')
        folders = self.env['woow.document.folder'].sudo()
        self.src = folders.create({'name': 'Source', 'owner_id': self.admin.id})
        self.dest = folders.create({'name': 'Destination', 'owner_id': self.admin.id})
        self.elsewhere = folders.create({'name': 'Elsewhere', 'owner_id': self.admin.id})
        documents = self.env['woow.document'].sudo()
        self.target = documents.create({
            'name': 'pointer-target.txt', 'folder_id': self.src.id,
            'datas': base64.b64encode(b'target-content')})
        self.link = documents.create({
            'name': 'Reference link', 'folder_id': self.src.id, 'document_type': 'url',
            'url': 'https://example.test/page'})
        self.plain = documents.create({
            'name': 'plain.txt', 'folder_id': self.dest.id,
            'datas': base64.b64encode(b'plain-content')})
        self.alice = new_test_user(self.env, login='sc_alice', groups=_USER_GROUP)
        self.bob = new_test_user(self.env, login='sc_bob', groups=_USER_GROUP)
        self.carol = new_test_user(self.env, login='sc_carol', groups=_USER_GROUP)
        self.erin = new_test_user(self.env, login='sc_erin', groups=_USER_GROUP)
        self.portal = new_test_user(self.env, login='sc_portal', groups='base.group_portal')
        for user, folder, level in (
                (self.alice, self.src, 'read'), (self.alice, self.dest, 'upload'),
                (self.alice, self.elsewhere, 'upload'),
                (self.bob, self.dest, 'upload'),
                (self.carol, self.src, 'read'), (self.carol, self.dest, 'read'),
                (self.portal, self.dest, 'read')):
            self._grant(folder, user, level)
        self.env.flush_all()

    def _make_shortcut(self, user, target, folder):
        """The shortcut that ``user`` makes through the wizard (the way the button does)."""
        action = target.with_user(user).action_open_shortcut_wizard()
        wizard = self.env[action['res_model']].with_user(user).with_context(
            **action['context']).create({'folder_id': folder.id})
        wizard.action_create()
        return self.env['woow.document'].sudo().search([
            ('folder_id', '=', folder.id), ('shortcut_target_id', '=', target.id)],
            order='id desc', limit=1)


@tagged('woow_docmgmt_core')
class TestShortcuts(ShortcutFixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()

    # -- creation ---------------------------------------------------------------------------

    def test_a_shortcut_takes_the_name_of_its_target_and_belongs_to_the_caller(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        self.assertEqual(shortcut.document_type, 'shortcut')
        self.assertEqual(shortcut.shortcut_target_id, self.target)
        self.assertEqual(shortcut.name, 'pointer-target.txt')
        self.assertEqual(shortcut.folder_id, self.dest)
        self.assertEqual(shortcut.owner_id, self.alice)
        self.assertTrue(shortcut.with_user(self.alice).shortcut_target_available)
        log = self.env['woow.document.log'].sudo().search([
            ('document_id', '=', shortcut.id), ('event', '=', 'upload')])
        self.assertEqual(len(log), 1)
        self.assertEqual(log.detail, {'bytes': 0, 'shortcut': 1})

    def test_a_link_can_be_the_target(self):
        shortcut = self._make_shortcut(self.alice, self.link, self.dest)
        self.assertEqual(shortcut.shortcut_target_id, self.link)
        self.assertEqual(shortcut.name, 'Reference link')

    def test_the_name_is_not_synchronised_afterwards(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        self.target.sudo().write({'name': 'renamed-target.txt'})
        self.assertEqual(shortcut.name, 'pointer-target.txt')
        shortcut.with_user(self.alice).write({'name': 'my label.txt'})
        self.assertEqual(shortcut.name, 'my label.txt')
        self.assertEqual(self.target.name, 'renamed-target.txt')

    def test_a_reader_of_the_destination_cannot_create_one(self):
        wizard = self.env['woow.document.shortcut.wizard'].with_user(self.carol).create({
            'document_id': self.target.id, 'folder_id': self.dest.id})
        with self.assertRaises(AccessError):
            wizard.action_create()
        self.assertFalse(self.env['woow.document'].sudo().search([
            ('document_type', '=', 'shortcut')]))

    def test_a_caller_who_cannot_read_the_target_creates_nothing_and_learns_nothing(self):
        with self.assertRaises(AccessError):
            self.target.with_user(self.bob).action_open_shortcut_wizard()
        wizard = self.env['woow.document.shortcut.wizard'].with_user(self.bob).create({
            'document_id': self.target.id, 'folder_id': self.dest.id})
        with self.assertRaises(AccessError) as hidden:
            wizard.action_create()
        self.assertFalse(self.env['woow.document'].sudo().search([
            ('document_type', '=', 'shortcut')]))
        # a document that is in the trash gets the same answer as one that the rules hide
        self.target.with_user(self.admin).action_trash()
        wizard = self.env['woow.document.shortcut.wizard'].with_user(self.alice).create({
            'document_id': self.target.id, 'folder_id': self.dest.id})
        with self.assertRaises(AccessError) as trashed:
            wizard.action_create()
        self.assertEqual(str(hidden.exception), str(trashed.exception))

    def test_the_destination_must_be_a_live_folder_with_the_upload_level(self):
        wizards = self.env['woow.document.shortcut.wizard'].with_user(self.alice)
        for folder in (self.src, self.env['woow.document.folder'].sudo().create({
                'name': 'Hidden', 'owner_id': self.admin.id})):
            with self.subTest(folder=folder.name):
                with self.assertRaises(AccessError):
                    wizards.create({'document_id': self.target.id,
                                    'folder_id': folder.id}).action_create()
        self.dest.with_user(self.admin).action_trash()
        with self.assertRaises(AccessError):
            wizards.create({'document_id': self.target.id,
                            'folder_id': self.dest.id}).action_create()
        self.assertFalse(self.env['woow.document'].sudo().with_context(active_test=False).search([
            ('document_type', '=', 'shortcut')]))

    def test_a_shortcut_cannot_be_made_by_a_plain_create(self):
        documents = self.env['woow.document']
        values = {'name': 'direct.txt', 'folder_id': self.dest.id, 'document_type': 'shortcut',
                  'shortcut_target_id': self.target.id}
        for label, context in (('no marker', {}),
                               ('a flag a client could send', {SHORTCUT_CTX_KEY: True}),
                               ('another object', {SHORTCUT_CTX_KEY: object()})):
            with self.subTest(context=label), self.assertRaises(AccessError):
                documents.with_user(self.alice).with_context(**context).create(values)
        self.assertFalse(documents.sudo().search([('document_type', '=', 'shortcut')]))

    def test_only_a_shortcut_has_a_target_and_it_is_set_by_the_server(self):
        documents = self.env['woow.document'].with_user(self.alice)
        with self.assertRaises(AccessError):
            documents.create({'name': 'x.txt', 'folder_id': self.dest.id,
                              'datas': base64.b64encode(b'x'),
                              'shortcut_target_id': self.target.id})
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        with self.assertRaises(AccessError):
            shortcut.with_user(self.alice).write({'shortcut_target_id': self.link.id})
        with self.assertRaises(AccessError):
            self.plain.with_user(self.admin).write({'shortcut_target_id': self.target.id})
        self.assertEqual(shortcut.shortcut_target_id, self.target)

    def test_ambient_defaults_do_not_make_a_shortcut(self):
        document = self.env['woow.document'].with_user(self.alice).with_context(
            default_document_type='shortcut', default_shortcut_target_id=self.target.id).create({
                'name': 'defaults.txt', 'folder_id': self.dest.id,
                'datas': base64.b64encode(b'defaults')})
        self.assertEqual(document.document_type, 'binary')
        self.assertFalse(document.shortcut_target_id)

    def test_a_shortcut_points_to_a_file_or_a_link_of_the_same_company(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        # not to a shortcut: neither through the wizard nor under the superuser
        with self.assertRaises(UserError):
            shortcut.with_user(self.alice).action_open_shortcut_wizard()
        wizard = self.env['woow.document.shortcut.wizard'].with_user(self.alice).create({
            'document_id': shortcut.id, 'folder_id': self.elsewhere.id})
        with self.assertRaises(AccessError):
            wizard.action_create()
        documents = self.env['woow.document'].sudo()
        with self.assertRaises(AccessError):
            documents.create({'name': 'x', 'folder_id': self.dest.id, 'document_type': 'shortcut',
                              'shortcut_target_id': shortcut.id})
        with self.assertRaises(ValidationError):
            shortcut.write({'shortcut_target_id': shortcut.id})
        with self.assertRaises(ValidationError):
            self.plain.write({'shortcut_target_id': self.target.id})
        with self.assertRaises(ValidationError):
            shortcut.write({'shortcut_target_id': False})
        # nor to a document of another company
        company = self.env['res.company'].sudo().create({'name': 'Second company'})
        foreign = self.env['woow.document.folder'].sudo().create({
            'name': 'Foreign', 'owner_id': self.admin.id, 'company_id': company.id})
        with self.assertRaises(UserError):
            documents.create({'name': 'x', 'folder_id': foreign.id, 'document_type': 'shortcut',
                              'shortcut_target_id': self.target.id})

    def test_a_shortcut_is_not_duplicated(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        for user in (self.admin, self.env.user):
            with self.subTest(user=user.login), self.assertRaises(UserError):
                shortcut.with_user(user).copy()

    # -- the target stays behind the reader's own rights ----------------------------------------

    def test_a_reader_who_cannot_read_the_target_learns_nothing_about_it(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        documents = self.env['woow.document'].with_user(self.bob)
        mine = documents.browse(shortcut.id)
        # the baseline: the target itself is not readable for him
        with self.assertRaises(AccessError):
            documents.browse(self.target.id).read(['name'])
        self.assertEqual(mine.read(['name', 'shortcut_target_id'])[0]['shortcut_target_id'], False)
        self.assertEqual(mine.read(['shortcut_target_id'], load=None)[0]['shortcut_target_id'],
                         False)
        row = documents.search_read([('id', '=', shortcut.id)], ['shortcut_target_id'])[0]
        self.assertEqual(row['shortcut_target_id'], False)
        spec = {'shortcut_target_id': {'fields': {'display_name': {}}}, 'name': {}}
        self.assertEqual(mine.web_read(spec)[0]['shortcut_target_id'], False)
        found = documents.web_search_read([('id', '=', shortcut.id)], spec)
        self.assertEqual(found['records'][0]['shortcut_target_id'], False)
        self.assertFalse(mine.shortcut_target_available)
        with self.assertRaises(AccessError):
            mine.action_open_shortcut_target()

    def test_the_target_cannot_be_used_to_search_group_order_or_export(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        for user in (self.alice, self.bob):
            documents = self.env['woow.document'].with_user(user)
            with self.subTest(user=user.login):
                for domain in ([('shortcut_target_id', '=', self.target.id)],
                               [('shortcut_target_id.name', 'ilike', 'pointer')],
                               ['|', ('name', '=', 'x'), ('shortcut_target_id', '!=', False)]):
                    with self.assertRaises(AccessError):
                        documents.search(domain)
                    with self.assertRaises(AccessError):
                        documents.search_count(domain)
                with self.assertRaises(AccessError):
                    documents.search([], order='shortcut_target_id')
                with self.assertRaises(AccessError):
                    documents.read_group([], ['shortcut_target_id'], ['shortcut_target_id'])
                with self.assertRaises(AccessError):
                    documents._read_group([], aggregates=['shortcut_target_id:count_distinct'])
                with self.assertRaises(AccessError):
                    documents.browse(shortcut.id)._export_rows([['shortcut_target_id', 'id']])
        # a document administrator reads everything anyway
        found = self.env['woow.document'].with_user(self.admin).search([
            ('shortcut_target_id', '=', self.target.id)])
        self.assertEqual(found, shortcut)

    def test_a_reader_of_the_target_sees_it_and_can_open_it(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        mine = self.env['woow.document'].with_user(self.alice).browse(shortcut.id)
        self.assertEqual(mine.read(['shortcut_target_id'])[0]['shortcut_target_id'],
                         (self.target.id, 'pointer-target.txt'))
        spec = {'shortcut_target_id': {'fields': {'display_name': {}}}}
        self.assertEqual(mine.web_read(spec)[0]['shortcut_target_id'],
                         {'id': self.target.id, 'display_name': 'pointer-target.txt'})
        self.assertTrue(mine.shortcut_target_available)
        action = mine.action_open_shortcut_target()
        self.assertEqual((action['res_model'], action['res_id']), ('woow.document', self.target.id))
        # carol reads both folders: she sees it as well; the sharing is only through her own rights
        theirs = self.env['woow.document'].with_user(self.carol).browse(shortcut.id)
        self.assertTrue(theirs.shortcut_target_available)

    def test_a_trashed_target_is_unavailable_and_looks_like_a_hidden_one(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        mine = self.env['woow.document'].with_user(self.alice).browse(shortcut.id)
        self.target.with_user(self.admin).action_trash()
        mine.invalidate_recordset()
        self.assertFalse(mine.shortcut_target_available)
        self.assertEqual(mine.read(['shortcut_target_id'])[0]['shortcut_target_id'], False)
        with self.assertRaises(AccessError) as trashed:
            mine.action_open_shortcut_target()
        with self.assertRaises(AccessError) as hidden:
            self.env['woow.document'].with_user(self.bob).browse(
                shortcut.id).action_open_shortcut_target()
        self.assertEqual(str(trashed.exception), str(hidden.exception))
        self.assertIn(_UNAVAILABLE, str(trashed.exception))
        # the shortcut is still there, and works again when the target is restored
        self.target.with_user(self.admin).action_restore()
        mine.invalidate_recordset()
        self.assertTrue(mine.shortcut_target_available)
        self.assertEqual(mine.action_open_shortcut_target()['res_id'], self.target.id)

    def test_a_trashed_folder_of_the_target_makes_it_unavailable(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        self.src.with_user(self.admin).action_trash()
        mine = self.env['woow.document'].with_user(self.admin).browse(shortcut.id)
        mine.invalidate_recordset()
        self.assertFalse(mine.shortcut_target_available)
        with self.assertRaises(AccessError):
            mine.action_open_shortcut_target()

    def test_a_purged_target_takes_its_shortcuts_with_it(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        other = self._make_shortcut(self.alice, self.target, self.elsewhere)
        self.target.with_user(self.admin).action_trash()
        self.assertEqual(self.target.with_user(self.admin)._woow_purge(), (1, 0))
        self.env.invalidate_all()
        self.assertFalse(self.target.exists())
        self.assertFalse((shortcut | other).exists())
        self.assertTrue(self.plain.exists())

    def test_a_shortcut_is_trashed_restored_and_purged_on_its_own(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        mine = shortcut.with_user(self.alice)
        mine.action_trash()
        self.assertFalse(shortcut.active)
        with self.assertRaises(AccessError):
            mine.with_context(active_test=False).action_purge()
        mine.with_context(active_test=False).action_restore()
        self.assertTrue(shortcut.active)
        mine.action_trash()
        self.assertEqual(shortcut.with_user(self.admin)._woow_purge(), (1, 0))
        self.env.invalidate_all()
        self.assertFalse(shortcut.exists())
        self.assertTrue(self.target.exists())

    def test_a_shortcut_can_be_moved_and_keeps_its_target(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        wizard = self.env['woow.document.move.wizard'].with_user(self.alice).create({
            'document_ids': [(6, 0, shortcut.ids)], 'target_folder_id': self.elsewhere.id})
        wizard.action_move()
        self.assertEqual(shortcut.folder_id, self.elsewhere)
        self.assertEqual(shortcut.shortcut_target_id, self.target)

    # -- no content ----------------------------------------------------------------------------

    def test_a_shortcut_holds_no_content_and_counts_no_bytes(self):
        quota = type(self.env['woow.document.quota'])
        with patch.object(quota, '_check_many') as check, patch.object(quota, '_check') as one:
            shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        check.assert_not_called()
        one.assert_not_called()
        self.assertEqual(shortcut.file_size, 0)
        self.assertFalse(shortcut.sudo().attachment_id)
        self.assertFalse(shortcut.sudo().datas)
        self.assertFalse(self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document'), ('res_id', '=', shortcut.id)]))
        for values in ({'datas': base64.b64encode(b'new')}, {'url': 'https://example.test/x'}):
            with self.subTest(values=sorted(values)), self.assertRaises(Exception):
                shortcut.with_user(self.alice).write(values)
        with self.assertRaises(UserError):
            self.env['woow.document'].sudo().create({
                'name': 'x', 'folder_id': self.dest.id, 'document_type': 'shortcut',
                'shortcut_target_id': self.target.id, 'datas': base64.b64encode(b'x')})

    def test_the_members_notice_works_for_a_folder_that_holds_a_shortcut(self):
        self._make_shortcut(self.alice, self.target, self.dest)
        self.carol.partner_id.email = 'carol@example.test'
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.notice_base_url', 'https://docs.example.test')
        wizard = self.env['woow.document.notify.wizard'].with_user(self.admin).create({
            'folder_id': self.dest.id, 'all_members': False,
            'recipient_ids': [(6, 0, [self.carol.partner_id.id])]})
        self.assertEqual(wizard.action_send()['type'], 'ir.actions.act_window_close')

    # -- share users ---------------------------------------------------------------------------

    def test_share_users_never_see_a_shortcut(self):
        shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        documents = self.env['woow.document'].with_user(self.portal)
        self.assertEqual(documents.search([('folder_id', '=', self.dest.id)]), self.plain)
        self.assertFalse(documents.search([('id', '=', shortcut.id)]))
        self.assertEqual(documents.search_count([('document_type', '=', 'shortcut')]), 0)
        with self.assertRaises(AccessError):
            documents.browse(shortcut.id).read(['name'])
        with self.assertRaises(AccessError):
            documents.browse(shortcut.id).action_open_shortcut_target()
        with self.assertRaises(AccessError):
            documents.browse(self.plain.id).action_open_shortcut_wizard()
        with self.assertRaises(AccessError):
            documents.create({'name': 'x', 'folder_id': self.dest.id,
                              'document_type': 'shortcut',
                              'shortcut_target_id': self.plain.id})
        # a document shared directly with the contact does not make its shortcut visible
        self.plain.sudo().write({'shared_partner_ids': [(4, self.portal.partner_id.id)]})
        self.assertFalse(documents.search([('id', '=', shortcut.id)]))

    # -- views ---------------------------------------------------------------------------------

    def test_the_views_show_the_kind_and_the_open_button(self):
        views = self.env['woow.document'].with_user(self.alice).get_views(
            [(False, 'list'), (False, 'form'), (False, 'search')])['views']
        form = views['form']['arch']
        self.assertIn('action_open_shortcut_target', form)
        self.assertIn('action_open_shortcut_wizard', form)
        self.assertIn('document_type', form)
        lst = views['list']['arch']
        self.assertIn('action_open_shortcut_target', lst)
        self.assertIn('document_type', lst)
        self.assertIn('woow_shortcuts', views['search']['arch'])


@tagged('woow_docmgmt_http')
class TestShortcutRoutes(ShortcutFixture, HttpCase):

    def setUp(self):
        super().setUp()
        self._fixture()
        self.shortcut = self._make_shortcut(self.alice, self.target, self.dest)
        self.env.flush_all()

    def test_no_route_serves_a_shortcut_as_a_file(self):
        for user in (self.alice, self.bob, self.admin):
            login = user.login
            self.authenticate(login, 'admin' if user == self.admin else login)
            for path in ('/woow_document/doc/%d/download', '/woow_document/doc/%d/preview',
                         '/web/content/woow.document/%d/datas'):
                with self.subTest(user=login, path=path):
                    response = self.url_open(path % self.shortcut.id)
                    self.assertEqual(response.status_code, 404, response.text[:200])
                    self.assertNotIn(b'target-content', response.content)

    def test_the_zip_never_holds_a_shortcut_or_the_file_it_points_to(self):
        self.authenticate(self.alice.login, self.alice.login)
        folder = self.url_open('/woow_document/zip?folder_ids=%d' % self.dest.id)
        self.assertEqual(folder.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(folder.content)) as zipped:
            self.assertEqual(zipped.namelist(), ['Destination/plain.txt'])
        both = self.url_open('/woow_document/zip?document_ids=%d,%d' % (
            self.shortcut.id, self.plain.id))
        self.assertEqual(both.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(both.content)) as zipped:
            self.assertEqual(zipped.namelist(), ['Destination/plain.txt'])
        # a member who cannot read the target gets the same archive, with nothing of it
        self.authenticate(self.bob.login, self.bob.login)
        other = self.url_open('/woow_document/zip?folder_ids=%d' % self.dest.id)
        with zipfile.ZipFile(io.BytesIO(other.content)) as zipped:
            self.assertEqual(zipped.namelist(), ['Destination/plain.txt'])
        direct = self.url_open('/woow_document/doc/%d/download' % self.target.id)
        self.assertEqual(direct.status_code, 404)

    def test_the_portal_pages_do_not_list_a_shortcut(self):
        self.authenticate(self.portal.login, self.portal.login)
        page = self.url_open('/my/docs/folder/%d' % self.dest.id)
        self.assertEqual(page.status_code, 200, page.text[:200])
        self.assertIn(b'plain.txt', page.content)
        self.assertNotIn(b'pointer-target.txt', page.content)
        one = self.url_open('/my/docs/file/%d' % self.shortcut.id)
        self.assertEqual(one.status_code, 404)
        downloaded = self.url_open('/my/docs/file/%d/download' % self.shortcut.id)
        self.assertEqual(downloaded.status_code, 404)
