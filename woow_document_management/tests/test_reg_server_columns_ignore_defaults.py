# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Create takes the own default of every column that only the server sets.

Create checks only the keys it is given. Core then completes the others from the defaults of the
caller (``ir.default``: every internal user may store their own, an administrator may store them for
the whole site) or from a ``default_<field>`` key of the context, and a call made with ``sudo()``
keeps the uid of its caller and so his defaults. The audit rows are written like that, and their
``count`` is what the limits of the share mails and of the share downloads add up, so a column that
the server sets must come out as if no default existed, whichever way the record is made.
"""
import base64
import re
import secrets
from datetime import timedelta

from psycopg2 import errors

from odoo import Command, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import TEST_CURSOR_COOKIE_NAME, Opener, new_test_user
from odoo.tools import mute_logger

from ..models.document_share import SECRET_CTX_KEY, _SECRET_SENTINEL
from ..services import tokens

_USER = 'woow_document_management.group_woow_document_user'
_MAGIC = frozenset(models.MAGIC_COLUMNS) | {'display_name'}
_CSRF = re.compile(rb'name="csrf_token"[^>]*value="([^"]+)"')
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


def _with_defaults(env, way, model_name, user, defaults, *, su=False):
    """``model_name`` as ``user`` (as a superuser of that uid when ``su``) with defaults given
    the way ``way`` names; ``create`` is called on the result."""
    model = env[model_name].with_user(user)
    if su:
        model = model.sudo()
    if way == 'context':
        _forget(env, model_name)
        return model.with_context(
            **{'default_%s' % name: value for name, value in defaults.items()})
    _store(env, model_name, defaults, user=user if way == 'personal' else None)
    return model


@tagged('woow_docmgmt_core')
class TestAuditRowsAreSetByTheServer(TransactionCase):
    """The audit row of an event: no column of it takes a default."""

    def setUp(self):
        super().setUp()
        self.member = new_test_user(self.env, login='srvcol_log_member', groups=_USER)
        self.log = self.env['woow.document.log']

    def _newest(self, event):
        """(count, link_ref, bucket_minute) as stored, of the member's newest row of ``event``."""
        self.env.flush_all()
        self.env.cr.execute(
            """SELECT count, link_ref, bucket_minute FROM woow_document_log
               WHERE user_id = %s AND event = %s ORDER BY id DESC LIMIT 1""",
            (self.member.id, event))
        return self.env.cr.fetchone()

    def test_a_stored_default_does_not_reach_an_audit_row(self):
        for way in ('personal', 'site'):
            for count in (0, -5, 7):
                with self.subTest(way=way, count=count):
                    _store(self.env, 'woow.document.log', {
                        'count': count, 'link_ref': 4242, 'bucket_minute': '2001-01-01 00:00:00'},
                        user=self.member if way == 'personal' else None)
                    self.log.with_user(self.member)._log_event('view', channel='backend')
                    self.assertEqual(self._newest('view'), (1, None, None))

    def test_a_context_default_does_not_reach_an_audit_row(self):
        context = {'default_count': 0, 'default_link_ref': 4242,
                   'default_bucket_minute': '2001-01-01 00:00:00'}
        # a call that already is a superuser's keeps the keys of its context
        row = self.log.sudo().with_context(**context).create({
            'event': 'view', 'channel': 'backend', 'user_id': self.member.id})
        self.env.flush_all()
        self.assertEqual((row.count, row.link_ref, row.bucket_minute), (1, 0, False))
        self.log.with_user(self.member).with_context(**context)._log_event('zip', channel='backend')
        self.assertEqual(self._newest('zip'), (1, None, None))

    def test_values_that_are_passed_are_kept(self):
        """A row that carries its own count and bucket (a test fixture, a migrated row) keeps them."""
        _store(self.env, 'woow.document.log', {'count': 0}, user=self.member)
        row = self.log.with_user(self.member).sudo().create({
            'event': 'share_send', 'channel': 'backend', 'user_id': self.member.id, 'count': 99,
            'bucket_minute': '2001-01-01 00:00:00'})
        self.assertEqual((row.count, str(row.bucket_minute)), (99, '2001-01-01 00:00:00'))

    def test_the_prefill_of_a_form_still_follows_the_stored_defaults(self):
        _store(self.env, 'woow.document.log', {'count': 0}, user=self.member)
        self.assertEqual(self.log.with_user(self.member).default_get(['count']), {'count': 0})
        self.assertEqual(self.log.default_get(['count']), {'count': 1})


