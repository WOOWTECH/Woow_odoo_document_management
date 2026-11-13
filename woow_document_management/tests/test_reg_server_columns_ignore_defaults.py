# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Create takes the own default of every column that only the server sets.

Create checks only the keys it is given. Core then completes the others from the defaults of the
caller (``ir.default``: every internal user may store their own, an administrator may store them for
the whole site) or from a ``default_<field>`` key of the context, and a call made with ``sudo()``
keeps the uid of its caller and so his defaults. The audit rows are written like that, so a column
that the server sets must come out as if no default existed, whichever way the record is made.
"""
import base64

from odoo import Command, models
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

_USER = 'woow_document_management.group_woow_document_user'
_MAGIC = frozenset(models.MAGIC_COLUMNS) | {'display_name'}
# The three ways a default reaches a create call: stored by the caller, stored for the site,
# or a key of the context.
WAYS = ('personal', 'site', 'context')


def _store(env, model, defaults, *, user=None):
    """Store defaults, instead of the ones the model had: for ``user`` alone (any internal user
    may), or for the whole site."""
    _forget(env, model)
    for name, value in defaults.items():
        if user is None:
            env['ir.default'].with_user(env.ref('base.user_admin')).set(model, name, value)
        else:
            env['ir.default'].with_user(user).set(model, name, value, user_id=True)


def _forget(env, *model_names):
    env['ir.default'].sudo().search([('field_id.model', 'in', list(model_names))]).unlink()


def _with_defaults(env, way, model_name, user, defaults):
    """``model_name`` as ``user`` with defaults given the way ``way`` names; ``create`` is called
    on the result."""
    model = env[model_name].with_user(user)
    if way == 'context':
        _forget(env, model_name)
        return model.with_context(
            **{'default_%s' % name: value for name, value in defaults.items()})
    _store(env, model_name, defaults, user=user if way == 'personal' else None)
    return model


@tagged('woow_docmgmt_core')
class TestAuditRowsAreSetByTheServer(TransactionCase):
    """The audit row of an event: no column of it takes a default."""

    # What a caller or a site may store as the default of the columns that the server fills in.
    POISON = {'ip_hash': 'f' * 64, 'ip_display': 'poison-display', 'user_agent': 'poison-agent',
              'detail': {'files': 99}}

    def setUp(self):
        super().setUp()
        self.member = new_test_user(self.env, login='srvcol_log_member', groups=_USER)
        self.log = self.env['woow.document.log']

    def _newest(self, event):
        """(result, ip_hash, ip_display, user_agent, detail) as stored, of the member's newest
        row of ``event``."""
        self.env.flush_all()
        self.env.cr.execute(
            """SELECT result, ip_hash, ip_display, user_agent, detail FROM woow_document_log
               WHERE user_id = %s AND event = %s ORDER BY id DESC LIMIT 1""",
            (self.member.id, event))
        return self.env.cr.fetchone()

    def test_a_stored_default_does_not_reach_an_audit_row(self):
        row = {'event': 'zip', 'channel': 'backend', 'user_id': self.member.id}
        self.log.with_user(self.member)._log_event('view', channel='backend')
        self.log.sudo().create(row)
        written, minimal = self._newest('view'), self._newest('zip')
        self.assertEqual((written[0], minimal[0]), ('ok', 'ok'))
        for way in ('personal', 'site'):
            with self.subTest(way=way):
                _store(self.env, 'woow.document.log', self.POISON,
                       user=self.member if way == 'personal' else None)
                self.log.with_user(self.member)._log_event('view', channel='backend')
                self.assertEqual(self._newest('view'), written)
                # a call made with sudo() keeps the uid of the caller, and so his defaults
                self.log.with_user(self.member).sudo().create(row)
                self.assertEqual(self._newest('zip'), minimal)

    def test_a_context_default_does_not_reach_an_audit_row(self):
        context = {'default_%s' % name: value for name, value in self.POISON.items()}
        row = {'event': 'zip', 'channel': 'backend', 'user_id': self.member.id}
        self.log.with_user(self.member)._log_event('view', channel='backend')
        self.log.sudo().create(row)
        written, minimal = self._newest('view'), self._newest('zip')
        # a call that already is a superuser's keeps the keys of its context
        self.log.sudo().with_context(**context).create(row)
        self.assertEqual(self._newest('zip'), minimal)
        self.log.with_user(self.member).with_context(**context)._log_event('view', channel='backend')
        self.assertEqual(self._newest('view'), written)

    def test_values_that_are_passed_are_kept(self):
        """A row that carries its own columns (a test fixture, a migrated row) keeps them."""
        _store(self.env, 'woow.document.log', {'ip_display': 'stored'}, user=self.member)
        self.log.with_user(self.member).sudo().create({
            'event': 'view', 'channel': 'backend', 'user_id': self.member.id,
            'ip_hash': 'a' * 64, 'ip_display': '203.0.113.0/24', 'user_agent': 'passed agent',
            'detail': {'files': 3}})
        self.assertEqual(self._newest('view'), (
            'ok', 'a' * 64, '203.0.113.0/24', 'passed agent', {'files': 3}))

    def test_the_prefill_of_a_form_still_follows_the_stored_defaults(self):
        _store(self.env, 'woow.document.log', {'user_agent': 'stored agent'}, user=self.member)
        self.assertEqual(self.log.with_user(self.member).default_get(['user_agent']),
                         {'user_agent': 'stored agent'})
        self.assertEqual(self.log.default_get(['user_agent', 'result']), {'result': 'ok'})


@tagged('woow_docmgmt_core')
class TestServerColumnsAreDeclared(TransactionCase):
    """The models of the module that only the server writes list every one of their columns."""

    def test_server_only_models_declare_every_column(self):
        model = self.env['woow.document.log']
        self.assertEqual(model._woow_own_default_fields, frozenset(model._fields) - _MAGIC)

    def test_a_declared_name_is_a_field_of_its_model(self):
        checked = []
        for name, model in self.env.registry.items():
            declared = getattr(model, '_woow_own_default_fields', None)
            if model._original_module != 'woow_document_management' or declared is None:
                continue
            checked.append(name)
            with self.subTest(model=name):
                self.assertEqual(declared - set(model._fields), set())
        self.assertGreaterEqual(len(checked), 7)


@tagged('woow_docmgmt_core')
class TestEveryModelTakesItsOwnDefaults(TransactionCase):
    """One test for each model that has columns which only the server sets."""

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = env.ref('base.user_admin')
        self.company_a = env.company
        self.company_b = env['res.company'].sudo().create({'name': 'Server columns company B'})
        self.member = new_test_user(env, login='srvcol_member', groups=_USER)
        self.other = new_test_user(env, login='srvcol_other', groups=_USER)
        self.contact = env['res.partner'].sudo().create({'name': 'Server columns contact'})
        folders = env['woow.document.folder'].sudo()
        self.parent = folders.create({'name': 'Server columns parent', 'owner_id': self.admin.id})
        self.folder = folders.create({
            'name': 'Server columns folder', 'owner_id': self.admin.id,
            'parent_id': self.parent.id})
        access = env['woow.document.folder.access'].sudo()
        access.create({'folder_id': self.parent.id, 'subject_type': 'user',
                       'user_id': self.member.id, 'level': 'edit'})
        access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                       'user_id': self.member.id, 'level': 'manage'})
        env.flush_all()

    # -- folders ---------------------------------------------------------------------------------
    def test_a_new_folder_takes_the_effective_access_of_its_parent(self):
        """An editor of the parent could otherwise give any user, group or contact access to the
        new folder above the ceiling of the parent: the effective access of a folder is computed."""
        folders = self.env['woow.document.folder']
        control = folders.with_user(self.member).create({
            'name': 'Server columns control', 'parent_id': self.parent.id})
        eff = sorted(name for name in folders._fields if name.startswith('eff_'))
        self.assertEqual(len(eff), 10)
        group = self.env.ref('base.group_user')
        defaults = {name: [self.contact.id] if name.endswith('partner_ids') else
                    [group.id] if name.endswith('group_ids') else [self.other.id] for name in eff}
        defaults.update({'in_trash': True, 'has_restricted_descendant': True})
        for way in WAYS:
            with self.subTest(way=way):
                child = _with_defaults(self.env, way, 'woow.document.folder', self.member,
                                       defaults).create({
                    'name': 'Server columns child %s' % way, 'parent_id': self.parent.id})
                self.env.flush_all()
                for name in eff:
                    self.assertEqual(child.sudo()[name], control.sudo()[name], name)
                self.assertFalse(child.sudo().in_trash)
                self.assertEqual(folders.with_user(self.other).search_count([
                    ('id', '=', child.id)]), 0)

    def test_a_new_folder_has_no_children_documents_or_activities_from_a_default(self):
        todo = self.env.ref('mail.mail_activity_data_todo')
        defaults = {
            'child_ids': [{'name': 'Server columns default child'}],
            'document_ids': [{'name': 'default.txt', 'datas': base64.b64encode(b'x').decode()}],
            'activity_ids': [{'summary': 'default activity', 'user_id': self.other.id,
                              'activity_type_id': todo.id,
                              'res_model_id': self.env['ir.model']._get_id(
                                  'woow.document.folder')}]}
        for name, value in defaults.items():
            for way in WAYS:
                with self.subTest(field=name, way=way):
                    child = _with_defaults(
                        self.env, way, 'woow.document.folder', self.member, {name: value}).create({
                            'name': 'Server columns %s %s' % (name, way),
                            'parent_id': self.parent.id})
                    self.env.flush_all()
                    child = child.sudo().with_context(active_test=False)
                    self.assertEqual(
                        (child.child_ids, child.document_ids, child.activity_ids),
                        (self.env['woow.document.folder'], self.env['woow.document'],
                         self.env['mail.activity']))

    # -- documents -------------------------------------------------------------------------------
    def test_a_new_document_has_no_activity_from_a_default(self):
        todo = self.env.ref('mail.mail_activity_data_todo')
        defaults = {'activity_ids': [{
            'summary': 'default activity', 'user_id': self.other.id,
            'activity_type_id': todo.id,
            'res_model_id': self.env['ir.model']._get_id('woow.document')}]}
        for way in WAYS:
            with self.subTest(way=way):
                doc = _with_defaults(self.env, way, 'woow.document', self.member, defaults).create({
                    'name': 'server-columns-%s.txt' % way, 'folder_id': self.folder.id,
                    'datas': base64.b64encode(b'activity')})
                self.assertFalse(doc.sudo().activity_ids)

    def test_the_file_of_a_document_is_never_public_or_tokenised(self):
        """The file is made as superuser under the uid of the uploader; only the document
        decides who reads it, so another user cannot read it as a public attachment."""
        outsider = new_test_user(self.env, login='srvcol_outsider', groups='base.group_user')
        defaults = {'public': True, 'access_token': 'poison-token', 'url': 'https://example.test/x',
                    'description': 'poison'}
        for way in ('personal', 'site'):
            with self.subTest(way=way):
                _store(self.env, 'ir.attachment', defaults,
                       user=self.member if way == 'personal' else None)
                doc = self.env['woow.document'].with_user(self.member).create({
                    'name': 'server-columns-file-%s.txt' % way, 'folder_id': self.folder.id,
                    'datas': base64.b64encode(b'a file')})
                attachment = doc.sudo().attachment_id
                self.assertEqual(
                    (attachment.public, attachment.access_token, attachment.url,
                     attachment.description), (False, False, False, False))
                with self.assertRaises(AccessError):
                    attachment.with_user(outsider).read(['raw'])
        with self.subTest(way='context'):
            # a call that already is a superuser's keeps the keys of its context
            image = self.env['ir.attachment'].sudo().with_context(
                default_public=True, default_access_token='poison-token').create({
                    'name': 'image_128', 'res_model': 'woow.document.folder',
                    'res_field': 'image_128', 'res_id': self.folder.id, 'type': 'binary',
                    'raw': b'a picture'})
            self.assertEqual((image.public, image.access_token), (False, False))
        with self.subTest(way='another attachment'):
            _store(self.env, 'ir.attachment', {'public': True}, user=self.member)
            note = self.env['ir.attachment'].with_user(self.member).create({
                'name': 'note.txt', 'raw': b'an ordinary attachment'})
            self.assertTrue(note.public)       # only the files of the module's records are reset

    def test_a_portal_upload_takes_nothing_from_a_default_of_the_site(self):
        """The reset does not depend on who creates: a contact's upload has no owner, hold or
        direct grant from a default that the site stored."""
        partner = self.env['res.partner'].sudo().create({'name': 'Server columns uploader'})
        portal = self.env['res.users'].sudo().create({
            'name': 'Server columns portal', 'login': 'srvcol_portal', 'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner', 'partner_id': partner.id,
            'level': 'upload'})
        self.env.flush_all()
        defaults = {'owner_id': self.other.id, 'legal_hold': True, 'active': False,
                    'shared_partner_ids': [self.contact.id],
                    'uploaded_by_partner_id': self.contact.id}
        for way in ('site', 'context'):
            with self.subTest(way=way):
                doc = _with_defaults(self.env, way, 'woow.document', portal, defaults).create({
                    'name': 'server-columns-portal-%s.txt' % way, 'folder_id': self.folder.id,
                    'datas': base64.b64encode(b'portal')}).sudo()
                self.assertEqual(
                    (doc.owner_id, doc.legal_hold, doc.active, doc.shared_partner_ids,
                     doc.uploaded_by_partner_id),
                    (self.env['res.users'], False, True, self.env['res.partner'], partner))

    # -- member rows -----------------------------------------------------------------------------
    def test_a_member_row_takes_the_company_of_its_folder(self):
        for way in WAYS:
            with self.subTest(way=way):
                newcomer = new_test_user(self.env, login='srvcol_new_%s' % way, groups=_USER)
                row = _with_defaults(
                    self.env, way, 'woow.document.folder.access', self.member,
                    {'company_id': self.company_b.id}).create({
                        'folder_id': self.folder.id, 'subject_type': 'user',
                        'user_id': newcomer.id, 'level': 'read'})
                self.assertEqual(row.company_id, self.company_a)

    # -- tags ------------------------------------------------------------------------------------
    def test_every_field_of_a_tag_is_the_choice_of_its_user(self):
        self.assertEqual(self.env['woow.document.tag']._woow_own_default_fields, frozenset())
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.tag_create_policy', 'users')
        for way in ('personal', 'site'):
            with self.subTest(way=way):
                tag = _with_defaults(self.env, way, 'woow.document.tag', self.member,
                                     {'color': 5}).create({'name': 'server columns tag %s' % way})
                self.assertEqual(tag.color, 5)

    def test_a_row_made_with_sudo_does_not_take_the_defaults_of_the_caller(self):
        """The superuser flag does not matter: the uid of the caller is the one that is kept."""
        defaults = {'legal_hold': True, 'inherit_access': False, 'active': False}
        _store(self.env, 'woow.document.folder', defaults, user=self.member)
        folder = self.env['woow.document.folder'].with_user(self.member).sudo().create({
            'name': 'Server columns sudo folder', 'parent_id': self.parent.id})
        self.assertEqual((folder.legal_hold, folder.inherit_access, folder.active),
                         (False, True, True))
