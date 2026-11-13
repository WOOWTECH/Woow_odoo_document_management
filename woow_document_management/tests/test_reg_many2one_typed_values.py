# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The company, the parent and the owner of a folder are judged by the id that the ORM stores.

``Many2one.convert_to_cache`` keeps an id only for an integer (a bool is not one), a record and a pair
(id, name); a list with something in it, a text, a float, a flag and a dict are stored as NULL. A
guard that read an id from such a value (the first element of a list, a number from a text, anything
that is truthy) judged a company, a parent or an owner that was not the one that got stored. Through
``/web/dataset/call_kw``, which takes JSON as it comes, a member of a folder who is not a document
administrator could so clear the company of a member row or of a root folder (the folders of a
company are then open to the administrators of every other company), or move a folder in the trash
to the top level and restore it where the site does not allow root folders.

The guards read the value once (``_m2o_value``), write the id that was judged back into the values and
refuse a value that has no id; after the ORM has stored the folder, the parent and the company that it
holds are compared with the ones that were judged. A write of the integer, the record and the pair,
the restore wizard and ``action_restore`` work as before.
"""
import json
from contextlib import closing
from unittest.mock import patch

from odoo import models
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_ADMINS = 'Only document administrators can do this.'
_COMPANY = 'Choose an available company.'
_FOLDER = 'Choose an available folder.'
_NOT_A_USER = 'Choose a document user.'
_FIELD = 'This folder field cannot be changed directly.'
_NO_SIGNUP_MAIL = {'no_reset_password': True}


def shapes(value):
    """The values that carry ``value`` in a shape that the ORM stores as NULL (JSON can carry them)."""
    return [('a list', [value]), ('a list with a name', [value, 'name']), ('a text', str(value)),
            ('a float', float(value)), ('true', True), ('an empty dict', {})]


class _Fixture:
    """A caller who manages folders but is no document administrator, and the folders to write to.

    The folders are made by the superuser, so that no guard is in the way. The site does not allow
    root folders (the default), the folders belong to the company of the caller, and an administrator
    of another company is there to see what becomes visible to it.
    """

    @classmethod
    def _make_fixture(cls):
        env = cls.env
        cls.admin = env.ref('base.user_admin')
        cls.company_a = env.company
        cls.company_b = env['res.company'].sudo().create({'name': 'Typed company B'})
        cls.manager = new_test_user(env, login='typed_manager', groups=_USER,
                                    context=_NO_SIGNUP_MAIL)
        cls.candidate = new_test_user(env, login='typed_candidate', groups=_USER,
                                      context=_NO_SIGNUP_MAIL)
        cls.doc_admin = new_test_user(env, login='typed_doc_admin', groups=_MANAGER,
                                      context=_NO_SIGNUP_MAIL)
        cls.admin_ab = new_test_user(
            env, login='typed_admin_ab', groups=_MANAGER, context=_NO_SIGNUP_MAIL,
            company_ids=[(6, 0, [cls.company_a.id, cls.company_b.id])])
        cls.admin_b = new_test_user(
            env, login='typed_admin_b', groups=_MANAGER, company_id=cls.company_b.id,
            company_ids=[(6, 0, [cls.company_b.id])], context=_NO_SIGNUP_MAIL)
        folders = env['woow.document.folder'].sudo()
        cls.top = folders.create({'name': 'Typed top', 'owner_id': cls.admin.id})
        cls.parent = folders.create({'name': 'Typed parent', 'owner_id': cls.admin.id})
        cls.other_parent = folders.create({'name': 'Typed other parent', 'owner_id': cls.admin.id})
        cls.third = folders.create({'name': 'Typed third', 'owner_id': cls.admin.id})
        cls.child = folders.create({'name': 'Typed child', 'parent_id': cls.parent.id,
                                    'owner_id': cls.admin.id})
        cls.trashed_top = folders.create({'name': 'Typed trashed top', 'owner_id': cls.admin.id})
        cls.trashed_child = folders.create({'name': 'Typed trashed child',
                                            'parent_id': cls.parent.id, 'owner_id': cls.admin.id})
        access = env['woow.document.folder.access'].sudo()
        for folder in (cls.top, cls.parent, cls.other_parent, cls.third, cls.trashed_top):
            access.create({'folder_id': folder.id, 'subject_type': 'user',
                           'user_id': cls.manager.id, 'level': 'manage'})
        cls.row_top = access.search([('folder_id', '=', cls.top.id)])
        # a member of the child who also has a grant on every folder that the child moves to
        for folder in (cls.parent, cls.other_parent, cls.third):
            access.create({'folder_id': folder.id, 'subject_type': 'user',
                           'user_id': cls.candidate.id, 'level': 'read'})
        cls.row_child = access.create({'folder_id': cls.child.id, 'subject_type': 'user',
                                       'user_id': cls.candidate.id, 'level': 'read'})
        documents = env['woow.document'].sudo()
        cls.documents = documents.create([
            {'name': 'typed-%s.txt' % name, 'folder_id': folder.id,
             'datas': 'dHlwZWQgZml4dHVyZQ=='}
            for name, folder in (('top', cls.top), ('trashed', cls.trashed_top),
                                 ('child', cls.child))])
        env.flush_all()
        for folder in (cls.trashed_top, cls.trashed_child):
            folder.with_user(cls.admin).action_trash()
        env.flush_all()
        cls.folders = cls.top | cls.parent | cls.other_parent | cls.third | cls.child \
            | cls.trashed_top | cls.trashed_child
        cls.rows = cls.row_top | cls.row_child

    def _held(self):
        """What the folders and the rows hold, read around every cache and rule."""
        self.env.invalidate_all()
        folders = self.folders.sudo().with_context(active_test=False)
        return {
            'folders': {folder.id: (folder.parent_id.id, folder.company_id.id, folder.owner_id.id,
                                    folder.active) for folder in folders},
            'rows': {row.id: (row.folder_id.id, row.company_id.id)
                     for row in self.rows.sudo()},
            'documents': {doc.id: doc.company_id.id for doc in self.documents.sudo()},
        }

    def _seen_by_the_other_company(self):
        """How many of the folders, rows and documents an administrator of company B finds."""
        env = self.env
        folders = env['woow.document.folder'].with_user(self.admin_b).with_context(active_test=False)
        rows = env['woow.document.folder.access'].with_user(self.admin_b)
        documents = env['woow.document'].with_user(self.admin_b).with_context(active_test=False)
        return (folders.search_count([('id', 'in', self.folders.ids)]),
                rows.search_count([('id', 'in', self.rows.ids)]),
                documents.search_count([('id', 'in', self.documents.ids)]))


@tagged('woow_docmgmt_http')
class TestAValueOfTheWrongShapeIsRefusedOverRpc(_Fixture, HttpCase):
    """What the web client never sends and ``/web/dataset/call_kw`` takes: JSON in every shape."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def _call(self, user, model, method, args, context=None):
        """The JSON answer of ``/web/dataset/call_kw`` for ``user``."""
        self.authenticate(user.login, user.login)
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 1, 'params': {
            'model': model, 'method': method, 'args': args,
            'kwargs': {'context': context} if context else {}}})
        response = self.url_open('/web/dataset/call_kw/%s/%s' % (model, method), data=payload,
                                 headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        answer = response.json()
        self.env.invalidate_all()
        return answer

    def _refused(self, answer, name, message):
        """``answer`` is a refusal of this kind and says this."""
        self.assertIn('error', answer, answer)
        data = answer['error']['data']
        self.assertEqual((data['name'], data['message']), (name, message))

    def _cases(self):
        """(label, model, id, field, the id that the caller means, context, the refusal), per target."""
        trashed = {'active_test': False}
        refusal = {
            'company_id': ('odoo.exceptions.AccessError', _ADMINS),
            'parent_id': ('odoo.exceptions.ValidationError', _FOLDER),
            'owner_id': ('odoo.exceptions.ValidationError', _NOT_A_USER)}
        meant = {'company_id': self.company_a.id, 'parent_id': self.other_parent.id,
                 'owner_id': self.candidate.id}
        cases = [('a member row', 'woow.document.folder.access', self.row_top.id, 'company_id',
                  self.company_a.id, None, ('odoo.exceptions.AccessError', _FIELD)),
                 ('a member row below a parent', 'woow.document.folder.access', self.row_child.id,
                  'company_id', self.company_a.id, None, ('odoo.exceptions.AccessError', _FIELD))]
        for label, folder, context in (
                ('an active root folder', self.top, None),
                ('an active folder below a parent', self.child, None),
                ('a folder in the trash', self.trashed_top, trashed),
                ('a folder in the trash below a parent', self.trashed_child, trashed)):
            for field in ('company_id', 'parent_id', 'owner_id'):
                cases.append((label, 'woow.document.folder', folder.id, field, meant[field],
                              context, refusal[field]))
        return cases

    def test_a_value_that_the_orm_stores_as_null_is_refused_for_every_target_and_field(self):
        """A list, a text, a float, a flag and a dict, as the company, the parent and the owner."""
        before = self._held()
        self.assertEqual(self._seen_by_the_other_company(), (0, 0, 0))
        for label, model, record_id, field, meant, context, (name, message) in self._cases():
            for shape, value in shapes(meant):
                with self.subTest(target=label, field=field, shape=shape):
                    answer = self._call(self.manager, model, 'write',
                                        [[record_id], {field: value}], context)
                    self._refused(answer, name, message)
                    self.assertEqual(self._held(), before)
        self.assertEqual(self._seen_by_the_other_company(), (0, 0, 0))

    def test_the_folders_that_the_attempts_aimed_at_are_restored_where_they_were(self):
        """The restore after a refused write puts a folder back under its parent, in its company."""
        before = self._held()
        for field, meant in (('parent_id', self.other_parent.id), ('company_id', self.company_a.id)):
            for shape, value in shapes(meant):
                with self.subTest(field=field, shape=shape):
                    for folder in (self.trashed_child, self.trashed_top):
                        answer = self._call(self.manager, 'woow.document.folder', 'write',
                                            [[folder.id], {field: value}], {'active_test': False})
                        self.assertIn('error', answer)
        for folder in (self.trashed_child, self.trashed_top):
            answer = self._call(self.manager, 'woow.document.folder', 'action_restore',
                                [[folder.id]], {'active_test': False})
            self.assertNotIn('error', answer, answer)
        held = self._held()['folders']
        self.assertEqual(held[self.trashed_child.id][:2], (self.parent.id, self.company_a.id))
        self.assertEqual(held[self.trashed_top.id][:2], (False, self.company_a.id))
        self.assertTrue(held[self.trashed_child.id][3] and held[self.trashed_top.id][3])
        self.assertEqual(self._seen_by_the_other_company(), (0, 0, 0))
        # the other folders did not change
        for key in before['folders']:
            if key not in (self.trashed_child.id, self.trashed_top.id):
                self.assertEqual(held[key], before['folders'][key])

    def test_a_document_administrator_gets_a_refusal_for_a_company_that_has_no_id(self):
        """The administrator may choose any company, but a list is not one: not a database error."""
        before = self._held()
        meant = self.company_a.id
        for label, folder, context in (
                ('an active root folder', self.top, None),
                ('an active folder below a parent', self.child, None),
                ('a folder in the trash', self.trashed_top, {'active_test': False})):
            for shape, value in shapes(meant) + [('zero', 0), ('a negative number', -meant)]:
                with self.subTest(target=label, shape=shape):
                    answer = self._call(self.doc_admin, 'woow.document.folder', 'write',
                                        [[folder.id], {'company_id': value}], context)
                    self._refused(answer, 'odoo.exceptions.ValidationError', _COMPANY)
                    self.assertEqual(self._held(), before)
        for shape, value in shapes(meant):
            with self.subTest(target='a new folder', shape=shape):
                answer = self._call(self.doc_admin, 'woow.document.folder', 'create',
                                    [{'name': 'Typed admin created', 'company_id': value}])
                self._refused(answer, 'odoo.exceptions.ValidationError', _COMPANY)
        # the rows of the members follow the company of their folder, for the administrator too
        for shape, value in shapes(meant):
            with self.subTest(target='a member row', shape=shape):
                answer = self._call(self.doc_admin, 'woow.document.folder.access', 'write',
                                    [[self.row_top.id], {'company_id': value}])
                self._refused(answer, 'odoo.exceptions.AccessError', _FIELD)
        self.assertEqual(self._held(), before)
        self.assertEqual(self.env['woow.document.folder'].sudo().search_count(
            [('name', '=', 'Typed admin created')]), 0)

    def test_a_new_folder_with_a_value_of_the_wrong_shape_is_refused(self):
        """The same shapes for the company, the parent and the owner of a folder that is made."""
        before = self._held()
        count = self.env['woow.document.folder'].sudo().with_context(active_test=False).search_count([])
        meant = {'company_id': self.company_a.id, 'parent_id': self.parent.id,
                 'owner_id': self.candidate.id}
        refusal = {'company_id': ('odoo.exceptions.AccessError', _ADMINS),
                   'parent_id': ('odoo.exceptions.ValidationError', _FOLDER),
                   'owner_id': ('odoo.exceptions.ValidationError', _NOT_A_USER)}
        for field, id_meant in meant.items():
            for shape, value in shapes(id_meant):
                with self.subTest(field=field, shape=shape):
                    values = {'name': 'Typed created %s %s' % (field, shape),
                              'parent_id': self.parent.id}
                    values[field] = value
                    answer = self._call(self.manager, 'woow.document.folder', 'create', [values])
                    self._refused(answer, *refusal[field])
        self.assertEqual(self._held(), before)
        self.assertEqual(self.env['woow.document.folder'].sudo().with_context(
            active_test=False).search_count([]), count)

    @mute_logger('odoo.sql_db', 'odoo.http')
    def test_a_new_member_row_takes_the_company_of_its_folder_whatever_the_call_names(self):
        """A row that a call makes holds the company of its folder, or the call fails: never another.

        (The database refuses a list or a flag for the column; that is logged, hence the muted loggers.)
        """
        for shape, value in shapes(self.company_a.id) + [('another company', self.company_b.id)]:
            with self.subTest(shape=shape):
                answer = self._call(self.manager, 'woow.document.folder.access', 'create', [{
                    'folder_id': self.top.id, 'subject_type': 'user', 'user_id': self.candidate.id,
                    'level': 'read', 'company_id': value}])
                if 'error' in answer:
                    continue
                row = self.env['woow.document.folder.access'].sudo().browse(answer['result'])
                self.assertEqual(row.company_id, self.company_a)
                row.unlink()

    def test_the_folder_of_a_member_row_is_judged_as_the_id_that_is_stored(self):
        """``folder_id`` is an integer that the caller manages, or it is refused, in every shape.

        An empty dict (or ``0``, ``''``, ``[]``) is no shape to try: the ORM stores NULL for it and the
        column is NOT NULL, so no row is ever made or moved with it.
        """
        before = self._held()
        meant = self.third.id
        for shape, value in [item for item in shapes(meant) if item[1] != {}]:
            with self.subTest(shape=shape, call='write'):
                answer = self._call(self.manager, 'woow.document.folder.access', 'write',
                                    [[self.row_top.id], {'folder_id': value}])
                self._refused(answer, 'odoo.exceptions.UserError', _FOLDER)
            with self.subTest(shape=shape, call='create'):
                answer = self._call(self.manager, 'woow.document.folder.access', 'create', [{
                    'folder_id': value, 'subject_type': 'user', 'user_id': self.candidate.id,
                    'level': 'read'}])
                self._refused(answer, 'odoo.exceptions.UserError', _FOLDER)
        self.assertEqual(self._held(), before)

    def test_the_integer_and_the_forms_that_the_orm_keeps_still_work(self):
        """The control: what the web client sends, and the record and the pair of Python callers."""
        manager = self.manager
        # the web client sends the id, or false
        answer = self._call(manager, 'woow.document.folder', 'write',
                            [[self.child.id], {'parent_id': self.other_parent.id}])
        self.assertNotIn('error', answer, answer)
        self.assertEqual(self.child.sudo().parent_id, self.other_parent)
        answer = self._call(manager, 'woow.document.folder', 'write',
                            [[self.child.id], {'parent_id': self.parent.id,
                                               'company_id': self.company_a.id,
                                               'owner_id': self.candidate.id}])
        self.assertNotIn('error', answer, answer)
        self.assertEqual((self.child.sudo().parent_id, self.child.sudo().company_id,
                          self.child.sudo().owner_id), (self.parent, self.company_a, self.candidate))
        answer = self._call(manager, 'woow.document.folder.access', 'write',
                            [[self.row_top.id], {'company_id': self.company_a.id, 'level': 'manage'}])
        self.assertNotIn('error', answer, answer)
        self.assertEqual((self.row_top.sudo().company_id, self.row_top.sudo().level),
                         (self.company_a, 'manage'))
        answer = self._call(manager, 'woow.document.folder', 'create', [{
            'name': 'Typed created fine', 'parent_id': self.parent.id,
            'company_id': self.company_a.id, 'owner_id': self.candidate.id}])
        self.assertNotIn('error', answer, answer)
        created = self.env['woow.document.folder'].sudo().browse(answer['result'])
        self.assertEqual((created.parent_id, created.company_id, created.owner_id),
                         (self.parent, self.company_a, self.candidate))
        # a document administrator may choose any company, and none
        answer = self._call(self.doc_admin, 'woow.document.folder', 'create',
                            [{'name': 'Typed without a company', 'company_id': False}])
        self.assertNotIn('error', answer, answer)
        free = self.env['woow.document.folder'].sudo().browse(answer['result'])
        self.assertFalse(free.company_id)
        for company in (self.company_a.id, False):
            answer = self._call(self.doc_admin, 'woow.document.folder', 'write',
                                [[free.id], {'company_id': company}])
            self.assertNotIn('error', answer, answer)
            self.assertEqual(free.sudo().company_id.id, company)


@tagged('woow_docmgmt_core')
class TestWhatTheOrmStoresIsWhatWasJudged(_Fixture, TransactionCase):
    """The reading of a value, the id that is written back, and the comparison after the store."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()
        cls.folder_model = cls.env['woow.document.folder']

    def _storing(self, **stored):
        """A context in which the ORM stores ``stored`` for the folders it makes, whatever the values said."""
        original = models.BaseModel._create

        def alter(records, data_list):
            if records._name == 'woow.document.folder':
                for data in data_list:
                    data['stored'].update(stored)
            return original(records, data_list)

        return patch.object(models.BaseModel, '_create', autospec=True, side_effect=alter)

    def _writing(self, **stored):
        """The same for a write: the ORM changes ``stored`` on the folders, whatever the write said."""
        original = models.BaseModel.write

        def alter(records, vals):
            if records._name == 'woow.document.folder':
                vals = dict(vals, **stored)
            return original(records, vals)

        return patch.object(models.BaseModel, 'write', autospec=True, side_effect=alter)

    def _refused(self, call, message, error=AccessError):
        with self.assertRaises(error) as caught, closing(self.cr.savepoint()):
            call()
        self.assertEqual(caught.exception.args, (message,))

    def test_the_reading_of_a_company_follows_the_orm(self):
        """The id that a company value names, and a refusal where the ORM keeps NULL."""
        company = self.company_a
        read = self.folder_model._woow_company_value
        for value, expected in ((company.id, company.id), (company, company.id),
                                ((company.id, 'name'), company.id), (False, False), (None, False),
                                ([], False), ((None, 'x'), False), ((), False)):
            for manager in (False, True):
                with self.subTest(value=value, manager=manager):
                    self.assertEqual(read(value, manager), expected)
        for value in ([company.id], [company.id, 'name'], str(company.id), float(company.id), True,
                      {}, {'id': company.id}, company | self.company_b, self.manager,
                      ('1',), (True,), 0, -1):
            with self.subTest(value=value, manager=False), self.assertRaises(AccessError):
                read(value, False)
            with self.subTest(value=value, manager=True), self.assertRaises(ValidationError):
                read(value, True)

    def test_the_forms_that_the_orm_keeps_are_judged_as_the_id_they_hold(self):
        """The number, the record and the pair of an id and a name: the same folder, company, owner."""
        folders = self.folder_model.with_user(self.manager)
        for label, wrap in (('a number', lambda record: record.id), ('a record', lambda record: record),
                            ('a pair', lambda record: (record.id, 'name'))):
            with self.subTest(label):
                self.child.sudo().write({'parent_id': self.parent.id})
                self.child.with_user(self.manager).write({
                    'parent_id': wrap(self.other_parent), 'company_id': wrap(self.company_a),
                    'owner_id': wrap(self.candidate)})
                self.assertEqual((self.child.parent_id, self.child.company_id, self.child.owner_id),
                                 (self.other_parent, self.company_a, self.candidate))
                created = folders.create({
                    'name': 'Typed form %s' % label, 'parent_id': wrap(self.parent),
                    'company_id': wrap(self.company_a), 'owner_id': wrap(self.candidate)})
                self.assertEqual((created.parent_id, created.company_id, created.owner_id),
                                 (self.parent, self.company_a, self.candidate))
                line = self.row_top.with_user(self.manager)
                line.write({'company_id': wrap(self.company_a)})
                self.assertEqual(line.company_id, self.company_a)

    def test_an_empty_list_and_none_are_no_record_for_a_document_administrator(self):
        """What may be left out is left out: the id that was judged (False) is what is stored."""
        admin = self.folder_model.with_user(self.doc_admin)
        for empty in ([], None, False):
            with self.subTest(empty=empty):
                made = admin.create({'name': 'Typed empty %r' % (empty,), 'parent_id': empty,
                                     'company_id': empty, 'owner_id': empty})
                self.assertEqual((made.parent_id.id, made.company_id.id, made.owner_id.id),
                                 (False, False, False))
                made.write({'company_id': self.company_a.id})
                made.write({'parent_id': self.parent.id, 'owner_id': self.candidate.id})
                self.assertEqual((made.parent_id, made.company_id, made.owner_id),
                                 (self.parent, self.company_a, self.candidate))
                made.write({'parent_id': empty, 'company_id': empty, 'owner_id': empty})
                self.assertEqual((made.parent_id.id, made.company_id.id, made.owner_id.id),
                                 (False, False, False))

    def test_a_member_row_of_a_folder_without_a_company_holds_no_company_for_zero_too(self):
        """``0`` is no company: the row is stored without one and the cache holds no record for it."""
        folder = self.folder_model.sudo().create({'name': 'Typed open folder', 'company_id': False,
                                                  'owner_id': self.admin.id})
        row = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user', 'user_id': self.manager.id,
            'level': 'manage'})
        self.assertFalse(row.company_id)
        for value in (0, False, None, []):
            with self.subTest(value=value):
                row.with_user(self.manager).write({'company_id': value, 'level': 'manage'})
                self.assertEqual(row.company_id.ids, [])
                self.assertFalse(row.company_id)
                self.env.cr.execute('SELECT company_id FROM woow_document_folder_access WHERE id = %s',
                                    (row.id,))
                self.assertEqual(self.env.cr.fetchone(), (None,))

    def test_a_parent_that_the_orm_stores_instead_of_the_judged_one_is_refused(self):
        """The rules would accept the folder that is stored; it is not the one that was authorised."""
        before = self._held()
        manager_child = self.child.with_user(self.manager)
        with self._writing(parent_id=self.third.id):
            self._refused(lambda: manager_child.write({'parent_id': self.other_parent.id}), _FIELD)
        with self._storing(parent_id=self.third.id):
            self._refused(lambda: self.folder_model.with_user(self.manager).create(
                {'name': 'Typed stored parent', 'parent_id': self.other_parent.id}), _FIELD)
        self.assertEqual(self._held(), before)
        # the control: without the interference the same calls work
        manager_child.write({'parent_id': self.other_parent.id})
        self.assertEqual(self.child.parent_id, self.other_parent)

    def test_a_company_that_the_orm_stores_instead_of_the_judged_one_is_refused_for_an_administrator(self):
        """The administrator is asked nothing about the company, so only the comparison catches it."""
        solo = self.folder_model.sudo().create({'name': 'Typed solo', 'owner_id': False})
        before = self._held()
        admin_solo = solo.with_user(self.admin_ab)
        with self._writing(company_id=self.company_b.id):
            self._refused(lambda: admin_solo.write({'company_id': self.company_a.id}), _FIELD)
        with self._storing(company_id=self.company_b.id):
            self._refused(lambda: self.folder_model.with_user(self.admin_ab).create(
                {'name': 'Typed stored company', 'company_id': self.company_a.id}), _FIELD)
        self.assertEqual(self._held(), before)
        self.assertEqual(solo.company_id, self.company_a)
        # a company that the call does not name is not compared: the default is the site's business
        with self._storing(company_id=False):
            made = self.folder_model.with_user(self.doc_admin).create({'name': 'Typed no company named'})
        self.assertFalse(made.company_id)

    def test_the_restore_wizard_and_action_restore_work_as_before(self):
        manager = self.manager
        # a folder in the trash whose parent is available goes back under it, and a root goes back to the top
        self.trashed_child.with_user(manager).with_context(active_test=False).action_restore()
        self.trashed_top.with_user(manager).with_context(active_test=False).action_restore()
        self.assertEqual((self.trashed_child.active, self.trashed_child.parent_id,
                          self.trashed_child.company_id), (True, self.parent, self.company_a))
        self.assertEqual((self.trashed_top.active, self.trashed_top.parent_id.id,
                          self.trashed_top.company_id), (True, False, self.company_a))
        # a folder whose parent is in the trash is restored through the wizard, into a folder
        # that the caller names
        self.parent.with_user(self.admin).action_trash()
        self.child.with_user(self.admin).action_trash()
        action = self.child.with_user(manager).with_context(active_test=False).action_restore()
        self.assertEqual(action['res_model'], 'woow.document.restore.wizard')
        wizard = self.env['woow.document.restore.wizard'].with_user(manager).with_context(
            active_test=False).create({'folder_ids': [(6, 0, [self.child.id])],
                                       'target_folder_id': self.other_parent.id})
        wizard.action_restore_to()
        self.assertEqual((self.child.active, self.child.parent_id, self.child.company_id),
                         (True, self.other_parent, self.company_a))