@tagged('woow_docmgmt_core')
class TestServerColumnsAreDeclared(TransactionCase):
    """The models of the module that only the server writes list every one of their columns."""

    def test_server_only_models_declare_every_column(self):
        for name in ('woow.document.log', 'woow.document.denied.budget',
                     'woow.document.denied.counter'):
            with self.subTest(model=name):
                model = self.env[name]
                self.assertEqual(model._woow_own_default_fields,
                                 frozenset(model._fields) - _MAGIC)

    def test_a_declared_name_is_a_field_of_its_model(self):
        checked = []
        for name, model in self.env.registry.items():
            declared = getattr(model, '_woow_own_default_fields', None)
            if model._original_module != 'woow_document_management' or declared is None:
                continue
            checked.append(name)
            with self.subTest(model=name):
                self.assertEqual(declared - set(model._fields), set())
        self.assertGreaterEqual(len(checked), 8)


@tagged('woow_docmgmt_share_mail')
class TestShareMailLimitHoldsWhateverTheSenderStored(TransactionCase):

    def _send_one(self, member, folder, recipient):
        wizard = self.env['woow.document.share.wizard'].with_user(member).create({
            'share_type': 'folder', 'folder_id': folder.id, 'recipient_emails': recipient,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        return wizard.action_create_links()

    def test_the_hourly_limit_counts_every_send(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.share_link_base_url', 'https://docs.example.test')
        for default in (0, -5):
            with self.subTest(default=default):
                member = new_test_user(self.env, login='srvcol_mail_%d' % abs(default),
                                       groups=_USER)
                folder = self.env['woow.document.folder'].sudo().create({
                    'name': 'Mail limit folder %d' % abs(default), 'owner_id': member.id})
                _store(self.env, 'woow.document.log', {'count': default}, user=member)
                # 99 sends are on record; the hundredth is allowed, the next one is not.
                self.env['woow.document.log'].create({
                    'event': 'share_send', 'channel': 'backend', 'user_id': member.id,
                    'count': 99})
                self.assertEqual(self._send_one(member, folder, 'first@example.test')['tag'],
                                 'woow_document_share_result')
                log = self.env['woow.document.log'].sudo()
                self.assertEqual(log._sum_count([
                    ('event', '=', 'share_send'), ('user_id', '=', member.id)]), 100)
                mails = self.env['mail.mail'].sudo().search_count([
                    ('model', '=', 'woow.document.share')])
                with self.assertRaises(UserError):
                    self._send_one(member, folder, 'second@example.test')
                self.assertEqual(self.env['mail.mail'].sudo().search_count([
                    ('model', '=', 'woow.document.share')]), mails)


@tagged('woow_docmgmt_share_scope')
class TestDownloadLimitHoldsWhateverTheVisitorStored(TransactionCase):
    """A visitor who is signed in is recorded as himself, with his own defaults at hand."""

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = env.ref('base.user_admin')
        self.visitor = new_test_user(env, login='srvcol_dl_visitor', groups='base.group_user')
        folder = env['woow.document.folder'].sudo().create({
            'name': 'Download limit folder', 'owner_id': self.admin.id})
        env['woow.document'].sudo().create({
            'name': 'limit.txt', 'folder_id': folder.id, 'datas': base64.b64encode(b'limit')})
        self.share, _code = env['woow.document.share'].with_user(self.admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id, 'max_downloads': 2,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        env.flush_all()

    def test_a_limited_link_counts_the_downloads_of_such_a_visitor(self):
        for default in (0, -5):
            with self.subTest(default=default):
                _store(self.env, 'woow.document.log', {'count': default}, user=self.visitor)
                share, scope = self.env['woow.document.share']._resolve_request_access(
                    share_id=self.share.id)
                before = share._woow_download_count(scope)
                self.assertTrue(share._download_slot_available(scope))
                share._record_download(scope, self.env(user=self.visitor.id), '192.0.2.10',
                                       {'files': 1})
                self.assertEqual(share._woow_download_count(scope), before + 1)
        # two counted downloads used the limit of two up
        self.assertFalse(share._download_slot_available(scope))


@tagged('woow_docmgmt_share_scope')
class TestOneUseLinkIsUsedUpByAnyVisitor(HttpCase):

    def _sign_in(self, user, password):
        """Sign in with one cookie per name (opening a link rotates the session)."""
        self.opener = Opener(self.cr)
        self.opener.cookies[TEST_CURSOR_COOKIE_NAME] = self.http_request_key
        self.authenticate(user.login, password)
        values = {cookie.name: cookie.value for cookie in self.opener.cookies}
        self.opener.cookies.clear()
        for name, value in values.items():
            self.opener.cookies.set(name, value, domain='127.0.0.1', path='/')

    def test_a_signed_in_visitor_with_a_stored_default_gets_one_download(self):
        env = self.env
        admin = env.ref('base.user_admin')
        password = secrets.token_urlsafe(16)
        visitor = new_test_user(env, login='srvcol_http_visitor', groups='base.group_user',
                                password=password)
        folder = env['woow.document.folder'].sudo().create({
            'name': 'One use folder', 'owner_id': admin.id})
        doc = env['woow.document'].sudo().create({
            'name': 'one-use.txt', 'folder_id': folder.id, 'datas': base64.b64encode(b'one use')})
        share, code = env['woow.document.share'].with_user(admin)._issue({
            'share_type': 'folder', 'folder_id': folder.id, 'max_downloads': 1,
            'expire_at': fields.Datetime.now() + timedelta(days=1)})
        _store(env, 'woow.document.log', {'count': 0}, user=visitor)
        env.flush_all()
        self._sign_in(visitor, password)
        landing = self.url_open('/woow-docs/s')
        opened = self.url_open('/woow-docs/s/open', data={
            'token': code, 'csrf_token': _CSRF.search(landing.content).group(1).decode()},
            allow_redirects=False)
        self.assertEqual(opened.status_code, 303)
        path = '/woow-docs/s/%d/file/%d' % (share.id, doc.id)
        self.assertEqual(self.url_open(path).status_code, 200)
        self.assertEqual(self.url_open(path).status_code, 404)
        self.assertEqual(share.with_user(admin).download_count, 1)


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

    def _expiry(self):
        return fields.Datetime.now() + timedelta(days=1)

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
        defaults.update({'in_trash': True, 'has_restricted_descendant': True,
                         'subtree_restored_at': '2001-01-01 00:00:00'})
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
        share from a default that the site stored."""
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

    # -- links ----------------------------------------------------------------------------------
    def test_a_new_link_is_active_unlimited_and_open_unless_it_is_passed(self):
        defaults = {
            'password_hash': tokens.hash_password(secrets.token_urlsafe(16)), 'max_downloads': 7,
            'state': 'revoked', 'revoked_at': '2001-01-01 00:00:00',
            'revoked_by_id': self.other.id, 'sent_at': '2001-01-02 00:00:00'}
        for way in WAYS:
            with self.subTest(way=way):
                shares = _with_defaults(self.env, way, 'woow.document.share', self.member,
                                        defaults)
                share, _token = shares._issue({
                    'share_type': 'folder', 'folder_id': self.folder.id,
                    'expire_at': self._expiry(), 'name': 'server columns %s' % way})
                share = share.sudo()
                self.assertEqual(
                    (share.state, share.max_downloads, share.has_password,
                     bool(share.password_hash), share.revoked_at, share.revoked_by_id,
                     share.sent_at),
                    ('active', 0, False, False, False, self.env['res.users'], False))
                # what is passed is kept
                chosen, _token = shares._issue({
                    'share_type': 'folder', 'folder_id': self.folder.id,
                    'expire_at': self._expiry(), 'max_downloads': 3,
                    'password': secrets.token_urlsafe(16)})
                self.assertEqual((chosen.sudo().max_downloads, chosen.sudo().has_password),
                                 (3, True))

    def test_a_link_is_not_made_without_the_issue_step(self):
        """The hash of the token of a link is not supplied by a stored default: a row that does
        not carry it is refused."""
        for way in WAYS:
            defaults = {'token_hash': way[0] * 64, 'token_hint': 'abcdef'}
            with self.subTest(way=way), mute_logger('odoo.sql_db'), \
                    self.assertRaises(errors.NotNullViolation), self.cr.savepoint():
                _with_defaults(self.env, way, 'woow.document.share', self.member, defaults).create({
                    'share_type': 'folder', 'folder_id': self.folder.id,
                    'company_id': self.company_a.id, 'expire_at': self._expiry(),
                    'name': 'server columns direct'})

    def test_the_company_of_a_link_is_not_supplied_by_a_default(self):
        """The company of a link is the one that ``_issue`` names, and is checked when it is
        passed: a stored default cannot move a link to another company of its creator."""
        both = [self.company_a.id, self.company_b.id]
        creator = new_test_user(self.env, login='srvcol_two_companies', groups=_USER,
                                company_id=self.company_a.id, company_ids=[Command.set(both)])
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': creator.id,
            'level': 'manage'})
        self.env.flush_all()
        for way in WAYS:
            with self.subTest(way=way), mute_logger('odoo.sql_db'), \
                    self.assertRaises(errors.NotNullViolation), self.cr.savepoint():
                shares = _with_defaults(self.env, way, 'woow.document.share', creator,
                                        {'company_id': self.company_b.id})
                shares.with_context(allowed_company_ids=both, **{
                    SECRET_CTX_KEY: _SECRET_SENTINEL}).create({
                        'share_type': 'folder', 'folder_id': self.folder.id,
                        'expire_at': self._expiry(), 'token_hash': way[0] * 64,
                        'token_hint': 'abcdef'})

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

    # -- wizards ---------------------------------------------------------------------------------
    def test_a_share_wizard_takes_no_password_hash_from_a_default(self):
        """The hash of a link password is written only through ``password``, which checks its
        length: a ready-made hash is refused when it is passed, and is not taken from a default."""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.share_link_base_url', 'https://docs.example.test')
        forged = tokens.hash_password(secrets.token_urlsafe(16))
        defaults = {'password_hash': forged, 'note': 'from a default'}
        for way in WAYS:
            with self.subTest(way=way):
                wizard = _with_defaults(self.env, way, 'woow.document.share.wizard', self.member,
                                        defaults).create({
                    'share_type': 'folder', 'folder_id': self.folder.id,
                    'expire_at': self._expiry()})
                self.assertFalse(wizard.password_hash)
                self.assertEqual(wizard.note, 'from a default')      # a form field keeps its default
                wizard.action_create_links()
                share = self.env['woow.document.share'].sudo().search([
                    ('folder_id', '=', self.folder.id)], order='id desc', limit=1)
                self.assertFalse(share.has_password)
        prefill = self.env['woow.document.share.wizard'].with_user(self.member)
        _store(self.env, 'woow.document.share.wizard', {'note': 'prefilled'}, user=self.member)
        self.assertEqual(prefill.default_get(['note']), {'note': 'prefilled'})

    # -- the counters of refused requests --------------------------------------------------------
    def test_a_counter_row_is_not_completed_from_a_default(self):
        """Their rows are written by statements; a call that creates one passes every column."""
        minute = fields.Datetime.to_datetime('2001-01-01 00:00:00')
        cases = (
            ('woow.document.denied.budget', {}, {'count': 5}),
            ('woow.document.denied.budget', {'count': 1}, {'bucket_minute': True}),
            ('woow.document.denied.counter', {'event': 'share_access'}, {'count': 5}),
            ('woow.document.denied.counter', {'count': 1}, {'event': 'share_access'}),
            ('woow.document.denied.counter', {'event': 'share_access', 'count': 1},
             {'bucket_minute': True}),
        )
        step = 0
        for way in WAYS:
            for name, passed, defaults in cases:
                step += 1
                row = dict(passed)
                defaults = dict(defaults)
                if 'bucket_minute' in defaults:
                    defaults['bucket_minute'] = minute + timedelta(minutes=step)
                else:
                    row['bucket_minute'] = minute + timedelta(minutes=step)
                if name.endswith('counter') and 'ip_hash' not in row:
                    row['ip_hash'] = '%064x' % step
                with self.subTest(way=way, model=name, missing=sorted(defaults)), \
                        mute_logger('odoo.sql_db'), self.assertRaises(errors.NotNullViolation), \
                        self.cr.savepoint():
                    _with_defaults(self.env, way, name, self.member, defaults, su=True).create(row)
        # a row that passes every column is written as it is
        row = self.env['woow.document.denied.budget'].sudo().create({
            'bucket_minute': minute + timedelta(minutes=step + 1), 'count': 3})
        self.assertEqual(row.count, 3)

    def test_a_row_made_with_sudo_does_not_take_the_defaults_of_the_caller(self):
        """The superuser flag does not matter: the uid of the caller is the one that is kept."""
        defaults = {'legal_hold': True, 'inherit_access': False, 'active': False}
        _store(self.env, 'woow.document.folder', defaults, user=self.member)
        folder = self.env['woow.document.folder'].with_user(self.member).sudo().create({
            'name': 'Server columns sudo folder', 'parent_id': self.parent.id})
        self.assertEqual((folder.legal_hold, folder.inherit_access, folder.active),
                         (False, True, True))
        manager_only = {'state': 'revoked', 'max_downloads': 9}
        _store(self.env, 'woow.document.share', manager_only, user=self.member)
        share, _token = self.env['woow.document.share'].with_user(self.member).sudo()._issue({
            'share_type': 'folder', 'folder_id': self.folder.id, 'expire_at': self._expiry()})
        self.assertEqual((share.state, share.max_downloads), ('active', 0))
