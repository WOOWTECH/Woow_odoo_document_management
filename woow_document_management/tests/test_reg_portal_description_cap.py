# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""A portal contact's description of a file is short and carries no styling.

The description is an HTML field that core sanitises twice on every create, and for some markup the
cost grows with the square of its length. A portal contact may send a description with a file, so
the module refuses a long one before anything is stored or sanitised.

Core's sanitiser keeps the ``style`` and ``class`` attributes. Staff read the description in their own
form, in the page of the backend, so a block with ``position: fixed`` that a portal contact wrote
covers the whole page, a fake notice on top of the real one. The description of a portal contact is
sanitised again without styles and classes when the file is created (portal contacts cannot write a
document, so that is the only place); links and images stay. The description of a document user is
not touched.
"""
import base64
import json
import re
import secrets
from unittest.mock import patch

from odoo import Command
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import html_sanitize

from ..models.upload_policy import WoowUploadRejected
from ..services.constants import PORTAL_DESCRIPTION_MAX_CHARS

# markup with nothing to close: the cheapest text that makes the sanitiser work hard
_PATHOLOGICAL = '<' * (10 * PORTAL_DESCRIPTION_MAX_CHARS)

# a notice that covers the page of whoever reads it, with a link to sign in again
_OVERLAY = (
    '<div style="position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:2000;'
    'background:#fff url(https://example.test/p.png)" class="o_view_nocontent text-bg-danger">'
    '<p class="lead" STYLE="font-size:40px">Your session has expired</p>'
    "<a href='https://example.test/login' style='color:red' CLASS='btn btn-primary'>Sign in again</a>"
    '<img src="https://example.test/a.png" style="position:fixed" class="img-fluid"/></div>')
_STYLING = re.compile(r'(?i)(?<![\w-])(style|class)\s*=')
_STRICT = dict(sanitize_attributes=True, sanitize_style=True, strip_style=True, strip_classes=True)


def _fixture(env, suffix, password=None):
    """A portal contact and a document user, each allowed to upload to one folder."""
    partner = env['res.partner'].sudo().create({'name': 'Description portal contact %s' % suffix})
    portal_vals = {
        'name': 'Description portal %s' % suffix, 'login': 'wdoc-desc-portal-%s' % suffix,
        'partner_id': partner.id,
        'groups_id': [Command.set([env.ref('base.group_portal').id])]}
    if password:
        portal_vals['password'] = password
    portal = env['res.users'].sudo().create(portal_vals)
    member_vals = {
        'name': 'Description member %s' % suffix, 'login': 'wdoc-desc-member-%s' % suffix,
        'groups_id': [Command.set([
            env.ref('woow_document_management.group_woow_document_user').id])]}
    if password:
        member_vals['password'] = password
    member = env['res.users'].sudo().create(member_vals)
    folder = env['woow.document.folder'].sudo().create({
        'name': 'Description folder %s' % suffix, 'owner_id': env.ref('base.user_admin').id})
    access = env['woow.document.folder.access'].sudo()
    access.create({'folder_id': folder.id, 'subject_type': 'partner',
                   'partner_id': partner.id, 'level': 'upload'})
    access.create({'folder_id': folder.id, 'subject_type': 'user', 'user_id': member.id,
                   'level': 'upload'})
    env.flush_all()
    return portal, member, folder


def _values(folder, name, description):
    return {'name': name, 'folder_id': folder.id, 'description': description,
            'datas': base64.b64encode(b'description fixture').decode()}


@tagged('woow_docmgmt_files')
class TestPortalDescriptionCapOrm(TransactionCase):

    def setUp(self):
        super().setUp()
        self.portal, self.member, self.folder = _fixture(self.env, 'orm')
        self.documents = self.env['woow.document']

    def _count(self):
        return self.documents.sudo().search_count([('folder_id', '=', self.folder.id)])

    def test_a_long_description_is_refused_before_it_is_sanitised(self):
        for label, description in (('pathological', _PATHOLOGICAL),
                                   ('one character over', 'x' * (PORTAL_DESCRIPTION_MAX_CHARS + 1))):
            with self.subTest(description=label), \
                    patch('odoo.fields.html_sanitize', wraps=html_sanitize) as sanitize:
                with self.assertRaises(WoowUploadRejected) as caught:
                    self.documents.with_user(self.portal).create(
                        _values(self.folder, 'long.txt', description))
                self.assertEqual(caught.exception.code, 'content')
                sanitize.assert_not_called()
                self.assertEqual(self._count(), 0)

    def test_a_short_description_is_created_and_sanitised(self):
        within = '<p>' + 'x' * (PORTAL_DESCRIPTION_MAX_CHARS - len('<p></p>')) + '</p>'
        self.assertEqual(len(within), PORTAL_DESCRIPTION_MAX_CHARS)
        for name, description in (('short.txt', '<p>ok</p>'), ('limit.txt', within),
                                  ('none.txt', False)):
            with self.subTest(file=name):
                document = self.documents.with_user(self.portal).create(
                    _values(self.folder, name, description))
                self.assertEqual(document.sudo().description, description)
        # what is short enough is sanitised as before
        scripted = self.documents.with_user(self.portal).create(
            _values(self.folder, 'script.txt', '<p>ok</p><script>alert(1)</script>'))
        self.assertIn('ok', scripted.sudo().description)
        self.assertNotIn('script', scripted.sudo().description)
        self.assertEqual(self._count(), 4)

    def test_a_portal_description_keeps_its_text_and_loses_its_styles_and_classes(self):
        """The overlay becomes plain markup: the words, the link and the image stay."""
        description = self.documents.with_user(self.portal).create(
            _values(self.folder, 'overlay.txt', _OVERLAY)).sudo().description
        self.assertEqual(str(description), str(html_sanitize(_OVERLAY, **_STRICT)))
        self.assertNotRegex(description, _STYLING)
        self.assertNotIn('position', description)
        self.assertNotIn('z-index', description)
        self.assertIn('Your session has expired', description)
        self.assertIn('Sign in again', description)
        self.assertIn('href="https://example.test/login"', description)
        self.assertIn('src="https://example.test/a.png"', description)
        # what has no styling is stored as it was, and the other shapes of the value are as before
        for label, plain in (('plain', '<p>Plain <b>words</b> and <a href="https://example.test/">a link</a></p>'),
                             ('empty', ''), ('none', False)):
            with self.subTest(description=label):
                document = self.documents.with_user(self.portal).create(
                    _values(self.folder, 'plain-%s.txt' % label, plain))
                self.assertEqual(document.sudo().description, plain)

    def test_every_description_of_a_portal_batch_loses_its_styling(self):
        created = self.documents.with_user(self.portal).create([
            _values(self.folder, 'first.txt', _OVERLAY),
            _values(self.folder, 'second.txt', '<p>no styling</p>'),
            _values(self.folder, 'third.txt', '<span style="color:red">red</span>')])
        for document in created.sudo():
            with self.subTest(file=document.name):
                self.assertNotRegex(document.description, _STYLING)
        self.assertEqual(created.sudo().mapped('description')[1], '<p>no styling</p>')
        self.assertIn('red', created.sudo().mapped('description')[2])

    def test_the_description_of_a_document_user_keeps_its_styling(self):
        """Staff format their own text: only the text of a portal contact is held to this."""
        for label, documents in (('document user', self.documents.with_user(self.member)),
                                 ('superuser', self.documents.sudo())):
            with self.subTest(user=label):
                description = documents.create(
                    _values(self.folder, label + '-overlay.txt', _OVERLAY)).sudo().description
                self.assertIn('position:fixed', description)
                self.assertIn('class="o_view_nocontent text-bg-danger"', description)
                self.assertRegex(description, _STYLING)

    def test_every_file_of_a_batch_is_checked(self):
        with self.assertRaises(WoowUploadRejected):
            self.documents.with_user(self.portal).create([
                _values(self.folder, 'first.txt', '<p>ok</p>'),
                _values(self.folder, 'second.txt', _PATHOLOGICAL)])
        self.assertEqual(self._count(), 0)

    def test_the_limit_is_for_portal_contacts_only(self):
        """Document users and the system (cron, the superuser) send what they like."""
        long = '<p>' + 'x' * (3 * PORTAL_DESCRIPTION_MAX_CHARS) + '</p>'
        for label, documents in (('document user', self.documents.with_user(self.member)),
                                 ('superuser', self.documents.sudo())):
            with self.subTest(user=label):
                document = documents.create(_values(self.folder, label + '.txt', long))
                self.assertEqual(document.sudo().description, long)


@tagged('woow_docmgmt_http')
class TestPortalDescriptionCapRpc(HttpCase):

    def _call_kw(self, model, method, args):
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 'desc',
                              'params': {'model': model, 'method': method,
                                         'args': args, 'kwargs': {}}})
        response = self.url_open('/web/dataset/call_kw/%s/%s' % (model, method),
                                 data=payload, headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_portal_user_cannot_send_a_long_description_over_rpc(self):
        password = secrets.token_urlsafe(16)
        portal, _member, folder = _fixture(self.env, 'rpc', password=password)
        self.authenticate(portal.login, password)
        count = lambda: self.env['woow.document'].sudo().search_count(  # noqa: E731
            [('folder_id', '=', folder.id)])
        with patch('odoo.fields.html_sanitize', wraps=html_sanitize) as sanitize:
            refused = self._call_kw('woow.document', 'create',
                                    [[_values(folder, 'long.txt', _PATHOLOGICAL)]])
            sanitize.assert_not_called()
        self.assertIn('error', refused)
        self.assertEqual(refused['error']['data']['message'], 'The file was not accepted.')
        self.assertEqual(count(), 0)
        created = self._call_kw('woow.document', 'create',
                                [[_values(folder, 'short.txt', '<p>ok</p>')]])
        self.assertNotIn('error', created)
        self.assertEqual(len(created['result']), 1)
        self.assertEqual(count(), 1)

    def test_a_portal_user_cannot_send_styling_over_rpc_and_a_document_user_can(self):
        """The call of the web client, from the account that is not staff and from the one that is."""
        password = secrets.token_urlsafe(16)
        portal, member, folder = _fixture(self.env, 'rpc-style', password=password)
        stored = {}
        for who, user in (('portal', portal), ('document user', member)):
            self.authenticate(user.login, password)
            answer = self._call_kw('woow.document', 'create',
                                   [[_values(folder, who.replace(' ', '-') + '.txt', _OVERLAY)]])
            self.assertNotIn('error', answer, answer)
            stored[who] = self.env['woow.document'].sudo().browse(answer['result']).description
        self.assertNotRegex(stored['portal'], _STYLING)
        self.assertNotIn('position', stored['portal'])
        self.assertIn('Your session has expired', stored['portal'])
        self.assertIn('href="https://example.test/login"', stored['portal'])
        self.assertIn('position:fixed', stored['document user'])
        self.assertRegex(stored['document user'], _STYLING)
