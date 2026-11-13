# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""A flag of a folder is judged by what the ORM keeps, not by the type of the value that came in.

A Boolean column holds ``bool(value)``: ``0``, ``0.0``, ``None``, ``''`` and ``[]`` are False as
``False`` is, and any other number or text is True. The guards of ``create`` and ``write`` that read
``inherit_access`` tested ``value is False``, which is true for the object ``False`` and for no other
value. So ``write({'inherit_access': 0})`` made a folder restricted without the check of its member rows
against its parent and without keeping the grants that it inherited as member rows, and an editor, who
may not restrict a folder, could create a restricted one. Every value that the column keeps as False has
to behave as ``False`` does, and every value that it keeps as True as ``True`` does.

The invariant behind it (AC-2): a folder that does not inherit its access has no member who gets more on
it than the parent gives the same member.
"""
from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..models.document_folder import _stored_flag

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_CEILING = 'A member cannot be granted more access than on the parent folder.'
_RIGHTS = 'You do not have enough rights on this folder.'
_MEMBERS_FIRST = 'Save member changes before disabling inherited access.'
_PARENT_FIRST = 'Save the new parent before disabling inherited access.'
_FIELD = {'user': 'user_id', 'group': 'group_id', 'partner': 'partner_id'}
_RANK = {'read': 1, 'upload': 2, 'edit': 3, 'manage': 4}
_EFFECTIVE = (
    'eff_read_user_ids', 'eff_upload_user_ids', 'eff_edit_user_ids', 'eff_manage_user_ids',
    'eff_read_group_ids', 'eff_upload_group_ids', 'eff_edit_group_ids', 'eff_manage_group_ids',
    'eff_read_partner_ids', 'eff_upload_partner_ids')

# The values that the column keeps as False (the object False is the control) ...
_FALSE_LIKE = (('False', False), ('0', 0), ('None', None), ('an empty text', ''),
               ('an empty list', []), ('0.0', 0.0))
# ... and the values that it keeps as True (the text 'false' is one of them).
_TRUE_LIKE = (('True', True), ('1', 1), ('2.5', 2.5), ('a text', 'yes'),
              ('the text false', 'false'), ('a list', [0]))


@tagged('woow_docmgmt_core')
class TestBooleanFlagsAreJudgedByWhatTheOrmKeeps(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.admin = env.ref('base.user_admin')
        cls.folders = env['woow.document.folder']
        cls.access = env['woow.document.folder.access']

        def user(name, groups=_USER):
            return new_test_user(env, login='flag_%s' % name, groups=groups)

        cls.manager = user('manager')              # manage on the parent: the caller of the writes
        cls.sibling = user('sibling')              # manage on the parent
        cls.editor = user('editor')                # edit on the parent: organizes, does not restrict
        cls.reader = user('reader')                # read on the parent
        cls.stranger = user('stranger')            # a document user with no grant on the parent
        cls.parent_owner = user('parent_owner')    # manage on the parent as its owner: no row
        cls.doc_admin = user('doc_admin', _MANAGER)
        cls.portal = user('portal', 'base.group_portal')
        cls.group = env['res.groups'].sudo().create({'name': 'Flags readers'})
        cls.parent = cls.folders.sudo().create({
            'name': 'Flags parent', 'owner_id': cls.parent_owner.id})
        cls.other_parent = cls.folders.sudo().create({
            'name': 'Flags other parent', 'owner_id': cls.admin.id})
        for folder, subject, member, level in (
                (cls.parent, 'user', cls.manager, 'manage'),
                (cls.parent, 'user', cls.sibling, 'manage'),
                (cls.parent, 'user', cls.editor, 'edit'),
                (cls.parent, 'user', cls.reader, 'read'),
                (cls.parent, 'group', cls.group, 'read'),
                (cls.parent, 'partner', cls.portal.partner_id, 'upload'),
                (cls.other_parent, 'user', cls.manager, 'manage')):
            cls.access.sudo().create({
                'folder_id': folder.id, 'subject_type': subject, _FIELD[subject]: member.id,
                'level': level})
        env.flush_all()

    # -- helpers ---------------------------------------------------------------------------------
    def _folder(self, name, *, owner=None, restricted=False, members=()):
        """A folder under the parent, made by the superuser, so that no guard is in the way."""
        folder = self.folders.sudo().create({
            'name': name, 'parent_id': self.parent.id, 'owner_id': (owner or self.admin).id,
            'inherit_access': not restricted})
        for member, level in members:
            self.access.sudo().create({'folder_id': folder.id, 'subject_type': 'user',
                                       'user_id': member.id, 'level': level})
        self.env.flush_all()
        return folder

    def _rows(self, folder):
        """The member rows of ``folder`` as (kind of member, id of the member, level)."""
        return sorted((line.subject_type, line[_FIELD[line.subject_type]].id, line.level)
                      for line in folder.sudo().access_ids)

    def _effective(self, folder):
        return {name: frozenset(folder.sudo()[name].ids) for name in _EFFECTIVE}

    def _above_the_parent(self, folder):
        """The member rows of ``folder`` that give a member more than its parent gives the member."""
        folder = folder.sudo()
        parent, above = folder.parent_id, self.access.browse()
        for line in folder.access_ids:
            kind = line.subject_type
            member = line[_FIELD[kind]]
            given = max((rank for level, rank in _RANK.items()
                         if 'eff_%s_%s_ids' % (level, kind) in parent._fields
                         and member in parent['eff_%s_%s_ids' % (level, kind)]), default=0)
            if _RANK[line.level] > given:
                above |= line
        return above

    def _refused(self, call, error=ValidationError, message=_CEILING):
        """``call`` is refused with ``error`` that says ``message``, and changes nothing."""
        with self.assertRaises(error) as caught, self.cr.savepoint():
            call()
        self.assertIs(type(caught.exception), error)
        self.assertEqual(caught.exception.args, (message,))

    # -- write: the member rows of a folder that stops inheriting -------------------------------
    def test_a_member_above_the_parent_holds_back_any_value_that_means_false(self):
        """The reproduced case: the folder inherits, and a member has more on it than on the parent.

        ``False`` is refused by the check of the member rows; ``0`` was not, and the folder became
        restricted with a member above its parent.
        """
        caller = self.manager
        for label, value in _FALSE_LIKE:
            with self.subTest(label):
                folder = self._folder('Flags member above %s' % label,
                                      members=[(self.stranger, 'manage')])
                self.assertEqual(self._above_the_parent(folder), folder.access_ids)
                self.assertTrue(folder.with_user(caller)._woow_has_rank(4))
                self._refused(lambda: folder.with_user(caller).write({'inherit_access': value}))
                self.assertTrue(folder.inherit_access)
                self.assertEqual(self._rows(folder), [('user', self.stranger.id, 'manage')])
                # a folder that does not inherit its access has no member above its parent
                self.assertTrue(folder.inherit_access or not self._above_the_parent(folder))
        for label, value in _TRUE_LIKE:
            with self.subTest(label):
                folder = self._folder('Flags member kept %s' % label,
                                      members=[(self.stranger, 'manage')])
                folder.with_user(caller).write({'inherit_access': value})
                self.assertTrue(folder.inherit_access)
                self.assertEqual(self._rows(folder), [('user', self.stranger.id, 'manage')])

    def test_the_effective_rights_become_member_rows_for_any_value_that_means_false(self):
        """Nobody loses a right when a folder stops inheriting: the grants of the parent are kept.

        The caller does not matter for this: a manager of the folder, a document administrator and
        the superuser all keep the grants that the folder inherited as member rows.
        """
        kept = sorted([
            ('user', self.manager.id, 'manage'), ('user', self.sibling.id, 'manage'),
            ('user', self.editor.id, 'edit'), ('user', self.reader.id, 'read'),
            ('user', self.parent_owner.id, 'manage'), ('group', self.group.id, 'read'),
            ('partner', self.portal.partner_id.id, 'upload')])
        callers = (('a manager of the folder', lambda folder: folder.with_user(self.manager)),
                   ('a document administrator', lambda folder: folder.with_user(self.doc_admin)),
                   ('the superuser', lambda folder: folder.sudo()))
        for who, act_as in callers:
            for label, value in _FALSE_LIKE:
                with self.subTest(who, value=label):
                    folder = self._folder('Flags converted %s by %s' % (label, who))
                    before = self._effective(folder)
                    self.assertFalse(folder.access_ids)
                    act_as(folder).write({'inherit_access': value})
                    self.assertFalse(folder.inherit_access)
                    self.assertEqual(self._rows(folder), kept)
                    self.assertEqual(self._effective(folder), before)
                    self.assertFalse(self._above_the_parent(folder))
        for label, value in _TRUE_LIKE:
            with self.subTest(value=label):
                folder = self._folder('Flags inherits on %s' % label)
                before = self._effective(folder)
                folder.with_user(self.manager).write({'inherit_access': value})
                self.assertTrue(folder.inherit_access)
                self.assertFalse(folder.access_ids)
                self.assertEqual(self._effective(folder), before)

    def test_member_changes_with_the_flag_off_are_refused_for_any_value_that_means_false(self):
        caller = self.manager
        for label, value in _FALSE_LIKE:
            with self.subTest(label):
                folder = self._folder('Flags members first %s' % label)
                self._refused(lambda: folder.with_user(caller).write(
                    {'inherit_access': value, 'access_ids': [Command.clear()]}),
                    UserError, _MEMBERS_FIRST)
                self.assertTrue(folder.inherit_access)
        for label, value in _TRUE_LIKE:
            with self.subTest(label):
                folder = self._folder('Flags members then %s' % label)
                folder.with_user(caller).write({
                    'inherit_access': value, 'access_ids': [Command.clear()],
                    'name': 'Flags members done %s' % label})
                self.assertEqual((folder.name, folder.inherit_access),
                                 ('Flags members done %s' % label, True))

    def test_a_new_parent_with_the_flag_off_is_refused_for_any_value_that_means_false(self):
        caller = self.manager
        for label, value in _FALSE_LIKE:
            with self.subTest(label):
                folder = self._folder('Flags parent first %s' % label)
                self._refused(lambda: folder.with_user(caller).write(
                    {'inherit_access': value, 'parent_id': self.other_parent.id}),
                    UserError, _PARENT_FIRST)
                self.assertEqual((folder.parent_id, folder.inherit_access), (self.parent, True))
        for label, value in _TRUE_LIKE:
            with self.subTest(label):
                folder = self._folder('Flags parent then %s' % label)
                folder.with_user(caller).write(
                    {'inherit_access': value, 'parent_id': self.other_parent.id})
                self.assertEqual((folder.parent_id, folder.inherit_access),
                                 (self.other_parent, True))

    # -- write: the owner of a folder that stops inheriting -------------------------------------
    def test_the_owner_of_a_folder_is_held_to_the_ceiling_by_the_flag_as_stored(self):
        """The owner has the manage level: only a folder that stays restricted needs a grant above it."""
        caller = self.manager
        for label, value in _FALSE_LIKE:
            with self.subTest(label):
                # the owner that the folder has, and the folder stops inheriting
                held = self._folder('Flags owner kept %s' % label, owner=self.stranger)
                self._refused(lambda: held.with_user(caller).write({'inherit_access': value}))
                # a new owner and the restriction in one write
                handed = self._folder('Flags owner named %s' % label)
                self._refused(lambda: handed.with_user(caller).write(
                    {'owner_id': self.stranger.id, 'inherit_access': value}))
                self.assertEqual((held.inherit_access, handed.inherit_access), (True, True))
                self.assertEqual((held.owner_id, handed.owner_id), (self.stranger, self.admin))
        for label, value in _TRUE_LIKE:
            with self.subTest(label):
                # a folder that inherits has no ceiling, for the owner it names or for the one it has
                inheriting = self._folder('Flags owner open %s' % label)
                inheriting.with_user(caller).write(
                    {'owner_id': self.stranger.id, 'inherit_access': value})
                self.assertEqual((inheriting.owner_id, inheriting.inherit_access),
                                 (self.stranger, True))
                # a restricted folder that starts to inherit takes any owner as well
                opened = self._folder('Flags owner opens %s' % label, restricted=True,
                                      members=[(caller, 'manage')])
                opened.with_user(caller).write(
                    {'owner_id': self.stranger.id, 'inherit_access': value})
                self.assertEqual((opened.owner_id, opened.inherit_access), (self.stranger, True))

    # -- the editor: create and write -----------------------------------------------------------
    def test_an_editor_cannot_restrict_a_folder_with_any_value_that_means_false(self):
        """An editor organizes the folders of the parent, and may not restrict one.

        A new folder of an editor takes the owner of its parent and inherits its access, and the
        right to change the flag follows the key that is named, whatever the value is.
        """
        folders = self.folders.with_user(self.editor)
        for label, value in _FALSE_LIKE:
            with self.subTest('create', value=label):
                self._refused(lambda: folders.create({
                    'name': 'Flags editor refused %s' % label, 'parent_id': self.parent.id,
                    'inherit_access': value}), AccessError, _RIGHTS)
            with self.subTest('write', value=label):
                folder = self._folder('Flags editor write %s' % label)
                self.assertTrue(folder.with_user(self.editor)._woow_has_rank(3))
                self._refused(lambda: folder.with_user(self.editor).write({'inherit_access': value}),
                              AccessError, _RIGHTS)
                self.assertTrue(folder.inherit_access)
        self.assertFalse(self.folders.sudo().search([('name', 'like', 'Flags editor refused')]))
        for label, value in _TRUE_LIKE:
            with self.subTest('create', value=label):
                child = folders.create({
                    'name': 'Flags editor child %s' % label, 'parent_id': self.parent.id,
                    'inherit_access': value})
                self.assertEqual((child.owner_id, child.inherit_access), (self.parent_owner, True))
            with self.subTest('write', value=label):
                folder = self._folder('Flags editor write kept %s' % label)
                self._refused(lambda: folder.with_user(self.editor).write({'inherit_access': value}),
                              AccessError, _RIGHTS)

    # -- create: a manager of the parent --------------------------------------------------------
    def test_a_folder_that_is_created_restricted_is_held_to_the_ceiling_for_any_false_value(self):
        """The owner and the members of a restricted folder need the manage right on the parent."""
        caller = self.manager
        folders = self.folders.with_user(caller)
        before = self.folders.sudo().search_count([])
        for label, value in _FALSE_LIKE:
            with self.subTest('owner', value=label):
                self._refused(lambda: folders.create({
                    'name': 'Flags create refused %s' % label, 'parent_id': self.parent.id,
                    'owner_id': self.stranger.id, 'inherit_access': value}))
                # an owner within the ceiling: the folder is restricted, as the flag says
                created = folders.create({
                    'name': 'Flags create restricted %s' % label, 'parent_id': self.parent.id,
                    'owner_id': self.sibling.id, 'inherit_access': value})
                self.assertEqual((created.owner_id, created.inherit_access), (self.sibling, False))
        self.assertEqual(self.folders.sudo().search_count([]), before + len(_FALSE_LIKE))
        for label, value in _TRUE_LIKE:
            with self.subTest('owner', value=label):
                created = folders.create({
                    'name': 'Flags create inheriting %s' % label, 'parent_id': self.parent.id,
                    'owner_id': self.stranger.id, 'inherit_access': value})
                self.assertEqual((created.owner_id, created.inherit_access), (self.stranger, True))

        # What the create stored is what the check of the member rows reads. A member row cannot be
        # made together with the folder by a caller who is not an administrator (the right to manage
        # the new folder is asked before its grants are stored), so the row is added to the folder
        # that the create made, in a request of its own (the flush).
        def member(folder):
            return {'folder_id': folder.id, 'subject_type': 'user', 'user_id': self.stranger.id,
                    'level': 'manage'}

        for label, value in _FALSE_LIKE:
            with self.subTest('member', value=label):
                created = folders.create({
                    'name': 'Flags create member %s' % label, 'parent_id': self.parent.id,
                    'inherit_access': value})
                self.assertFalse(created.inherit_access)
                self.env.flush_all()
                self._refused(lambda: self.access.with_user(caller).create(member(created)))
                self.assertFalse(self._rows(created))
        for label, value in _TRUE_LIKE:
            with self.subTest('member', value=label):
                created = folders.create({
                    'name': 'Flags create member kept %s' % label, 'parent_id': self.parent.id,
                    'inherit_access': value})
                self.assertTrue(created.inherit_access)
                self.env.flush_all()
                self.access.with_user(caller).create(member(created))
                self.assertEqual(self._rows(created), [('user', self.stranger.id, 'manage')])

    # -- the reading itself ---------------------------------------------------------------------
    def test_the_flag_is_read_as_the_column_keeps_it(self):
        """``_stored_flag`` is ``bool(value)`` for a key that is named, and the default for one that is not."""
        for label, value in _FALSE_LIKE + _TRUE_LIKE:
            with self.subTest(label):
                kept = bool(value)
                self.assertIs(_stored_flag({'inherit_access': value}, 'inherit_access', not kept),
                              kept)
                # the premise: this is what the column holds, when it is made and when it is written
                folder = self.folders.sudo().create({
                    'name': 'Flags kept %s' % label, 'parent_id': self.parent.id,
                    'inherit_access': value})
                self.env.flush_all()
                folder.invalidate_recordset()
                self.assertIs(folder.inherit_access, kept)
                folder.sudo().write({'inherit_access': not kept})
                folder.sudo().write({'inherit_access': value})
                self.env.flush_all()
                folder.invalidate_recordset()
                self.assertIs(folder.inherit_access, kept)
        self.assertIs(_stored_flag({}, 'inherit_access', True), True)
        self.assertIs(_stored_flag({}, 'inherit_access', False), False)
        self.assertIs(_stored_flag({'name': 'x'}, 'inherit_access', True), True)
        self.assertIs(_stored_flag({'name': 'x'}, 'inherit_access', False), False)
