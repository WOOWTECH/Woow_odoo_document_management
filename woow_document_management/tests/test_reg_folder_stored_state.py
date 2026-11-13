# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""A folder is judged by what the ORM stores: one reading of each value, and the state is checked again.

Four rounds of hardening each closed the shapes that a review had named, and each time the next review
found another shape of the same class: the value that a guard judged was not the value that was stored
(another type, a default that came from another source, another transport). The class is closed by
three things that do not depend on the shape of the input, and the tests below pin each of them on its
own:

* the owner, the parent and the company are read once, as the ORM keeps them (``_m2o_value``), written
  into the values and stored as they were judged, and the defaults that a create decides on are read
  with the context that core gets (``_woow_without_ambient_defaults``);
* after the ORM has made or changed the folders, the rules of the owner, the parent and the company
  are asked again of what the records hold (``_woow_check_stored_state``), with the same helper
  functions as the checks before the call. That is the check that counts: the tests change what the ORM
  stores, or switch the checks before the call off, and the refusal stays;
* one answer is kept for the folders that share an owner, a parent and an access, in a key that holds
  all of them: the sets of folders below mix them, in both orders.

The last test walks many shapes of the values, with and without a stored default and a context default.
"""
import random
from unittest.mock import patch

from psycopg2 import Error as DatabaseError

from odoo import models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.document_folder import _m2o_value

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_CEILING = 'A member cannot be granted more access than on the parent folder.'
_NOT_A_USER = 'Choose a document user.'
_RIGHTS = 'You do not have enough rights on this folder.'
_ROOT = 'Only document administrators may create a root folder.'
_ADMINS = 'Only document administrators can do this.'
_FOLDER = 'Choose an available folder.'
_ROOT_PARAM = 'woow_document_management.allow_root_create'


class _Fixture:
    """The parents p1 to p5, a manager of all of them, and the users that the cases need."""

    @classmethod
    def _make_fixture(cls):
        env = cls.env
        cls.admin = env.ref('base.user_admin')
        cls.folders = env['woow.document.folder']
        cls.access = env['woow.document.folder.access']

        def user(name, groups=_USER):
            return new_test_user(env, login='stored_%s' % name, groups=groups)

        cls.manager = user('manager')              # manage on every parent: the caller of the cases
        cls.outsider = user('outsider')            # a document user with no grant anywhere
        cls.sibling = user('sibling')              # manage on p1
        cls.third = user('third')                  # manage on p5
        cls.editor = user('editor')                # edit on p1
        cls.parent_owner = user('parent_owner')    # the owner of p1
        cls.plain = user('plain', 'base.group_user')   # internal, no document group: owns p4
        cls.doc_admin = user('doc_admin', _MANAGER)
        owners = {'p1': cls.parent_owner, 'p2': cls.admin, 'p3': cls.admin, 'p4': cls.plain,
                  'p5': cls.admin}
        cls.parents = {}
        for name, owner in owners.items():
            cls.parents[name] = cls.folders.sudo().create({
                'name': 'Stored parent %s' % name, 'owner_id': owner.id})
        grants = [(cls.parents[name], cls.manager, 'manage') for name in owners]
        grants += [(cls.parents['p1'], cls.sibling, 'manage'), (cls.parents['p1'], cls.editor, 'edit'),
                   (cls.parents['p5'], cls.third, 'manage')]
        for folder, who, level in grants:
            cls.access.sudo().create({'folder_id': folder.id, 'subject_type': 'user',
                                      'user_id': who.id, 'level': level})
        env.flush_all()

    # -- helpers ---------------------------------------------------------------------------------
    def _folder(self, name, *, owner=None, restricted=True, parent='p1', members=(), company=None):
        """A folder made by the superuser, so that no guard is in the way; the manager manages it."""
        values = {'name': name, 'parent_id': self.parents[parent].id if parent else False,
                  'owner_id': owner.id if owner else False, 'inherit_access': not restricted}
        if company:
            values['company_id'] = company.id
        folder = self.folders.sudo().create(values)
        for who, level in members or ((self.manager, 'manage'),):
            self.access.sudo().create({'folder_id': folder.id, 'subject_type': 'user',
                                       'user_id': who.id, 'level': level})
        self.env.flush_all()
        return folder

    def _refused(self, call, message, error=ValidationError):
        """``call`` is refused with ``error`` that says ``message``, and changes nothing."""
        with self.assertRaises(error) as caught, self.cr.savepoint():
            call()
        self.assertEqual(caught.exception.args, (message,))

    def _count(self):
        return self.folders.sudo().with_context(active_test=False).search_count([])

    def _store_default(self, user, owner):
        """``user`` stores ``owner`` as their own default owner (any internal user may)."""
        self.env['ir.default'].with_user(user).set(
            'woow.document.folder', 'owner_id', owner.id, user_id=True)

    def _root_right(self, on=True):
        self.env['ir.config_parameter'].sudo().set_param(_ROOT_PARAM, 'True' if on else 'False')

    def _storing(self, **stored):
        """A context in which the ORM stores ``stored`` for the folders it makes, whatever the values said.

        What the ORM stores is what the guards of the values did not foresee: another shape of a value,
        a default of another kind. This is that, made by hand.
        """
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


@tagged('woow_docmgmt_core')
class TestTheOwnerThatIsCheckedIsTheOwnerThatIsStored(_Fixture, TransactionCase):
    """A default of the context decides nothing: core takes the stored default, and so does the check."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def _context_defaults(self):
        return {'False': False, 'None': None, 'the caller': self.manager.id,
                'the outsider': self.outsider.id, 'zero': 0, 'a text': '5',
                'the id of the outsider as text': str(self.outsider.id)}

    def _create(self, name, value, **extra):
        return self.folders.with_user(self.manager).with_context(default_owner_id=value).create(
            dict({'name': name, 'parent_id': self.parents['p1'].id}, **extra))

    def test_a_stored_default_owner_is_judged_whatever_the_context_default_says(self):
        """The reproduced bypass: a manager stores an outsider as the default and names the caller."""
        self._store_default(self.manager, self.outsider)
        before = self._count()
        for label, value in self._context_defaults().items():
            with self.subTest(label, folder='restricted'):
                self._refused(lambda: self._create('Stored ctx %s' % label, value,
                                                   inherit_access=False), _CEILING)
            with self.subTest(label, folder='restricted, flag as 0'):
                self._refused(lambda: self._create('Stored ctx zero %s' % label, value,
                                                   inherit_access=0), _CEILING)
        self.assertEqual(self._count(), before)
        # an owner that is no document user is refused whatever the context says as well
        self._store_default(self.manager, self.plain)
        for label, value in self._context_defaults().items():
            for inherit in (False, True):
                with self.subTest(label, folder='plain default', inherit=inherit):
                    self._refused(lambda: self._create('Stored plain %s' % label, value,
                                                       inherit_access=inherit), _NOT_A_USER)
        self.assertEqual(self._count(), before)

    def test_the_folder_that_is_made_has_exactly_the_owner_that_was_checked(self):
        """A folder that is made takes the stored default, whatever the context says: that is checked."""
        for owner, restricted in ((self.outsider, False), (self.sibling, True), (self.sibling, False)):
            self._store_default(self.manager, owner)
            for label, value in self._context_defaults().items():
                with self.subTest(owner=owner.login, restricted=restricted, context=label):
                    created = self._create('Stored kept %s %s %s' % (owner.id, restricted, label),
                                           value, inherit_access=not restricted)
                    self.assertEqual((created.owner_id, created.inherit_access),
                                     (owner, not restricted))

    def test_a_context_default_without_a_stored_default_gives_nothing(self):
        """Core ignores the key (the guard mixin takes it out), and so does the check."""
        for label, value in self._context_defaults().items():
            for restricted in (False, True):
                with self.subTest(label, restricted=restricted):
                    created = self._create('Stored only ctx %s %s' % (label, restricted), value,
                                           inherit_access=not restricted)
                    self.assertEqual(created.owner_id, self.manager)

    def test_the_owner_that_is_named_is_the_one_that_is_stored(self):
        """Named in the values, the owner is the one that is judged and the one that is stored."""
        for label, value in self._context_defaults().items():
            with self.subTest(label, folder='accepted'):
                created = self._create('Stored named %s' % label, value, owner_id=self.sibling.id,
                                       inherit_access=False)
                self.assertEqual(created.owner_id, self.sibling)
            with self.subTest(label, folder='refused'):
                self._refused(lambda: self._create('Stored named out %s' % label, value,
                                                   owner_id=self.outsider.id, inherit_access=False),
                              _CEILING)

    def test_an_owner_named_as_a_record_or_a_pair_is_stored_as_that_user(self):
        """What has an id is read once and stored as that id: a record and the pair of an id and a name."""
        for label, value in (('a number', self.sibling.id), ('a record', self.sibling),
                             ('a pair', (self.sibling.id, 'Stored sibling'))):
            with self.subTest(label):
                created = self.folders.with_user(self.manager).create({
                    'name': 'Stored owner shape %s' % label, 'parent_id': self.parents['p1'].id,
                    'owner_id': value, 'inherit_access': False})
                self.assertEqual(created.owner_id, self.sibling)
                folder = self._folder('Stored owner shape written %s' % label)
                folder.with_user(self.manager).write({'owner_id': value})
                self.assertEqual(folder.owner_id, self.sibling)

    def test_the_default_parent_of_the_context_is_the_one_that_is_judged_and_stored(self):
        """``default_parent_id`` is a key that a create keeps, so check and storage use the same one."""
        folders = self.folders.with_user(self.manager)
        created = folders.with_context(default_parent_id=self.parents['p2'].id).create(
            {'name': 'Stored parent from the context'})
        self.assertEqual(created.parent_id, self.parents['p2'])
        stranger = self.folders.sudo().create({'name': 'Stored nobody parent',
                                               'owner_id': self.admin.id})
        with self.assertRaises(AccessError), self.cr.savepoint():
            folders.with_context(default_parent_id=stranger.id).create(
                {'name': 'Stored parent without a right'})
        self._root_right(False)
        with self.assertRaises(AccessError), self.cr.savepoint():
            folders.with_context(default_parent_id=False).create({'name': 'Stored no parent'})


