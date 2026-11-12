# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""A refused request for member rows takes no lock and updates no folder.

Creating or moving member rows takes an advisory lock and updates the row of every folder that the
request names, so that two requests for one folder run one after the other. Those are effects that
other sessions see, so they are for a caller who has the right to the folders only: the right to the
model, the form of the folder ids and the manage rank are checked first, whatever number of folders
the request names.
"""
import contextlib
import json
import secrets
from unittest.mock import patch

from odoo import sql_db
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models import folder_access

_USER = 'woow_document_management.group_woow_document_user'
_UPDATE = 'UPDATE woow_document_folder SET id = id'


@contextlib.contextmanager
def _watched():
    """(the calls of the lock helper of the member model, the updates of folder rows) made inside."""
    updates = []
    execute = sql_db.Cursor.execute

    def counting(cursor, query, params=None, *args, **kwargs):
        text = getattr(query, 'code', query)
        if isinstance(text, str) and _UPDATE in text:
            updates.append(params)
        return execute(cursor, query, params, *args, **kwargs)

    with patch.object(folder_access, 'lock', wraps=folder_access.lock) as locks, \
            patch.object(sql_db.Cursor, 'execute', counting):
        yield locks, updates


def _fixture(env, suffix, password=None):
    """Three folders, the callers that may not manage them and one that manages the first two."""
    admin = env.ref('base.user_admin')
    folders = env['woow.document.folder'].sudo().create([
        {'name': 'Access order %s %d' % (suffix, number), 'owner_id': admin.id}
        for number in range(3)])
    callers = {
        'portal': new_test_user(env, login='wdoc-order-portal-%s' % suffix,
                                groups='base.group_portal', password=password),
        'internal': new_test_user(env, login='wdoc-order-internal-%s' % suffix,
                                  groups='base.group_user'),
        'document user': new_test_user(env, login='wdoc-order-reader-%s' % suffix, groups=_USER)}
    manager = new_test_user(env, login='wdoc-order-manager-%s' % suffix, groups=_USER)
    access = env['woow.document.folder.access'].sudo()
    access.create({'folder_id': folders[0].id, 'subject_type': 'user',
                   'user_id': callers['document user'].id, 'level': 'edit'})
    for folder in folders[:2]:
        access.create({'folder_id': folder.id, 'subject_type': 'user', 'user_id': manager.id,
                       'level': 'manage'})
    env.flush_all()
    return folders, callers, manager


def _rows(folder_ids, user, **extra):
    return [dict({'folder_id': folder_id, 'subject_type': 'user', 'user_id': user.id,
                  'level': 'read'}, **extra) for folder_id in folder_ids]


@tagged('woow_docmgmt_core')
class TestMemberRowsAreAuthorizedBeforeTheyAreLocked(TransactionCase):

    def setUp(self):
        super().setUp()
        self.folders, self.callers, self.manager = _fixture(self.env, 'orm')
        self.access = self.env['woow.document.folder.access']
        self.newcomers = [new_test_user(self.env, login='wdoc-order-new-%d' % number, groups=_USER)
                          for number in range(3)]

    def _assert_refused_without_locking(self, caller, call):
        """The call ends in an error (an AccessError is a UserError) with no lock and no update."""
        with _watched() as (locks, updates), self.assertRaises(UserError) as caught:
            call()
        self.assertEqual((locks.call_count, updates), (0, []), caller)
        return caught.exception

    def test_a_request_for_many_folders_is_refused_before_any_lock(self):
        """Folders that exist and ones that do not, for a caller who manages none of them."""
        ids = self.folders.ids + list(range(10 ** 6, 10 ** 6 + 197))
        for name, caller in self.callers.items():
            with self.subTest(caller=name):
                self._assert_refused_without_locking(
                    name, lambda: self.access.with_user(caller).create(
                        _rows(ids, self.newcomers[0])))

    def test_other_spellings_of_one_folder_are_refused_before_any_lock(self):
        """``'7'``, ``'07'`` and ``'007'`` are one folder, and not a plain id."""
        spellings = ['0' * number + str(self.folders[0].id) for number in range(30)]
        for name, caller in self.callers.items():
            with self.subTest(caller=name):
                refused = self._assert_refused_without_locking(
                    name, lambda: self.access.with_user(caller).create(
                        _rows(spellings, self.newcomers[0])))
                # a caller with no right to the model is told so, before anything else is looked at
                self.assertIsInstance(refused, UserError)
                self.assertEqual(isinstance(refused, AccessError), name != 'document user')

    def test_a_value_that_is_not_a_plain_id_is_refused(self):
        reader = self.callers['document user']
        for value in (True, 1.0, self.folders[0], [self.folders[0].id], '1', -1):
            with self.subTest(value=value):
                self._assert_refused_without_locking(
                    'document user', lambda: self.access.with_user(reader).create(
                        _rows([value], self.newcomers[0])))

    def test_one_folder_that_the_caller_does_not_manage_refuses_the_whole_list(self):
        for ids in (self.folders.ids, list(reversed(self.folders.ids)), self.folders[1:].ids):
            with self.subTest(ids=ids):
                self._assert_refused_without_locking(
                    'manager', lambda: self.access.with_user(self.manager).create(
                        _rows(ids, self.newcomers[0])))
        self.assertFalse(self.access.sudo().search([('user_id', '=', self.newcomers[0].id)]))

    def test_moving_a_row_is_authorized_before_it_is_locked(self):
        row = self.access.sudo().create(_rows([self.folders[0].id], self.newcomers[0])[0])
        spelled = '0%d' % self.folders[1].id
        cases = [(name, caller, target) for name, caller in self.callers.items()
                 for target in (self.folders[1].id, spelled)]
        cases += [('manager', self.manager, self.folders[2].id),
                  ('manager', self.manager, spelled)]
        for name, caller, target in cases:
            for label, records in (('no row', self.access.browse()), ('a row', row)):
                with self.subTest(caller=name, target=target, records=label):
                    self._assert_refused_without_locking(
                        name, lambda: records.with_user(caller).write({'folder_id': target}))

    def test_a_manager_still_adds_rows_for_several_folders_in_one_call(self):
        """One lock and one update for each distinct folder, as before."""
        manager = self.access.with_user(self.manager)
        rows = _rows([self.folders[0].id, self.folders[1].id, self.folders[0].id],
                     self.newcomers[0])
        rows = [dict(row, user_id=user.id) for row, user in zip(rows, self.newcomers)]
        with _watched() as (locks, updates):
            created = manager.create(rows)
        self.assertEqual((len(created), locks.call_count, len(updates)), (3, 2, 2))
        self.assertEqual(sorted(row.folder_id.id for row in created),
                         sorted([self.folders[0].id] * 2 + [self.folders[1].id]))
        # a move to another folder of the same caller
        with _watched() as (locks, updates):
            created[0].with_user(self.manager).write({'folder_id': self.folders[1].id})
        self.assertEqual((locks.call_count, len(updates)), (1, 1))
        # and the superuser, which is not asked for rights, takes the same locks
        with _watched() as (locks, updates):
            self.access.sudo().create(_rows(self.folders.ids, self.newcomers[0])[2:])
        self.assertEqual((locks.call_count, len(updates)), (1, 1))


@tagged('woow_docmgmt_http')
class TestMemberRowsAreAuthorizedBeforeTheyAreLockedRpc(HttpCase):

    def _call_kw(self, model, method, args):
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 'order',
                              'params': {'model': model, 'method': method,
                                         'args': args, 'kwargs': {}}})
        response = self.url_open('/web/dataset/call_kw/%s/%s' % (model, method),
                                 data=payload, headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_portal_session_is_refused_before_any_lock(self):
        password = secrets.token_urlsafe(16)
        folders, callers, _manager = _fixture(self.env, 'rpc', password=password)
        newcomer = new_test_user(self.env, login='wdoc-order-rpc-new', groups=_USER)
        self.authenticate(callers['portal'].login, password)
        ids = folders.ids + list(range(10 ** 6, 10 ** 6 + 197))
        with _watched() as (locks, updates):
            refused = self._call_kw('woow.document.folder.access', 'create',
                                    [_rows(ids, newcomer)])
        self.assertIn('error', refused)
        self.assertEqual(refused['error']['data']['name'], 'odoo.exceptions.AccessError')
        self.assertEqual((locks.call_count, updates), (0, []))
        self.assertFalse(self.env['woow.document.folder.access'].sudo().search([
            ('user_id', '=', newcomer.id)]))
