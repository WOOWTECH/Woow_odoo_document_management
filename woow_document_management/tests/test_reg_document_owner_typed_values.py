# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The owner of a document is judged by the id that the ORM stores.

``Many2one.convert_to_cache`` keeps an id only for an integer (a bool is not one), a record and a pair
(id, name); a float, a flag, a list with something in it, a text and a dict are stored as NULL. The
owner check of a document compared the raw value with the caller's id, so ``2.0`` passed for the
caller 2 and cleared the owner of a document of someone else. The check now reads the value with the
reader that folders use (``_m2o_value``), refuses what has no id, lets a caller who is not a document
administrator name only himself, and writes the judged id back into the values.
"""
import json

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_NOT_A_USER = 'Choose a document user.'
_OTHER_OWNER = 'You cannot assign a different owner.'
_NO_SIGNUP_MAIL = {'no_reset_password': True}
_DATAS = 'dHlwZWQgZml4dHVyZQ=='


def untyped(uid):
    """The values that carry ``uid`` in a shape that the ORM stores as NULL, with the uid in a string."""
    return [('a float', float(uid)), ('true', True), ('a list', [uid]),
            ('a list with a name', [uid, 'name']), ('a text', str(uid)), ('an empty dict', {})]


class _Fixture:

    @classmethod
    def _make_fixture(cls):
        env = cls.env
        cls.admin = env.ref('base.user_admin')
        cls.editor = new_test_user(env, login='dot_editor', groups=_USER, context=_NO_SIGNUP_MAIL)
        cls.candidate = new_test_user(env, login='dot_candidate', groups=_USER,
                                      context=_NO_SIGNUP_MAIL)
        cls.doc_admin = new_test_user(env, login='dot_doc_admin', groups=_MANAGER,
                                      context=_NO_SIGNUP_MAIL)
        cls.folder = env['woow.document.folder'].sudo().create({
            'name': 'Owner typed folder', 'owner_id': cls.admin.id})
        access = env['woow.document.folder.access'].sudo()
        for user, level in ((cls.editor, 'edit'), (cls.doc_admin, 'manage')):
            access.create({'folder_id': cls.folder.id, 'subject_type': 'user',
                           'user_id': user.id, 'level': level})
        cls.doc = env['woow.document'].sudo().create({
            'name': 'owner-typed.txt', 'folder_id': cls.folder.id, 'datas': _DATAS,
            'owner_id': cls.admin.id})
        env.flush_all()

    def _owner(self):
        self.env.invalidate_all()
        return self.doc.sudo().owner_id.id

    def _vals(self, name, **extra):
        return dict({'name': name, 'folder_id': self.folder.id, 'datas': _DATAS}, **extra)

    def _count(self, name):
        return self.env['woow.document'].sudo().with_context(active_test=False).search_count(
            [('name', '=', name)])


@tagged('woow_docmgmt_http')
class TestTheOwnerOfAValueOfTheWrongShapeIsRefusedOverRpc(_Fixture, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def _call(self, user, method, args):
        self.authenticate(user.login, user.login)
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 1, 'params': {
            'model': 'woow.document', 'method': method, 'args': args, 'kwargs': {}}})
        response = self.url_open('/web/dataset/call_kw/woow.document/%s' % method, data=payload,
                                 headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        answer = response.json()
        self.env.invalidate_all()
        return answer

    @mute_logger('odoo.http')
    def test_a_float_of_the_own_id_does_not_clear_the_owner_of_a_document_of_someone_else(self):
        uid = self.editor.id
        for label, value in untyped(uid):
            with self.subTest(label):
                answer = self._call(self.editor, 'write', [[self.doc.id], {'owner_id': value}])
                self.assertIn('error', answer, answer)
                self.assertEqual(self._owner(), self.admin.id)

    @mute_logger('odoo.http')
    def test_a_new_document_with_such_an_owner_is_refused_too(self):
        uid = self.editor.id
        for index, (label, value) in enumerate(untyped(uid)):
            name = 'rpc-created-%d.txt' % index
            with self.subTest(label):
                answer = self._call(self.editor, 'create', [[self._vals(name, owner_id=value)]])
                self.assertIn('error', answer, answer)
                self.assertEqual(self._count(name), 0)

    def test_the_own_id_is_still_accepted_over_rpc(self):
        answer = self._call(self.editor, 'write', [[self.doc.id], {'owner_id': self.editor.id}])
        self.assertNotIn('error', answer, answer)
        self.assertEqual(self._owner(), self.editor.id)
        answer = self._call(self.editor, 'create', [[self._vals(
            'rpc-own.txt', owner_id=self.editor.id)]])
        self.assertNotIn('error', answer, answer)
        self.assertEqual(self._count('rpc-own.txt'), 1)


@tagged('woow_docmgmt_core')
class TestTheOwnerOfADocumentIsJudgedAsTheIdThatIsStored(_Fixture, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def test_a_value_with_no_id_is_refused_for_an_editor_on_write_and_create(self):
        uid = self.editor.id
        docs = self.env['woow.document'].with_user(self.editor)
        for label, value in untyped(uid):
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    docs.browse(self.doc.id).write({'owner_id': value})
                self.assertEqual(self._owner(), self.admin.id)
                name = 'orm-%s.txt' % label.replace(' ', '-')
                with self.assertRaises(ValidationError):
                    docs.create(self._vals(name, owner_id=value))
                self.assertEqual(self._count(name), 0)

    def test_a_value_with_no_id_gets_the_message_of_the_folder_owner_check(self):
        docs = self.env['woow.document'].with_user(self.editor)
        for value in (float(self.editor.id), True, [self.editor.id], 0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as ctx:
                    docs.browse(self.doc.id).write({'owner_id': value})
                self.assertEqual(ctx.exception.args[0], _NOT_A_USER)

    def test_an_editor_may_name_only_himself(self):
        docs = self.env['woow.document'].with_user(self.editor)
        for value in (False, self.candidate.id, self.candidate):
            with self.subTest(value=value):
                with self.assertRaises(AccessError) as ctx:
                    docs.browse(self.doc.id).write({'owner_id': value})
                self.assertEqual(ctx.exception.args[0], _OTHER_OWNER)
                self.assertEqual(self._owner(), self.admin.id)
                with self.assertRaises(AccessError):
                    docs.create(self._vals('other-owner.txt', owner_id=value))

    def test_the_own_id_in_the_forms_that_the_orm_keeps_is_accepted(self):
        uid = self.editor.id
        docs = self.env['woow.document'].with_user(self.editor)
        for index, value in enumerate((uid, (uid, 'name'), self.editor)):
            with self.subTest(value=value):
                self.doc.sudo().write({'owner_id': self.admin.id})
                docs.browse(self.doc.id).write({'owner_id': value})
                self.assertEqual(self._owner(), uid)
                created = docs.create(self._vals('accepted-%d.txt' % index, owner_id=value))
                self.assertEqual(created.sudo().owner_id.id, uid)

    def test_a_document_administrator_can_assign_another_user_and_clear_the_owner(self):
        docs = self.env['woow.document'].with_user(self.doc_admin)
        docs.browse(self.doc.id).write({'owner_id': self.candidate.id})
        self.assertEqual(self._owner(), self.candidate.id)
        docs.browse(self.doc.id).write({'owner_id': False})
        self.assertEqual(self._owner(), False)
        created = docs.create(self._vals('admin-made.txt', owner_id=self.candidate.id))
        self.assertEqual(created.sudo().owner_id, self.candidate)
        created = docs.create(self._vals('admin-made-none.txt', owner_id=False))
        self.assertFalse(created.sudo().owner_id)

    def test_a_document_administrator_gets_a_refusal_for_a_value_with_no_id(self):
        docs = self.env['woow.document'].with_user(self.doc_admin)
        for label, value in untyped(self.candidate.id) + [('zero', 0), ('minus one', -1)]:
            with self.subTest(label):
                with self.assertRaises(ValidationError) as ctx:
                    docs.browse(self.doc.id).write({'owner_id': value})
                self.assertEqual(ctx.exception.args[0], _NOT_A_USER)
                self.assertEqual(self._owner(), self.admin.id)
                name = 'admin-bad-%s.txt' % label.replace(' ', '-')
                with self.assertRaises(ValidationError):
                    docs.create(self._vals(name, owner_id=value))
                self.assertEqual(self._count(name), 0)

    def test_the_superuser_path_is_unchanged(self):
        docs = self.env['woow.document'].sudo()
        docs.browse(self.doc.id).write({'owner_id': self.candidate.id})
        self.assertEqual(self._owner(), self.candidate.id)
        docs.browse(self.doc.id).write({'owner_id': False})
        self.assertEqual(self._owner(), False)
        created = docs.create(self._vals('su-made.txt', owner_id=self.candidate.id))
        self.assertEqual(created.owner_id, self.candidate)