@tagged('woow_docmgmt_core')
class TestTheParentOfAFolderIsJudgedAsTheOrmKeepsIt(_Fixture, TransactionCase):
    """A value that has no id is refused: the ORM keeps NULL for it, which is the top level."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def _shapes(self, target):
        return {'a list': [target.id], 'a text': str(target.id), 'a float': float(target.id),
                'a flag': True, 'a dict': {'id': target.id}, 'a pair in a list': [target.id, 'name'],
                'zero': 0, 'a negative number': -1, 'a text of zero': '0',
                'several folders': target | self.parents['p3'],
                'a record of another model': self.manager.partner_id}

    def _inactive(self, name):
        """A folder that is in the trash: no check of the name looks at it."""
        folder = self._folder(name, restricted=False)
        folder.sudo().write({'active': False})
        self.env.flush_all()
        return folder.with_user(self.manager).with_context(active_test=False)

    def test_a_write_of_a_parent_that_has_no_id_is_refused_for_an_active_and_an_inactive_folder(self):
        target = self.parents['p2']
        for kind in ('an active folder', 'an inactive folder'):
            folder = (self._folder('Stored typed active', restricted=False).with_user(self.manager)
                      if kind == 'an active folder' else self._inactive('Stored typed inactive'))
            for label, value in self._shapes(target).items():
                with self.subTest(kind=kind, shape=label):
                    self._refused(lambda: folder.write({'parent_id': value}), _FOLDER)
                    self.assertEqual(folder.sudo().parent_id, self.parents['p1'])
            # the control: the top level is a root folder and needs the right to make one
            for label, value in (('False', False), ('None', None), ('an empty list', [])):
                with self.subTest(kind=kind, shape=label, control=True):
                    self._refused(lambda: folder.write({'parent_id': value}), _ROOT, AccessError)
                    self.assertEqual(folder.sudo().parent_id, self.parents['p1'])
            self.assertFalse(self.folders.sudo().search([('parent_id', '=', False),
                                                         ('name', 'like', 'Stored typed')]))

    def test_a_parent_that_has_no_id_is_not_taken_for_the_top_level_by_a_manager_who_may_make_one(self):
        self._root_right()
        folder = self._inactive('Stored typed root right')
        for label, value in self._shapes(self.parents['p2']).items():
            with self.subTest(label):
                self._refused(lambda: folder.write({'parent_id': value}), _FOLDER)
                self.assertEqual(folder.sudo().parent_id, self.parents['p1'])

    def test_a_create_with_a_parent_that_has_no_id_is_refused(self):
        self._root_right(False)
        folders = self.folders.with_user(self.manager)
        before = self._count()
        for label, value in self._shapes(self.parents['p2']).items():
            for extra in ({}, {'inherit_access': False}):
                with self.subTest(label, extra=extra):
                    self._refused(lambda: folders.create(
                        dict({'name': 'Stored typed create', 'parent_id': value}, **extra)), _FOLDER)
        self.assertEqual(self._count(), before)
        for label, value in (('False', False), ('None', None), ('an empty list', [])):
            with self.subTest(label, control=True):
                self._refused(lambda: folders.create({'name': 'Stored root', 'parent_id': value}),
                              _ROOT, AccessError)

    def test_the_shapes_of_a_parent_that_have_an_id_are_the_folder_they_name(self):
        """What the ORM keeps as an id: the number, the record, and the pair of an id and a name."""
        target = self.parents['p2']
        for label, value in (('a number', target.id), ('a record', target),
                             ('a pair', (target.id, 'Stored parent p2'))):
            with self.subTest(label, call='write'):
                folder = self._folder('Stored shape %s' % label, restricted=False)
                folder.with_user(self.manager).write({'parent_id': value})
                self.assertEqual(folder.parent_id, target)
            with self.subTest(label, call='create'):
                created = self.folders.with_user(self.manager).create({
                    'name': 'Stored shape created %s' % label, 'parent_id': value})
                self.assertEqual(created.parent_id, target)
        self._root_right()
        folder = self._folder('Stored shape top', restricted=False)
        folder.with_user(self.manager).write({'parent_id': False})
        self.assertFalse(folder.parent_id)

    def test_the_one_reading_of_a_many2one_value(self):
        """``_m2o_value`` is ``Many2one.convert_to_cache``, with a refusal where the ORM keeps NULL."""
        folder, other = self.parents['p1'], self.parents['p2']
        env = self.env
        for value, expected in (
                (False, False), (None, False), ([], False), (7, 7), (0, 0), (-3, -3),
                ((7, 'name'), 7), ((7,), 7), ((), False), ((None, 'x'), False),
                (folder, folder.id), (folder.browse(), False), (folder | other, None),
                (env['res.users'].browse(7), None),
                (True, None), ('7', None), (7.0, None), ({'id': 7}, None), ([7], None),
                ([7, 'name'], None), (('7',), None), ((True,), None)):
            with self.subTest(value=value):
                self.assertEqual(_m2o_value(value, 'woow.document.folder'), expected)
        self.assertEqual(_m2o_value(env['res.users'].browse(7), 'res.users'), 7)
        self.assertEqual(_m2o_value(env.company, 'res.company'), env.company.id)
        # the same reading for the parent and for the owner
        self.assertEqual(self.folders._woow_parent_value((other.id, 'x')), other)
        self.assertFalse(self.folders._woow_parent_value(None))
        self.assertEqual(self.folders._woow_owner_value([]), env['res.users'])
        for value in ([other.id], '7', 7.0, True, {'id': 7}, 0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.folders._woow_parent_value(value)
                with self.assertRaises(ValidationError):
                    self.folders._woow_owner_value(value)


@tagged('woow_docmgmt_core')
class TestOneAnswerIsKeptOnlyForTheFoldersThatShareIt(_Fixture, TransactionCase):
    """The key of an answer holds the check, the owner, the parent and the access: sets that mix them."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()
        cls.fifth = new_test_user(cls.env, login='stored_fifth_owner', groups=_USER)
        cls.access.sudo().create({'folder_id': cls.parents['p5'].id, 'subject_type': 'user',
                                  'user_id': cls.fifth.id, 'level': 'manage'})
        cls.env.flush_all()

    def _both_orders(self, first, second, call, message):
        """``call`` on the set in both orders is refused: the first answer is the one that is reused."""
        state = [(folder.owner_id, folder.parent_id) for folder in (first, second)]
        for label, records in (('first, second', first | second), ('second, first', second | first)):
            with self.subTest(label):
                self._refused(lambda: call(records.with_user(self.manager)), message)
                self.assertEqual([(folder.owner_id, folder.parent_id) for folder in (first, second)],
                                 state)

    def test_two_restricted_folders_under_different_parents_for_an_owner_of_the_first_only(self):
        first = self._folder('Stored mix a1', owner=self.admin, parent='p1')
        second = self._folder('Stored mix a2', owner=self.admin, parent='p2')
        self._both_orders(first, second, lambda records: records.write(
            {'owner_id': self.sibling.id}), _CEILING)

    def test_an_inheriting_and_a_restricted_folder_under_one_parent_for_an_owner_without_the_right(self):
        inheriting = self._folder('Stored mix b1', owner=self.admin, restricted=False)
        restricted = self._folder('Stored mix b2', owner=self.admin)
        self._both_orders(inheriting, restricted, lambda records: records.write(
            {'owner_id': self.outsider.id}), _CEILING)

    def test_two_restricted_folders_with_different_owners_moved_under_a_parent_of_one_of_them(self):
        first = self._folder('Stored mix c1', owner=self.fifth)
        second = self._folder('Stored mix c2', owner=self.outsider)
        self._both_orders(first, second, lambda records: records.write(
            {'parent_id': self.parents['p5'].id}), _CEILING)

    def test_an_owner_that_stays_and_one_that_is_named_for_a_user_who_is_no_document_user(self):
        """``plain`` owns p4 and one folder already; the other folder is given to it by the same write."""
        stays = self._folder('Stored mix d1', owner=self.plain)
        named = self._folder('Stored mix d2', owner=self.admin)
        self._both_orders(stays, named, lambda records: records.write(
            {'owner_id': self.plain.id, 'parent_id': self.parents['p4'].id}), _NOT_A_USER)

    def test_the_answer_is_reused_for_folders_that_share_all_of_it(self):
        """Two folders that share the owner, the parent and the access cost one question."""
        first = self._folder('Stored same 1', owner=self.admin)
        second = self._folder('Stored same 2', owner=self.admin)
        model = type(self.folders)
        original = model.search_count
        calls = []

        def counting(records, *args, **kwargs):
            calls.append(records._name)
            return original(records, *args, **kwargs)

        with patch.object(model, 'search_count', counting):
            (first | second).with_user(self.manager).write({'owner_id': self.sibling.id})
        self.assertEqual(calls.count('woow.document.folder'), 1)
        self.assertEqual((first.owner_id, second.owner_id), (self.sibling, self.sibling))


