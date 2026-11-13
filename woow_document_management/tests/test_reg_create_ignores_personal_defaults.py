# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""A stored default never fills a field that a create call is refused for when it names it.

Create checks the keys it is given. Odoo then completes the others from the stored defaults of the
caller (``ir.default``): every internal user may store their own, an administrator may store them
for the whole site, and no field group applies to them. A field that only the server, or a checked
value, fills must come out as if no default existed, whichever way the record is made.
"""
import base64
import json
import secrets

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, tagged
from odoo.tests.common import new_test_user

from .common import csrf_token

_USER = 'woow_document_management.group_woow_document_user'
_OVERVIEW = 'woow_document_management.group_woow_document_attachment_overview'
_LINK = 'https://example.test/page'


@tagged('woow_docmgmt_http')
class TestStoredDefaultsDoNotFillServerFields(HttpCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        env = self.env
        self.password = secrets.token_urlsafe(16)
        self.admin = env.ref('base.user_admin')
        self.uploader = new_test_user(env, login='wdoc-dflt-uploader', password=self.password,
                                      groups=','.join((_USER, _OVERVIEW)))
        self.other = new_test_user(env, login='wdoc-dflt-other', groups=_USER,
                                   password=self.password)
        self.contact = env['res.partner'].sudo().create({'name': 'Default customer'})
        self.partner = env['res.partner'].sudo().create({'name': 'Default portal contact'})
        self.portal = env['res.users'].sudo().create({
            'name': 'Default portal', 'login': 'wdoc-dflt-portal', 'password': self.password,
            'partner_id': self.partner.id,
            'groups_id': [Command.set([env.ref('base.group_portal').id])]})
        self.tag = env['woow.document.tag'].sudo().create({'name': 'Default tag'})
        folders = env['woow.document.folder'].sudo()
        self.folder = folders.create({'name': 'Default folder', 'owner_id': self.admin.id})
        self.parent = folders.create({'name': 'Default parent', 'owner_id': self.admin.id})
        access = env['woow.document.folder.access'].sudo()
        access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                       'user_id': self.uploader.id, 'level': 'upload'})
        access.create({'folder_id': self.folder.id, 'subject_type': 'partner',
                       'partner_id': self.partner.id, 'level': 'read'})
        access.create({'folder_id': self.parent.id, 'subject_type': 'user',
                       'user_id': self.uploader.id, 'level': 'edit'})
        env.flush_all()

    # -- helpers ---------------------------------------------------------------------------------
    def _rpc(self, model, method, args, kwargs=None):
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 'dflt', 'params': {
            'model': model, 'method': method, 'args': args, 'kwargs': kwargs or {}}})
        response = self.url_open('/web/dataset/call_kw/%s/%s' % (model, method), data=payload,
                                 headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn('error', body, body)
        return body['result']

    def _store(self, model, defaults, scope):
        """Store defaults as the signed-in uploader (personal) or as the administrator (site)."""
        for name, value in defaults.items():
            if scope == 'personal':
                self._rpc('ir.default', 'set', [model, name, value], {'user_id': True})
            else:
                self.env['ir.default'].with_user(self.admin).set(model, name, value)

    def _forget_defaults(self):
        self.env['ir.default'].sudo().search([
            ('field_id.model', 'in', ['woow.document', 'woow.document.folder'])]).unlink()

    def _upload_form(self):
        """The form fields of an upload into the fixture folder, with this session's CSRF token."""
        return {'folder_id': str(self.folder.id), 'csrf_token': csrf_token(self)}

    def _document_defaults(self):
        """A default for every field that create refuses, or checks, when it is passed."""
        return {
            'shared_user_ids': [self.other.id], 'shared_partner_ids': [self.partner.id],
            'owner_id': self.other.id, 'legal_hold': True,
            'uploaded_by_partner_id': self.partner.id,
            'source_res_model': 'res.partner', 'source_res_id': self.partner.id,
            'document_type': 'url', 'url': 'ftp://files.example.test/unchecked',
            'datas': base64.b64encode(b'unchecked').decode(), 'mimetype': 'text/html',
            'active': False, 'trashed_date': '2026-01-01 00:00:00',
            'trashed_by_id': self.other.id, 'restored_date': '2026-01-02 00:00:00',
        }

    def _ordinary_defaults(self):
        return {'description': '<p>from a default</p>', 'partner_id': self.contact.id,
                'tag_ids': [self.tag.id]}

    def _state(self, doc):
        doc = doc.sudo().with_context(active_test=False)
        return {
            'type': doc.document_type, 'url': doc.url or False, 'file': bool(doc.datas),
            'mimetype': doc.mimetype or False, 'owner': doc.owner_id.id,
            'shared_users': doc.shared_user_ids.ids, 'shared_partners': doc.shared_partner_ids.ids,
            'hold': doc.legal_hold, 'uploaded_by': doc.uploaded_by_partner_id.id,
            'source': (doc.source_res_model or False, doc.source_res_id or False),
            'active': doc.active,
            'trashed': (doc.trashed_date, doc.trashed_by_id.id, doc.restored_date),
            # ordinary fields keep taking their defaults
            'description': str(doc.description), 'partner': doc.partner_id.id,
            'tags': doc.tag_ids.ids,
        }

    def _expected(self, *, link=False):
        return {
            'type': 'url' if link else 'binary', 'url': _LINK if link else False,
            'file': not link, 'mimetype': False if link else 'text/plain',
            'owner': self.uploader.id, 'shared_users': [], 'shared_partners': [], 'hold': False,
            'uploaded_by': False, 'source': (False, False), 'active': True,
            'trashed': (False, False, False),
            'description': '<p>from a default</p>', 'partner': self.contact.id,
            'tags': [self.tag.id],
        }

    def _documents(self, ids):
        return self.env['woow.document'].sudo().with_context(active_test=False).browse(ids)

    def _check_documents(self, label, ids, *, link=False):
        docs = self._documents(ids)
        self.assertEqual(len(docs), 1, label)
        self.assertEqual(self._state(docs), self._expected(link=link), label)

    def _file_wizard(self, name):
        attachment = self.env['ir.attachment'].with_user(self.uploader).create({
            'name': name, 'datas': base64.b64encode(b'wizard file')})
        wizard = self.env['woow.document.add.attachment.wizard'].with_user(self.uploader) \
            .with_context(active_model='ir.attachment', active_ids=[attachment.id]) \
            .create({'folder_id': self.folder.id})
        wizard.action_add()
        return self.env['woow.document'].sudo().with_context(active_test=False).search([
            ('name', '=', name)]).ids

    def _make_documents(self, label, *, every_way):
        """The same file, and a link, made through the ways in; checks every record made."""
        self.authenticate(self.uploader.login, self.password)
        response = self.url_open('/woow_document/upload', data=self._upload_form(), files=[
            ('files', ('%s-route.txt' % label, b'route file', 'text/plain'))])
        self.assertEqual(response.status_code, 200, response.text)
        self._check_documents('%s: upload route' % label, response.json()['document_ids'])
        rows = [{'name': '%s-rpc.txt' % label, 'folder_id': self.folder.id,
                 'datas': base64.b64encode(b'rpc file').decode()}]
        self._check_documents('%s: create call' % label,
                              self._rpc('woow.document', 'create', [rows]))
        link = [{'name': '%s-link' % label, 'folder_id': self.folder.id,
                 'document_type': 'url', 'url': _LINK}]
        self._check_documents('%s: link' % label, self._rpc('woow.document', 'create', [link]),
                              link=True)
        if not every_way:
            return
        loaded = self._rpc('woow.document', 'load', [
            ['name', 'folder_id/.id', 'datas'],
            [['%s-import.txt' % label, str(self.folder.id),
              base64.b64encode(b'import file').decode()]]])
        self.assertFalse(loaded['messages'], loaded)
        self._check_documents('%s: import' % label, loaded['ids'])
        self._check_documents('%s: attachment wizard' % label,
                              self._file_wizard('%s-wizard.txt' % label))

    def _explicit_values_keep_their_checks(self):
        """Passing a field is still refused, or checked, as without a default for it."""
        documents = self.env['woow.document'].with_user(self.uploader)
        base = {'name': 'explicit.txt', 'folder_id': self.folder.id,
                'datas': base64.b64encode(b'explicit file').decode()}
        for label, extra, error in (
                ('shared partner', {'shared_partner_ids': [Command.set([self.partner.id])]},
                 AccessError),
                ('shared user', {'shared_user_ids': [Command.set([self.other.id])]}, AccessError),
                ('legal hold', {'legal_hold': True}, AccessError),
                ('other owner', {'owner_id': self.other.id}, AccessError),
                ('source', {'source_res_model': 'res.partner', 'source_res_id': 1}, AccessError),
                ('uploader', {'uploaded_by_partner_id': self.partner.id}, AccessError),
                ('link with a file', {'document_type': 'url', 'url': _LINK}, UserError),
                ('link without an address', {'document_type': 'url', 'datas': False}, UserError),
                ('file with a link', {'url': _LINK}, UserError)):
            with self.subTest(label), self.assertRaises(error), self.env.cr.savepoint():
                documents.create({**base, **extra})
        # an explicit value of an allowed field wins over a default, as it always did
        mine = documents.create({
            **base, 'owner_id': self.uploader.id, 'description': '<p>mine</p>'})
        self.assertEqual(self._state(mine), {
            **self._expected(), 'description': '<p>mine</p>'})

    # -- documents -------------------------------------------------------------------------------
    def test_personal_defaults_do_not_fill_server_fields_of_a_document(self):
        self.authenticate(self.uploader.login, self.password)
        self._store('woow.document', {**self._document_defaults(), **self._ordinary_defaults()},
                    'personal')
        # what a form shows when it starts a record is left as it was
        shown = self.env['woow.document'].with_user(self.uploader).default_get(
            ['legal_hold', 'document_type', 'owner_id'])
        self.assertEqual(shown, {'legal_hold': True, 'document_type': 'url',
                                 'owner_id': self.other.id})
        self._make_documents('personal', every_way=True)
        self._explicit_values_keep_their_checks()

    def test_site_wide_defaults_do_not_fill_them_either(self):
        self._store('woow.document', {**self._document_defaults(), **self._ordinary_defaults()},
                    'site')
        self._make_documents('site', every_way=False)

    def test_a_default_document_type_cannot_leave_a_link_without_an_address(self):
        """A file stored as a link without an address made every ZIP of its folder fail."""
        self.authenticate(self.uploader.login, self.password)
        self._store('woow.document', {'document_type': 'url'}, 'personal')
        response = self.url_open('/woow_document/upload', data=self._upload_form(), files=[
            ('files', ('zip.txt', b'zip file', 'text/plain'))])
        self.assertEqual(response.status_code, 200, response.text)
        with self.subTest('type'):
            self.assertEqual(self._documents(response.json()['document_ids']).document_type,
                             'binary')
        with self.subTest('back-office ZIP'):
            archive = self.url_open('/woow_document/zip?folder_ids=%d' % self.folder.id)
            self.assertEqual(archive.status_code, 200)
        with self.subTest('portal ZIP'):
            self.authenticate(self.portal.login, self.password)
            archive = self.url_open('/my/docs/folder/%d/zip' % self.folder.id)
            self.assertEqual(archive.status_code, 200)

    # -- folders ---------------------------------------------------------------------------------
    def _check_folder(self, label, folder_id, *, child):
        folder = self.env['woow.document.folder'].sudo().with_context(active_test=False).browse(
            folder_id)
        self.assertEqual({
            'hold': folder.legal_hold, 'inherit': folder.inherit_access, 'active': folder.active,
            'trashed': (folder.trashed_date, folder.trashed_by_id.id, folder.restored_date),
            'company': folder.company_id.id, 'owner': folder.owner_id.id,
            'parent_hold': folder.parent_id.subtree_legal_hold if child else None,
            'color': folder.color,
        }, {
            'hold': False, 'inherit': True, 'active': True, 'trashed': (False, False, False),
            'company': self.env.company.id, 'owner': self.admin.id if child else self.uploader.id,
            'parent_hold': False if child else None, 'color': 3,
        }, label)

    def _each_folder_default(self, scope):
        self.authenticate(self.uploader.login, self.password)
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.allow_root_create', 'True')
        self._store('woow.document.folder', {
            'legal_hold': True, 'inherit_access': False, 'active': False,
            'trashed_date': '2026-01-01 00:00:00', 'trashed_by_id': self.other.id,
            'restored_date': '2026-01-02 00:00:00', 'color': 3}, scope)
        folders = self.env['woow.document.folder'].with_user(self.uploader)
        self.assertEqual(
            folders.default_get(['legal_hold', 'inherit_access']),
            {'legal_hold': True, 'inherit_access': False})
        child = folders.create({'name': '%s orm child' % scope, 'parent_id': self.parent.id})
        self._check_folder('%s: create' % scope, child.id, child=True)
        rows = [{'name': '%s rpc child' % scope, 'parent_id': self.parent.id}]
        self._check_folder('%s: create call' % scope,
                           self._rpc('woow.document.folder', 'create', [rows])[0], child=True)
        # A site that lets members make folders at the top level still leaves them a company:
        # only a document administrator makes one that every company can see.
        self._forget_defaults()
        self._store('woow.document.folder', {'company_id': False, 'color': 3}, scope)
        root = folders.create({'name': '%s orm root' % scope})
        self._check_folder('%s: root' % scope, root.id, child=False)

    def test_personal_defaults_do_not_fill_server_fields_of_a_folder(self):
        self._each_folder_default('personal')

    def test_site_wide_defaults_do_not_fill_server_fields_of_a_folder(self):
        self._each_folder_default('site')
