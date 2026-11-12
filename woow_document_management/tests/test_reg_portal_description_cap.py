# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""A portal contact's description of a file is short: core sanitises HTML, and that costs time.

The description is an HTML field that core sanitises twice on every create, and for some markup the
cost grows with the square of its length. A portal contact may send a description with a file, so
the module refuses a long one before anything is stored or sanitised.
"""
import base64
import json
import secrets
from unittest.mock import patch

from odoo import Command
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import html_sanitize

from ..models.upload_policy import WoowUploadRejected
from ..services.constants import PORTAL_DESCRIPTION_MAX_CHARS

# markup with nothing to close: the cheapest text that makes the sanitiser work hard
_PATHOLOGICAL = '<' * (10 * PORTAL_DESCRIPTION_MAX_CHARS)


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
    member = env['res.users'].sudo().create({
        'name': 'Description member %s' % suffix, 'login': 'wdoc-desc-member-%s' % suffix,
        'groups_id': [Command.set([
            env.ref('woow_document_management.group_woow_document_user').id])]})
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