@tagged('woow_docmgmt_core')
class TestAFolderWithoutAParentOrWithoutAnOwner(_Fixture, TransactionCase):
    """The two clauses that need no check: no parent to hold the owner to, and no owner to hold."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def test_the_owner_of_a_restricted_root_folder_is_any_document_user(self):
        """A write that leaves the folder at the top level needs no root right, and holds the owner to no parent."""
        self._root_right(False)
        root = self._folder('Stored root restricted', owner=self.admin, parent=None)
        self.assertFalse(root.parent_id)
        root.with_user(self.manager).write({'owner_id': self.outsider.id})
        self.assertEqual(root.owner_id, self.outsider)
        # the document user rule is still asked
        self._refused(lambda: root.with_user(self.manager).write({'owner_id': self.plain.id}),
                      _NOT_A_USER)

    def test_a_restricted_root_folder_is_made_by_a_manager_when_the_site_allows_it(self):
        self._root_right()
        folders = self.folders.with_user(self.manager)
        created = folders.create({'name': 'Stored root made', 'inherit_access': False,
                                  'owner_id': self.outsider.id})
        self.assertEqual((created.parent_id, created.owner_id, created.inherit_access),
                         (self.folders, self.outsider, False))
        self._root_right(False)
        self._refused(lambda: folders.create({'name': 'Stored root refused',
                                              'inherit_access': False}), _ROOT, AccessError)

    def test_a_restricted_folder_is_moved_to_the_top_level_with_its_owner(self):
        self._root_right()
        folder = self._folder('Stored to the top', owner=self.outsider)
        folder.with_user(self.manager).write({'parent_id': False})
        self.assertEqual((folder.parent_id, folder.owner_id), (self.folders, self.outsider))

    def test_a_folder_without_an_owner_is_moved_and_restricted_without_a_question(self):
        ownerless = self._folder('Stored ownerless moved', owner=None)
        self.assertFalse(ownerless.owner_id)
        ownerless.with_user(self.manager).write({'parent_id': self.parents['p2'].id})
        self.assertEqual(ownerless.parent_id, self.parents['p2'])
        inheriting = self._folder('Stored ownerless opened', owner=None, restricted=False)
        inheriting.with_user(self.manager).write({'inherit_access': False})
        self.assertFalse(inheriting.inherit_access)
        self.assertFalse(inheriting.owner_id)
        both = self._folder('Stored ownerless both', owner=None, restricted=False)
        both.with_user(self.manager).write({'owner_id': False, 'inherit_access': False,
                                            'name': 'Stored ownerless both kept'})
        self.assertEqual((both.name, both.inherit_access), ('Stored ownerless both kept', False))


@tagged('woow_docmgmt_core')
class TestTheStoredStateIsTheAuthority(_Fixture, TransactionCase):
    """The rules are asked again of what the ORM stored: the checks before the call are not the only ones."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    # -- what the ORM stored differs from what was judged ---------------------------------------
    def test_an_owner_that_the_orm_stores_instead_of_the_checked_one_is_refused_for_a_create(self):
        folders = self.folders.with_user(self.manager)
        before = self._count()
        with self._storing(owner_id=self.outsider.id):
            self._refused(lambda: folders.create({
                'name': 'Stored other owner', 'parent_id': self.parents['p1'].id,
                'inherit_access': False}), _CEILING)
            self._refused(lambda: folders.create({
                'name': 'Stored other owner named', 'parent_id': self.parents['p1'].id,
                'owner_id': self.sibling.id, 'inherit_access': False}), _CEILING)
        with self._storing(owner_id=self.plain.id):
            self._refused(lambda: folders.create({
                'name': 'Stored other owner not a user', 'parent_id': self.parents['p1'].id}),
                _NOT_A_USER)
        self.assertEqual(self._count(), before)
        # the control: an owner that is within the rules is stored as it is
        with self._storing(owner_id=self.sibling.id):
            created = folders.create({'name': 'Stored other owner fine',
                                      'parent_id': self.parents['p1'].id, 'inherit_access': False})
        self.assertEqual(created.owner_id, self.sibling)

    def _open_parent(self):
        """A parent that belongs to no company, which a folder with or without one can be made in."""
        parent = self.folders.sudo().create({'name': 'Stored open parent', 'company_id': False,
                                             'owner_id': self.admin.id})
        self.access.sudo().create({'folder_id': parent.id, 'subject_type': 'user',
                                   'user_id': self.manager.id, 'level': 'manage'})
        self.env.flush_all()
        return parent

    def test_a_parent_and_a_company_that_the_orm_stores_instead_of_the_checked_ones_are_refused(self):
        folders = self.folders.with_user(self.manager)
        parent = self._open_parent()
        before = self._count()
        with self._storing(parent_id=False):
            self._refused(lambda: folders.create({
                'name': 'Stored other parent', 'parent_id': self.parents['p1'].id}), _ROOT,
                AccessError)
        with self._storing(company_id=False):
            self._refused(lambda: folders.create({
                'name': 'Stored other company', 'parent_id': parent.id}), _ADMINS, AccessError)
        self.assertEqual(self._count(), before)
        # a document administrator may make a folder that has no company
        with self._storing(company_id=False):
            created = self.folders.with_user(self.doc_admin).create({
                'name': 'Stored administrator folder', 'parent_id': parent.id})
        self.assertFalse(created.company_id)

    def test_the_folder_of_an_editor_is_the_one_of_its_parent_whatever_the_orm_stores(self):
        folders = self.folders.with_user(self.editor)
        before = self._count()
        for label, stored in (('restricted', {'inherit_access': False}),
                              ('owned by the editor', {'owner_id': self.editor.id}),
                              ('owned by nobody', {'owner_id': False})):
            with self.subTest(label), self._storing(**stored):
                self._refused(lambda: folders.create({
                    'name': 'Stored editor %s' % label, 'parent_id': self.parents['p1'].id}),
                    _RIGHTS, AccessError)
        self.assertEqual(self._count(), before)
        child = folders.create({'name': 'Stored editor fine', 'parent_id': self.parents['p1'].id})
        self.assertEqual((child.owner_id, child.inherit_access), (self.parent_owner, True))

    def test_what_a_write_stores_instead_of_what_was_checked_is_refused(self):
        restricted = self._folder('Stored write restricted', owner=self.sibling)
        inheriting = self._folder('Stored write inheriting', owner=self.admin, restricted=False)
        open_parent = self._open_parent()
        company_free = self.folders.sudo().create({
            'name': 'Stored write company', 'parent_id': open_parent.id, 'owner_id': self.admin.id})
        self.env.flush_all()
        with self._writing(owner_id=self.outsider.id):
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'owner_id': self.sibling.id, 'name': 'Stored write kept'}), _CEILING)
        with self._writing(owner_id=self.plain.id):
            self._refused(lambda: inheriting.with_user(self.manager).write(
                {'owner_id': self.outsider.id}), _NOT_A_USER)
        with self._writing(parent_id=self.parents['p5'].id):
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'parent_id': self.parents['p1'].id, 'name': 'Stored write moved'}), _CEILING)
        with self._writing(parent_id=False):
            self._refused(lambda: inheriting.with_user(self.manager).write(
                {'parent_id': self.parents['p2'].id}), _ROOT, AccessError)
        with self._writing(company_id=False):
            self._refused(lambda: company_free.with_user(self.manager).write(
                {'company_id': company_free.company_id.id}), _ADMINS, AccessError)
        self.assertEqual((restricted.owner_id, restricted.parent_id, inheriting.owner_id,
                          inheriting.parent_id, company_free.company_id.id),
                         (self.sibling, self.parents['p1'], self.admin, self.parents['p1'],
                          self.env.company.id))

    def test_a_folder_that_stops_inheriting_with_an_owner_that_the_orm_keeps_is_refused(self):
        """``inherit_access`` stored as False for an owner that the parent does not give the right."""
        folder = self._folder('Stored write stops', owner=self.outsider, restricted=False)
        with self._writing(inherit_access=False):
            self._refused(lambda: folder.with_user(self.manager).write(
                {'owner_id': self.outsider.id, 'name': 'Stored write stops kept'}), _CEILING)
        self.assertTrue(folder.inherit_access)

    # -- the checks before the call switched off: the refusal stays ------------------------------
    def test_the_checks_before_a_write_are_not_the_only_ones(self):
        """Without ``_woow_check_owners`` a write is refused all the same, by the state it leaves."""
        restricted = self._folder('Stored late restricted', owner=self.sibling)
        inheriting = self._folder('Stored late inheriting', owner=self.outsider, restricted=False)
        model = type(self.folders)
        with patch.object(model, '_woow_check_owners', autospec=True) as early:
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'owner_id': self.outsider.id}), _CEILING)
            self._refused(lambda: inheriting.with_user(self.manager).write(
                {'inherit_access': False}), _CEILING)
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'owner_id': self.plain.id}), _NOT_A_USER)
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'parent_id': self.parents['p2'].id, 'owner_id': self.outsider.id}), _CEILING)
            self.assertTrue(early.called)
        self.assertEqual((restricted.owner_id, inheriting.inherit_access),
                         (self.sibling, True))
        # and what is stored is what the final state is made of: with both switched off nothing refuses
        with patch.object(model, '_woow_check_owners', autospec=True), \
                patch.object(model, '_woow_check_stored_state', autospec=True):
            restricted.with_user(self.manager).write({'owner_id': self.outsider.id})
        self.assertEqual(restricted.owner_id, self.outsider)

    def test_the_final_state_is_not_the_only_check_either(self):
        """Without ``_woow_check_stored_state`` the checks before the call refuse the same things."""
        restricted = self._folder('Stored early restricted', owner=self.sibling)
        model = type(self.folders)
        folders = self.folders.with_user(self.manager)
        with patch.object(model, '_woow_check_stored_state', autospec=True) as late:
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'owner_id': self.outsider.id}), _CEILING)
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'owner_id': self.plain.id}), _NOT_A_USER)
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'parent_id': [self.parents['p2'].id]}), _FOLDER)
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'parent_id': False}), _ROOT, AccessError)
            self._refused(lambda: restricted.with_user(self.manager).write(
                {'company_id': [self.env.company.id]}), _ADMINS, AccessError)
            self._refused(lambda: folders.create({
                'name': 'Stored early create', 'parent_id': self.parents['p1'].id,
                'owner_id': self.outsider.id, 'inherit_access': False}), _CEILING)
            self._refused(lambda: folders.create({
                'name': 'Stored early create typed', 'parent_id': str(self.parents['p1'].id)}),
                _FOLDER)
            self._refused(lambda: folders.create({
                'name': 'Stored early create company', 'parent_id': self.parents['p1'].id,
                'company_id': [self.env.company.id]}), _ADMINS, AccessError)
            self._refused(lambda: self.folders.with_user(self.editor).create({
                'name': 'Stored early create editor', 'parent_id': self.parents['p1'].id,
                'inherit_access': False}), _RIGHTS, AccessError)
            self.assertFalse(late.called)

    def test_a_company_that_is_a_list_is_not_the_company_of_the_folder(self):
        """``[company]`` was read as the company, and the ORM stored NULL: the folder left its company."""
        folder = self._folder('Stored company list', restricted=False)
        company = folder.company_id
        self.assertTrue(company)
        for label, value in (('a list', [company.id]), ('a list pair', [company.id, 'name']),
                             ('a text', str(company.id)), ('a flag', True)):
            with self.subTest(label):
                self._refused(lambda: folder.with_user(self.manager).write({'company_id': value}),
                              _ADMINS, AccessError)
                self._refused(lambda: self.folders.with_user(self.manager).create({
                    'name': 'Stored company list create', 'parent_id': self.parents['p1'].id,
                    'company_id': value}), _ADMINS, AccessError)
                self.assertEqual(folder.company_id, company)
        # the number, the record and the pair are the company that the folder has: no change
        for value in (company.id, company, (company.id, 'name')):
            folder.with_user(self.manager).write({'company_id': value, 'color': 3})
            created = self.folders.with_user(self.manager).create({
                'name': 'Stored company shape %s' % type(value).__name__,
                'parent_id': self.parents['p1'].id, 'company_id': value})
            self.assertEqual(created.company_id, company)
        self.assertEqual((folder.company_id, folder.color), (company, 3))

    def test_a_caller_who_loses_the_access_by_the_write_is_not_refused_for_reading_the_state(self):
        """The owner of a restricted folder, with no other grant, hands it to a document administrator.

        After the write the caller reads nothing of the folder any more; the state that is judged
        is the one that the records hold in memory, so nothing is fetched under the rules of a
        caller who may not read it.
        """
        folder = self._folder('Stored handed over', owner=self.outsider, members=[(self.sibling, 'read')])
        self.assertTrue(folder.with_user(self.outsider)._woow_has_rank(4))
        folder.with_user(self.outsider).write({'owner_id': self.doc_admin.id})
        self.assertEqual(folder.owner_id, self.doc_admin)
        self.env.flush_all()
        self.assertFalse(self.folders.with_user(self.outsider).search_count([('id', '=', folder.id)]))
        row = self.env['woow.document.log'].sudo().search(
            [('event', '=', 'access_change'), ('folder_id', '=', folder.id)], order='id desc', limit=1)
        self.assertEqual(row.detail, {'owner_id': self.doc_admin.id,
                                      'previous_owner_id': self.outsider.id})

    def test_the_final_state_costs_nothing_when_the_answers_are_the_same(self):
        """The usual case: what was asked before the call is what the records hold after it."""
        folder = self._folder('Stored cost', owner=self.admin)
        created = []
        model = type(self.folders)
        original = model.search_count

        def counting(records, *args, **kwargs):
            created.append(records._name)
            return original(records, *args, **kwargs)

        with patch.object(model, 'search_count', counting):
            folder.with_user(self.manager).write({'owner_id': self.sibling.id})
        self.assertEqual(created.count('woow.document.folder'), 1)
        del created[:]
        with patch.object(model, 'search_count', counting):
            self.folders.with_user(self.manager).create({
                'name': 'Stored cost create', 'parent_id': self.parents['p1'].id,
                'owner_id': self.sibling.id, 'inherit_access': False})
        self.assertEqual(created.count('woow.document.folder'), 1)
        # the rank of the parent is asked once for a batch under it, not once for each folder
        ranks = []
        has_rank = model._woow_has_rank

        def counting_rank(records, n):
            ranks.append(n)
            return has_rank(records, n)

        with patch.object(model, '_woow_has_rank', counting_rank):
            self.folders.with_user(self.manager).create([
                {'name': 'Stored cost batch %d' % number, 'parent_id': self.parents['p1'].id}
                for number in range(4)])
        self.assertEqual(sorted(ranks), [3, 4])


