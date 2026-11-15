# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The overview of the attachments that other apps own (DEC-17, amended on 2026-11-14).

T-ATT-01 to T-ATT-03 are the original cases; T-ATT-10 to T-ATT-19 cover the default visibility,
the source labels, the search on the source type, the open and download buttons and the filter.
"""
import ast
import base64
import datetime
from xml.etree import ElementTree

from dateutil.relativedelta import relativedelta

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools.safe_eval import safe_eval

MODULE = 'woow_document_management'
ACTION = MODULE + '.action_woow_document_attachment_overview'
MENU = MODULE + '.menu_woow_document_attachment_overview'
OVERVIEW_GROUP = MODULE + '.group_woow_document_attachment_overview'
USER_GROUP = MODULE + '.group_woow_document_user'


@tagged('woow_docmgmt_core')
class TestAttachmentOverview(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        users = cls.env['res.users']
        cls.doc_user = users.create({
            'name': 'Overview document user', 'login': 'wdoc-ov-doc',
            'groups_id': [Command.set([cls.env.ref(USER_GROUP).id])]})
        cls.overview_only = users.create({
            'name': 'Overview only user', 'login': 'wdoc-ov-only',
            'groups_id': [Command.set([cls.env.ref(OVERVIEW_GROUP).id])]})
        cls.plain_user = users.create({
            'name': 'Plain internal user', 'login': 'wdoc-ov-plain',
            'groups_id': [Command.set([cls.env.ref('base.group_user').id])]})

        partners = cls.env['res.partner']
        cls.partner = partners.create({'name': 'Overview source partner'})
        cls.hidden_partner = partners.create({'name': 'Overview hidden partner'})
        cls.env['ir.rule'].create({
            'name': 'Overview test: one hidden partner',
            'model_id': cls.env['ir.model']._get_id('res.partner'),
            'domain_force': "[('id', '!=', %d)]" % cls.hidden_partner.id,
            'perm_read': True, 'perm_write': True, 'perm_create': True, 'perm_unlink': True})

        attachments = cls.env['ir.attachment']
        data = base64.b64encode(b'overview fixture')

        def make(name, res_model, res_id, **extra):
            return attachments.create(dict(
                {'name': name, 'res_model': res_model, 'res_id': res_id, 'datas': data}, **extra))

        cls.att_partner = make('overview-partner.txt', 'res.partner', cls.partner.id)
        cls.att_partner_pdf = make('overview-partner.pdf', 'res.partner', cls.partner.id,
                                   mimetype='application/pdf')
        cls.att_hidden = make('overview-hidden.txt', 'res.partner', cls.hidden_partner.id)
        cls.att_hidden_public = make('overview-hidden-public.txt', 'res.partner',
                                     cls.hidden_partner.id, public=True)
        cls.att_gone = make('overview-gone.txt', 'res.partner', 987654321)
        cls.att_unknown = make('overview-unknown.txt', 'x_gone.model', 3)
        cls.att_no_id = make('overview-no-id.txt', 'res.partner', 0)
        cls.att_no_model = make('overview-no-model.txt', False, 0)
        cls.att_transient = make('overview-transient.txt', 'res.config.settings', 1)
        cls.att_abstract = make('overview-abstract.txt', 'mail.thread', 1)
        view = cls.env['ir.ui.view'].search([], limit=1)
        cls.att_view = make('overview-view.txt', 'ir.ui.view', view.id)
        cls.att_asset = attachments.create({
            'name': 'overview-asset.js', 'type': 'url', 'public': True,
            'url': '/web/assets/overview/overview-asset.js'})
        cls.att_compose = make('overview-compose.txt', 'mail.compose.message', 1)
        cls.fixtures = (
            cls.att_partner | cls.att_partner_pdf | cls.att_hidden | cls.att_hidden_public
            | cls.att_gone | cls.att_unknown | cls.att_no_id | cls.att_no_model
            | cls.att_transient | cls.att_abstract | cls.att_view | cls.att_asset
            | cls.att_compose)

    # ----- helpers ------------------------------------------------------------------------

    def _action(self):
        return self.env.ref(ACTION)

    def _action_domain(self):
        return ast.literal_eval(self._action().domain)

    def _filter_domain(self, name):
        arch = self.env['ir.attachment'].get_views(
            [(self._action().search_view_id.id, 'search')])['views']['search']['arch']
        for node in ElementTree.fromstring(arch).iter('filter'):
            if node.get('name') == name:
                return safe_eval(node.get('domain'), {
                    'uid': self.env.uid, 'context_today': datetime.date.today,
                    'relativedelta': relativedelta})
        self.fail('no filter named %s' % name)

    def _view_ids(self):
        """The ids of the views of the action, by kind, and of its search view."""
        action = self._action()
        ids = {line.view_mode: line.view_id.id for line in action.view_ids}
        ids['search'] = action.search_view_id.id
        return ids

    def _own(self, user):
        return self.env['ir.attachment'].with_user(user)

    # ----- T-ATT-01 to T-ATT-03 --------------------------------------------------------------

    def test_overview_equals_native_search(self):
        """T-ATT-01: what the overview lists is what the user's own search returns."""
        domain = self._action_domain()
        business = domain + self._filter_domain('woow_business_records')
        for user in (self.doc_user, self.overview_only, self.plain_user,
                     self.env.ref('base.user_admin')):
            attachments = self._own(user)
            for wanted in (domain, business):
                listed = attachments.web_search_read(wanted, {'name': {}})['records']
                self.assertEqual(
                    sorted(row['id'] for row in listed),
                    sorted(attachments.search(wanted).ids), user.login)
        seen = self._own(self.doc_user).search(business)
        self.assertIn(self.att_partner, seen)
        self.assertNotIn(self.att_hidden, seen)

    def test_overview_menu_and_readonly(self):
        """T-ATT-02: the menu is for the overview group and for document users; the views read only."""
        menu = self.env.ref(MENU)
        visible = {
            user: menu.id in self.env['ir.ui.menu'].with_user(user)._visible_menu_ids()
            for user in (self.doc_user, self.overview_only, self.plain_user)}
        self.assertEqual(
            visible, {self.doc_user: True, self.overview_only: True, self.plain_user: False})
        action_groups = self._action().groups_id
        for user, expected in ((self.doc_user, True), (self.overview_only, True),
                               (self.plain_user, False)):
            self.assertEqual(bool(action_groups & user.groups_id), expected, user.login)
        ids = self._view_ids()
        views = self.env['ir.attachment'].get_views(
            [(ids[kind], kind) for kind in ('list', 'form', 'kanban', 'search')])['views']
        for kind in ('list', 'form', 'kanban'):
            root = ElementTree.fromstring(views[kind]['arch'])
            self.assertEqual(
                (root.get('create'), root.get('edit'), root.get('delete')), ('0', '0', '0'), kind)
        # no new right: the groups of the overview have no ACL line and no record rule of their own
        groups = self.env.ref(OVERVIEW_GROUP) | self.env.ref(USER_GROUP)
        self.assertFalse(self.env['ir.model.access'].search([
            ('model_id.model', '=', 'ir.attachment'), ('group_id', 'in', groups.ids)]))
        self.assertFalse(self.env['ir.rule'].search([
            ('model_id.model', '=', 'ir.attachment'), ('groups', 'in', groups.ids)]))

    def test_overview_excludes_woow_files(self):
        """T-ATT-03: the files of this module's records are not in the overview."""
        folder = self.env['woow.document.folder'].create({'name': 'Overview folder'})
        document = self.env['woow.document'].create({
            'name': 'overview-doc.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'a file of the module')})
        attachments = self.env['ir.attachment'].with_context(skip_res_field_check=True)
        own = attachments.search([('res_model', '=', 'woow.document'), ('res_id', '=', document.id)])
        self.assertTrue(own)
        # the domain of the action leaves them out, and so does the native search without it
        self.assertFalse(attachments.search(self._action_domain() + [('id', 'in', own.ids)]))
        self.assertFalse(self.env['ir.attachment'].search(
            [('res_model', '=', 'woow.document'), ('res_id', '=', document.id)]))

    # ----- T-ATT-10: default visibility ------------------------------------------------------

    def test_document_user_gets_overview_group_by_default(self):
        """T-ATT-10: a document user is in the overview group without being given it."""
        overview = self.env.ref(OVERVIEW_GROUP)
        self.assertIn(overview, self.env.ref(USER_GROUP).implied_ids)
        self.assertTrue(self.doc_user.has_group(OVERVIEW_GROUP))
        self.assertTrue(self.overview_only.has_group(OVERVIEW_GROUP))
        self.assertFalse(self.overview_only.has_group(USER_GROUP))
        self.assertFalse(self.plain_user.has_group(OVERVIEW_GROUP))
        manager = self.env['res.users'].create({
            'name': 'Overview manager', 'login': 'wdoc-ov-manager',
            'groups_id': [Command.set([self.env.ref(MODULE + '.group_woow_document_manager').id])]})
        self.assertTrue(manager.has_group(OVERVIEW_GROUP))
        self.assertEqual(self.env.ref(MENU).sequence, 30)

    # ----- T-ATT-11, T-ATT-12, T-ATT-13: labels ----------------------------------------------

    def test_labels_follow_the_model_and_the_record(self):
        """T-ATT-11: the type is the name of the model and the record is its display name."""
        model_name = self.env['ir.model']._get('res.partner').name
        attachment = self.att_partner.with_user(self.doc_user)
        self.assertEqual(attachment.woow_source_model_label, model_name)
        self.assertEqual(attachment.woow_source_record_label, self.partner.display_name)
        self.assertEqual(self.att_view.woow_source_model_label,
                         self.env['ir.model']._get('ir.ui.view').name)

    def test_labels_fall_back_without_raising(self):
        """T-ATT-12: a record that is gone, a model that is gone, no id, no model, a transient or
        abstract model, and a record the user may not read give the specified fallbacks."""
        self.assertEqual(self.att_gone.woow_source_model_label,
                         self.env['ir.model']._get('res.partner').name)
        self.assertFalse(self.att_gone.woow_source_record_label)
        self.assertEqual(self.att_unknown.woow_source_model_label, 'x_gone.model')
        self.assertFalse(self.att_unknown.woow_source_record_label)
        self.assertEqual(self.att_no_id.woow_source_model_label,
                         self.env['ir.model']._get('res.partner').name)
        self.assertFalse(self.att_no_id.woow_source_record_label)
        self.assertFalse(self.att_no_model.woow_source_model_label)
        self.assertFalse(self.att_no_model.woow_source_record_label)
        self.assertFalse(self.att_transient.woow_source_record_label)
        self.assertFalse(self.att_abstract.woow_source_record_label)
        self.assertTrue(self.att_transient.woow_source_model_label)
        # a public file on a record that the user cannot read: the file is readable, the name not
        public = self.att_hidden_public.with_user(self.doc_user)
        self.assertEqual(public.woow_source_model_label,
                         self.env['ir.model']._get('res.partner').name)
        self.assertFalse(public.woow_source_record_label)
        # the whole set at once, in one call
        self.assertEqual(len(self.fixtures.mapped('woow_source_record_label')), len(self.fixtures))

    def test_record_labels_are_read_per_model(self):
        """T-ATT-13: more attachments of the same models do not cost more queries."""
        def cost(count):
            rows = self.env['ir.attachment'].create([{
                'name': 'overview-batch-%d.txt' % index, 'res_model': 'res.partner',
                'res_id': self.partner.id} for index in range(count)])
            self.env.invalidate_all()
            before = self.env.cr.sql_log_count
            rows.mapped('woow_source_model_label')
            rows.mapped('woow_source_record_label')
            return self.env.cr.sql_log_count - before
        small, large = cost(3), cost(60)
        self.assertLessEqual(large, small + 2, (small, large))

    # ----- T-ATT-14: search -----------------------------------------------------------------

    def test_search_by_the_name_of_the_source_type(self):
        """T-ATT-14: typing the model's name or its technical name finds its attachments."""
        attachments = self.env['ir.attachment']
        scope = [('id', 'in', self.fixtures.ids)]
        model_name = self.env['ir.model']._get('res.partner').name
        partner_files = self.att_partner | self.att_partner_pdf | self.att_hidden \
            | self.att_hidden_public | self.att_gone | self.att_no_id
        for operator, value in (('ilike', model_name.lower()), ('=', model_name),
                                ('ilike', 'res.partner'), ('in', [model_name])):
            found = attachments.search(scope + [('woow_source_model_label', operator, value)])
            self.assertEqual(found & partner_files, partner_files, (operator, value))
            self.assertFalse(found & (self.att_view | self.att_unknown), (operator, value))
        # a model that the registry does not hold is found by its technical name
        self.assertEqual(
            attachments.search(scope + [('woow_source_model_label', 'ilike', 'x_gone')]),
            self.att_unknown)
        # the other way round
        rest = attachments.search(scope + [('woow_source_model_label', '!=', model_name)])
        self.assertFalse(rest & partner_files)
        self.assertIn(self.att_view, rest)
        self.assertEqual(
            attachments.search(scope + [('woow_source_model_label', '=', False)]),
            self.att_no_model | self.att_asset)
        with self.assertRaises(UserError):
            attachments.search([('woow_source_model_label', '>', 'a')])

    # ----- T-ATT-15, T-ATT-16: buttons -------------------------------------------------------

    def test_open_source_returns_the_record_or_refuses(self):
        """T-ATT-15: the button opens the record; it refuses when it cannot be opened."""
        action = self.att_partner.with_user(self.doc_user).action_woow_open_source()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual((action['res_model'], action['res_id']), ('res.partner', self.partner.id))
        self.assertEqual(action['view_mode'], 'form')
        for attachment in (self.att_gone, self.att_unknown, self.att_no_id, self.att_no_model,
                           self.att_transient, self.att_abstract):
            with self.assertRaises(UserError, msg=attachment.name):
                attachment.action_woow_open_source()
        with self.assertRaises(UserError):
            self.att_hidden_public.with_user(self.doc_user).action_woow_open_source()
        with self.assertRaises(ValueError):  # ensure_one
            (self.att_partner | self.att_partner_pdf).action_woow_open_source()
        with self.assertRaises(AccessError):
            self.att_hidden.with_user(self.doc_user).action_woow_open_source()

    def test_download_action_points_at_the_content_route(self):
        """T-ATT-16: the download button goes to the core route, which checks the access."""
        action = self.att_partner.with_user(self.doc_user).action_woow_download()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(action['url'], '/web/content/%d?download=true' % self.att_partner.id)
        with self.assertRaises(AccessError):
            self.att_hidden.with_user(self.doc_user).action_woow_download()

    # ----- T-ATT-17, T-ATT-18: filter and views ----------------------------------------------

    def test_business_records_filter_leaves_out_technical_files(self):
        """T-ATT-17: the default filter keeps the files of business records only."""
        action = self._action()
        self.assertEqual(safe_eval(action.context).get('search_default_woow_business_records'), 1)
        attachments = self.env['ir.attachment']
        domain = self._action_domain() + self._filter_domain('woow_business_records')
        found = attachments.search(domain + [('id', 'in', self.fixtures.ids)])
        self.assertEqual(
            found, self.att_partner | self.att_partner_pdf | self.att_hidden
            | self.att_hidden_public | self.att_gone | self.att_unknown | self.att_transient
            | self.att_abstract)
        self.assertFalse(found & (self.att_view | self.att_asset | self.att_compose
                                  | self.att_no_id | self.att_no_model))
        # the other filters of the search view are well formed
        for name in ('woow_my_uploads', 'woow_images', 'woow_pdf', 'woow_last_30_days'):
            attachments.search(self._filter_domain(name), limit=1)
        pdf = attachments.search(self._filter_domain('woow_pdf') + [('id', 'in', self.fixtures.ids)])
        self.assertEqual(pdf, self.att_partner_pdf)

    def test_views_of_the_action_load(self):
        """T-ATT-18: list, kanban, form and search load for a document user."""
        action = self._action()
        self.assertEqual(action.view_mode, 'list,kanban,form')
        attachments = self.env['ir.attachment'].with_user(self.doc_user)
        ids = self._view_ids()
        views = attachments.get_views(
            [(ids[kind], kind) for kind in ('list', 'kanban', 'form', 'search')])['views']
        self.assertEqual(set(views), {'list', 'kanban', 'form', 'search'})
        fields_in_list = set(views['list']['arch'].split('"'))
        for name in ('woow_source_model_label', 'woow_source_record_label',
                     'action_woow_open_source'):
            self.assertIn(name, fields_in_list)
        self.assertIn('action_woow_download', views['form']['arch'])
        groupings = views['search']['arch']
        for name in ('create_date:month', 'res_model', 'create_uid'):
            self.assertIn(name, groupings)
        ids = self.att_partner.ids
        rows = attachments.web_search_read([('id', 'in', ids)], {
            'name': {}, 'woow_source_model_label': {}, 'woow_source_record_label': {}})['records']
        self.assertEqual(rows[0]['woow_source_record_label'], self.partner.display_name)
