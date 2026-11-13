# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""``woow.document.create`` takes at most the per-request file limit, like the upload routes."""
import base64
import json
import secrets

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

_LIMIT = 3


def _values(folder, count, prefix):
    return [{'name': '%s-%d.txt' % (prefix, index), 'folder_id': folder.id,
             'datas': base64.b64encode(b'cap fixture %d' % index).decode()}
            for index in range(count)]


def _fixture(env, suffix, password=None):
    """A portal contact and a document user, each allowed to upload to one folder."""
    env['ir.config_parameter'].sudo().set_param(
        'woow_document_management.max_files_per_upload', str(_LIMIT))
    partner = env['res.partner'].sudo().create({'name': 'Cap portal contact %s' % suffix})
    portal_vals = {
        'name': 'Cap portal %s' % suffix, 'login': 'wdoc-cap-portal-%s' % suffix,
        'partner_id': partner.id,
        'groups_id': [Command.set([env.ref('base.group_portal').id])]}
    if password:
        portal_vals['password'] = password
    portal = env['res.users'].sudo().create(portal_vals)
    member = env['res.users'].sudo().create({
        'name': 'Cap member %s' % suffix, 'login': 'wdoc-cap-member-%s' % suffix,
        'groups_id': [Command.set([
            env.ref('woow_document_management.group_woow_document_user').id])]})
    folder = env['woow.document.folder'].sudo().create({
        'name': 'Cap folder %s' % suffix, 'owner_id': env.ref('base.user_admin').id})
    env['woow.document.folder.access'].sudo().create({
        'folder_id': folder.id, 'subject_type': 'partner', 'partner_id': partner.id,
        'level': 'upload'})
    env['woow.document.folder.access'].sudo().create({
        'folder_id': folder.id, 'subject_type': 'user', 'user_id': member.id,
        'level': 'upload'})
    env.flush_all()
    return portal, member, folder


@tagged('woow_docmgmt_files')
class TestCreateBatchCapOrm(TransactionCase):

    def test_a_batch_above_the_limit_is_refused_and_creates_nothing(self):
        portal, member, folder = _fixture(self.env, 'orm')
        documents = self.env['woow.document'].sudo()
        for label, user in (('portal', portal), ('document user', member)):
            with self.subTest(user=label):
                before = documents.search_count([('folder_id', '=', folder.id)])
                created = self.env['woow.document'].with_user(user).create(
                    _values(folder, _LIMIT, label))
                self.assertEqual(len(created), _LIMIT)
                with self.assertRaises(UserError) as caught:
                    self.env['woow.document'].with_user(user).create(
                        _values(folder, _LIMIT + 1, label + '-over'))
                self.assertEqual(str(caught.exception), 'Invalid number of files.')
                self.assertEqual(documents.search_count([('folder_id', '=', folder.id)]),
                                 before + _LIMIT)

    def test_the_cron_and_the_superuser_are_not_limited(self):
        _portal, _member, folder = _fixture(self.env, 'su')
        created = self.env['woow.document'].sudo().create(
            _values(folder, _LIMIT + 2, 'system'))
        self.assertEqual(len(created), _LIMIT + 2)


@tagged('woow_docmgmt_http')
class TestCreateBatchCapRpc(HttpCase):

    def _call_kw(self, model, method, args):
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 'cap',
                              'params': {'model': model, 'method': method,
                                         'args': args, 'kwargs': {}}})
        response = self.url_open('/web/dataset/call_kw/%s/%s' % (model, method),
                                 data=payload, headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_portal_user_cannot_create_more_files_than_a_request_may_carry(self):
        password = secrets.token_urlsafe(16)
        portal, _member, folder = _fixture(self.env, 'rpc', password=password)
        self.authenticate(portal.login, password)
        count = lambda: self.env['woow.document'].sudo().search_count(  # noqa: E731
            [('folder_id', '=', folder.id)])
        within = self._call_kw('woow.document', 'create', [_values(folder, _LIMIT, 'rpc')])
        self.assertNotIn('error', within)
        self.assertEqual(len(within['result']), _LIMIT)
        self.assertEqual(count(), _LIMIT)
        over = self._call_kw('woow.document', 'create', [_values(folder, _LIMIT + 1, 'over')])
        self.assertIn('error', over)
        self.assertEqual(over['error']['data']['name'], 'odoo.exceptions.UserError')
        self.assertEqual(over['error']['data']['message'], 'Invalid number of files.')
        self.assertEqual(count(), _LIMIT)