@tagged('woow_docmgmt_core')
class TestNoShapeOfTheValuesLeavesAFolderOutsideTheRules(_Fixture, TransactionCase):
    """Property-style: many shapes of the four values, and defaults of every kind, in a few calls each.

    The caller is a manager of the parents who is not a document administrator, on a site where no
    root folder may be made. Every call may be refused or accepted; after each of them every folder
    that the walk has made or touched must hold: it has a parent and the company that it had, and a
    folder that does not inherit its access has an owner who is a document user and has the manage
    right on its parent (a document administrator needs no grant). The editor makes folders under p1:
    each one inherits and has the owner of p1.
    """

    WALKS = 30
    STEPS = 4

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def setUp(self):
        super().setUp()
        self._root_right(False)
        self.company = self.env.company
        parents = self.parents
        users = self.outsider | self.sibling | self.third | self.plain | self.manager
        self.owners = [False, None, 0, -1, True, '5', 5.5, [], [self.sibling.id], (self.sibling.id,),
                       (self.sibling.id, 'name'), {'id': self.sibling.id}, self.sibling.id,
                       self.sibling, self.outsider.id, self.outsider, self.plain.id, self.third.id,
                       self.manager.id, self.admin.id, 10 ** 9, self.env['res.users'], users]
        self.parent_shapes = [False, None, 0, -1, True, '1', 1.0, [], [parents['p1'].id],
                              (parents['p1'].id,), (parents['p2'].id, 'name'), {'id': parents['p1'].id},
                              parents['p1'].id, parents['p2'].id, parents['p3'].id,
                              parents['p4'].id, parents['p5'].id, parents['p2'], 10 ** 9,
                              self.folders.browse(), parents['p1'] | parents['p2']]
        self.flags = [True, False, 0, 1, None, '', 'no', [0], []]
        self.companies = [False, None, 0, self.company.id, self.company, [self.company.id],
                          (self.company.id,), 'x', True, 10 ** 9]
        self.pool = self.folders.browse()
        self.companies_of = {}

    def _track(self, folders):
        self.pool |= folders
        for folder in folders:
            self.companies_of[folder.id] = folder.company_id.id

    def _violations(self):
        """What is wrong with the folders of the pool, read as the superuser."""
        bad = []
        for folder in self.pool.sudo().with_context(active_test=False).exists():
            if not folder.parent_id:
                bad.append((folder.name, 'a root folder'))
            if folder.company_id.id != self.companies_of[folder.id]:
                bad.append((folder.name, 'another company'))
            owner, parent = folder.owner_id, folder.parent_id
            if not folder.inherit_access and parent and owner:
                if not (owner._is_internal() and owner.has_group(_USER)):
                    bad.append((folder.name, 'an owner who is no document user'))
                elif not (owner.has_group(_MANAGER) or owner in parent.eff_manage_user_ids
                          or owner.groups_id & parent.eff_manage_group_ids):
                    bad.append((folder.name, 'an owner without the right on the parent'))
        return bad

    def _values(self, rng, creating):
        shapes = {'owner_id': self.owners, 'parent_id': self.parent_shapes,
                  'inherit_access': self.flags, 'company_id': self.companies}
        keys = rng.sample(sorted(shapes), rng.randint(1, len(shapes)))
        values = {key: rng.choice(shapes[key]) for key in keys}
        if creating:
            values['name'] = 'Stored walk %d' % rng.randrange(10 ** 9)
        return values

    def _context(self, rng):
        """Sometimes a default of the context, and sometimes a default that the caller stored."""
        context = {}
        if rng.random() < 0.4:
            context['default_owner_id'] = rng.choice(self.owners[:12])
        if rng.random() < 0.3:
            context['default_parent_id'] = rng.choice(self.parent_shapes[:14])
        return context

    def _walk(self, rng):
        self._store_default(self.manager, rng.choice([self.outsider, self.sibling, self.plain,
                                                      self.manager]))
        for _step in range(self.STEPS):
            caller = self.folders.with_user(self.manager).with_context(**self._context(rng))
            try:
                with self.cr.savepoint():
                    if not self.pool or rng.random() < 0.35:
                        created = caller.create(self._values(rng, creating=True))
                        self.env.flush_all()
                        self._track(created.sudo())
                    else:
                        targets = self.pool.browse([folder.id for folder in rng.sample(
                            list(self.pool), rng.randint(1, min(3, len(self.pool))))])
                        targets.with_user(self.manager).with_context(active_test=False).write(
                            self._values(rng, creating=False))
                        self.env.flush_all()
            except (UserError, DatabaseError, ValueError, TypeError, KeyError):
                self.env.invalidate_all()      # refused: the savepoint took the call back
            self.assertFalse(self._violations())

    def test_no_shape_of_the_values_leaves_a_folder_outside_the_rules(self):
        for number in range(3):
            self._track(self._folder('Stored pool restricted %d' % number,
                                     owner=self.manager if number else self.sibling))
        for number in range(2):
            self._track(self._folder('Stored pool inheriting %d' % number, restricted=False,
                                     owner=self.outsider if number else self.admin))
        rng = random.Random('stored state of a folder')
        for walk in range(self.WALKS):
            with self.subTest(walk=walk):
                self._walk(rng)

    def test_the_folders_of_an_editor_inherit_and_have_the_owner_of_the_parent(self):
        rng = random.Random('stored state of an editor')
        folders = self.folders.with_user(self.editor)
        made = 0
        for walk in range(self.WALKS * 2):
            values = self._values(rng, creating=True)
            context = self._context(rng)
            with self.subTest(walk=walk):
                try:
                    with self.cr.savepoint():
                        created = folders.with_context(**context).create(values)
                        self.env.flush_all()
                except (UserError, DatabaseError, ValueError, TypeError, KeyError):
                    self.env.invalidate_all()
                else:
                    made += 1
                    folder = created.sudo()
                    self.assertEqual((folder.parent_id, folder.owner_id, folder.inherit_access),
                                     (self.parents['p1'], self.parent_owner, True))
        # the walks do reach a folder that is made (a name and the parent of the editor)
        for _ in range(3):
            created = folders.create({'name': 'Stored editor control %d' % made,
                                      'parent_id': self.parents['p1'].id})
            made += 1
            self.assertEqual((created.owner_id, created.inherit_access), (self.parent_owner, True))
