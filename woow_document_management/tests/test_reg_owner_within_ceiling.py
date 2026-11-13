# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The owner of a folder that does not inherit its access stays within what its parent grants.

The owner of a folder has the manage level of it. A folder that does not inherit its access can only
narrow the access of its parent, and member rows are held to that: a member cannot be given more
than the parent gives the member. The owner was not: a write of ``owner_id`` asked the caller for
the manage right on the folder and looked at nothing else, so a manager could hand the folder, its
subtree and its future documents to a user without any grant above it, which the same grant as a
member row is refused for. The owner has to be a document user as well, as a member is.

Who is held to what: the superuser to nothing; a document administrator to the document user rule;
anyone else to both, and to the ceiling only for a folder that does not inherit its access and has a
parent. The owner who stays is held to the ceiling when the folder moves below another parent or
stops inheriting, which closes the way of two steps (an owner while the folder inherits, then the
restriction). Whoever cannot read the parent gets the answer of an owner without a right there.
"""
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

_USER = 'woow_document_management.group_woow_document_user'
_CEILING = 'A member cannot be granted more access than on the parent folder.'
_NOT_A_USER = 'Choose a document user.'


@tagged('woow_docmgmt_core')
class TestOwnerStaysWithinTheCeiling(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.admin = env.ref('base.user_admin')
        cls.folders = env['woow.document.folder']
        cls.access = env['woow.document.folder.access']

        def user(name, groups=_USER):
            return new_test_user(env, login='ceil_%s' % name, groups=groups)

        cls.outsider = user('outsider')            # a document user with no grant on the parent
        cls.sibling = user('sibling')              # manage on the parent
        cls.editor = user('editor')                # edit on the parent
        cls.parent_owner = user('parent_owner')    # the owner of the parent: manage without a row
        cls.plain = user('plain', 'base.group_user')
        cls.portal = user('portal', 'base.group_portal')
        # The callers: each manages a folder under the parent, and has this standing on the parent.
        cls.callers = {'manage': user('manage'), 'read': user('read'), 'none': user('none'),
                       'owner only': user('owner_only')}
        cls.parent = cls.folders.sudo().create({
            'name': 'Ceiling parent', 'owner_id': cls.parent_owner.id})
        cls.other_parent = cls.folders.sudo().create({
            'name': 'Ceiling other parent', 'owner_id': cls.admin.id})
        for folder, who, level in (
                (cls.parent, cls.sibling, 'manage'), (cls.parent, cls.editor, 'edit'),
                (cls.parent, cls.callers['manage'], 'manage'),
                (cls.parent, cls.callers['read'], 'read'),
                (cls.other_parent, cls.callers['manage'], 'manage')):
            cls.access.sudo().create({'folder_id': folder.id, 'subject_type': 'user',
                                      'user_id': who.id, 'level': level})
        env.flush_all()

    # -- helpers ---------------------------------------------------------------------------------
    def _folder(self, name, *, owner=None, restricted=True, parent=None, members=()):
        folder = self.folders.sudo().create({
            'name': name, 'parent_id': (parent or self.parent).id,
            'owner_id': (owner or self.admin).id, 'inherit_access': not restricted})
        for who, level in members:
            self.access.sudo().create({'folder_id': folder.id, 'subject_type': 'user',
                                       'user_id': who.id, 'level': level})
        self.env.flush_all()
        return folder

    def _managed(self, standing, name=None):
        """(a caller with this standing on the parent, the restricted folder that it manages)"""
        caller = self.callers[standing]
        name = name or 'Ceiling managed by %s' % standing
        if standing == 'owner only':
            return caller, self._folder(name, owner=caller)
        return caller, self._folder(name, members=[(caller, 'manage')])

    def _sees_parent(self, caller):
        return bool(self.parent.with_user(caller).search_count([('id', '=', self.parent.id)]))

    def _refused(self, call, message=_CEILING):
        """``call`` is refused as a validation error that says ``message``, and changes nothing."""
        with self.assertRaises(ValidationError) as caught, self.cr.savepoint():
            call()
        self.assertEqual(caught.exception.args, (message,))

    def _events(self, folder=None):
        domain = [('event', '=', 'access_change')]
        if folder:
            domain.append(('folder_id', '=', folder.id))
        return self.env['woow.document.log'].sudo().search(domain, order='id')

    # -- the owner of a restricted folder -------------------------------------------------------
    def test_an_owner_without_manage_on_the_parent_is_refused_in_a_write(self):
        """The callers of every standing: a manager, a reader of the parent, none, and the owner."""
        for standing in self.callers:
            with self.subTest(standing):
                caller, folder = self._managed(standing)
                owner = folder.owner_id
                self.assertEqual(self._sees_parent(caller), standing in ('manage', 'read'))
                self.assertTrue(folder.with_user(caller)._woow_has_rank(4))
                self._refused(
                    lambda: folder.with_user(caller).write({'owner_id': self.outsider.id}))
                self.assertEqual(folder.owner_id, owner)
                self.assertNotIn(self.outsider, folder.eff_manage_user_ids)
                self.assertFalse(folder.with_user(self.outsider)._woow_has_rank(1))

    def test_the_same_grant_through_a_member_row_is_refused_the_same_way(self):
        """The control: the row of the member and the owner are one right, with one answer."""
        row = {'subject_type': 'user', 'user_id': self.outsider.id, 'level': 'manage'}
        for standing in self.callers:
            with self.subTest(standing):
                caller, folder = self._managed(standing)
                if standing in ('manage', 'read'):
                    with self.assertRaises(ValidationError) as by_row, self.cr.savepoint():
                        self.access.with_user(caller).create(dict(row, folder_id=folder.id))
                    with self.assertRaises(ValidationError) as by_owner, self.cr.savepoint():
                        folder.with_user(caller).write({'owner_id': self.outsider.id})
                    self.assertEqual(by_row.exception.args, (_CEILING,))
                    self.assertEqual(by_owner.exception.args, by_row.exception.args)
                else:
                    # A caller who cannot read the parent cannot ask it for its grants: that is an
                    # access error for a row, as it was. The owner gets the refusal of a right that
                    # is not there, and learns nothing of the parent.
                    with self.assertRaises(AccessError), self.cr.savepoint():
                        self.access.with_user(caller).create(dict(row, folder_id=folder.id))
                    self._refused(lambda: folder.with_user(caller).write(
                        {'owner_id': self.outsider.id}))
                self.assertNotIn(self.outsider, folder.eff_manage_user_ids)

    def test_an_owner_without_manage_on_the_parent_is_refused_in_a_create(self):
        caller = self.callers['manage']
        folders = self.folders.with_user(caller)
        before = self.folders.sudo().search_count([])
        for label, extra in (('flag off', {'inherit_access': False}),
                             ('flag 0', {'inherit_access': 0}),
                             ('flag None', {'inherit_access': None})):
            with self.subTest(label):
                self._refused(lambda: folders.create({
                    'name': 'Ceiling refused %s' % label, 'parent_id': self.parent.id,
                    'owner_id': self.outsider.id, **extra}))
        self.assertEqual(self.folders.sudo().search_count([]), before)

    def test_an_owner_with_manage_on_the_parent_is_accepted(self):
        caller = self.callers['manage']
        for label, owner in (('a member', self.sibling), ('the owner of the parent',
                                                          self.parent_owner),
                             ('the caller', caller)):
            with self.subTest(label):
                folder = self._folder('Ceiling accepted %s' % label, members=[(caller, 'manage')])
                folder.with_user(caller).write({'owner_id': owner.id})
                self.assertEqual(folder.owner_id, owner)
                self.assertIn(owner, folder.eff_manage_user_ids)
                created = self.folders.with_user(caller).create({
                    'name': 'Ceiling created %s' % label, 'parent_id': self.parent.id,
                    'owner_id': owner.id, 'inherit_access': False})
                self.assertEqual((created.owner_id, created.inherit_access), (owner, False))
                self.assertIn(owner, created.eff_manage_user_ids)
        # who can read the parent decides nothing else: a reader of it hands a folder over as well
        reader, folder = self._managed('read')
        folder.with_user(reader).write({'owner_id': self.sibling.id})
        self.assertEqual(folder.owner_id, self.sibling)
        # without a name the owner is the caller, who manages the parent
        created = self.folders.with_user(caller).create({
            'name': 'Ceiling default owner', 'parent_id': self.parent.id, 'inherit_access': False})
        self.assertEqual((created.owner_id, created.inherit_access), (caller, False))

    def test_a_manage_right_through_a_group_and_a_document_administrator_count(self):
        """The right that ``_woow_has_rank(4)`` finds for the caller, found for the owner."""
        member = new_test_user(self.env, login='ceil_group_member', groups=_USER)
        managers = self.env['res.groups'].sudo().create({
            'name': 'Ceiling managers', 'users': [Command.link(self.outsider.id)]})
        editors = self.env['res.groups'].sudo().create({
            'name': 'Ceiling editors', 'users': [Command.link(member.id)]})
        for group, level in ((managers, 'manage'), (editors, 'edit')):
            self.access.sudo().create({'folder_id': self.parent.id, 'subject_type': 'group',
                                       'group_id': group.id, 'level': level})
        self.env.flush_all()
        for standing in self.callers:
            with self.subTest(standing):
                caller, folder = self._managed(standing)
                if standing in ('manage', 'read'):
                    folder.with_user(caller).write({'owner_id': self.outsider.id})
                    self.assertEqual(folder.owner_id, self.outsider)
                    self._refused(lambda: folder.with_user(caller).write({'owner_id': member.id}))
                # a document administrator needs no grant, and nobody can read the parent for it
                folder.with_user(caller).write({'owner_id': self.admin.id})
                self.assertEqual(folder.owner_id, self.admin)

    def test_a_folder_that_inherits_its_access_is_not_limited(self):
        for standing, caller in self.callers.items():
            with self.subTest(standing):
                folder = self._folder('Ceiling inherits %s' % standing, restricted=False,
                                      members=[(caller, 'manage')])
                folder.with_user(caller).write({'owner_id': self.outsider.id})
                self.assertEqual(folder.owner_id, self.outsider)
        caller = self.callers['manage']
        created = self.folders.with_user(caller).create({
            'name': 'Ceiling inheriting created', 'parent_id': self.parent.id,
            'owner_id': self.outsider.id})
        self.assertEqual((created.owner_id, created.inherit_access), (self.outsider, True))
        # a restricted folder that starts to inherit has no ceiling after the write
        folder = self._folder('Ceiling opened', owner=self.outsider, members=[(caller, 'manage')])
        folder.with_user(caller).write({'inherit_access': True})
        self.assertTrue(folder.inherit_access)

    def test_a_document_administrator_and_the_superuser_are_not_held_to_the_ceiling(self):
        _caller, folder = self._managed('none')
        folder.with_user(self.admin).write({'owner_id': self.outsider.id})
        self.assertEqual(folder.owner_id, self.outsider)
        created = self.folders.with_user(self.admin).create({
            'name': 'Ceiling by an administrator', 'parent_id': self.parent.id,
            'owner_id': self.outsider.id, 'inherit_access': False})
        self.assertEqual(created.owner_id, self.outsider)
        # the owner stays within no grant when the administrator moves the folder or restricts one
        folder.with_user(self.admin).write({'parent_id': self.other_parent.id})
        self.assertEqual(folder.parent_id, self.other_parent)
        inheriting = self._folder('Ceiling restricted by an administrator', restricted=False,
                                  owner=self.outsider)
        inheriting.with_user(self.admin).write({'inherit_access': False})
        self.assertFalse(inheriting.inherit_access)
        # the superuser is not asked whether the owner is a document user either
        for owner in (self.plain, self.portal):
            folder.sudo().write({'owner_id': owner.id})
            self.assertEqual(folder.owner_id, owner)
            created = self.folders.sudo().create({
                'name': 'Ceiling by the superuser %s' % owner.id, 'parent_id': self.parent.id,
                'owner_id': owner.id, 'inherit_access': False})
            self.assertEqual(created.owner_id, owner)

    def test_the_owner_a_folder_already_has_is_not_a_change(self):
        caller = self.callers['manage']
        folder = self._folder('Ceiling same owner', owner=self.outsider,
                              members=[(caller, 'manage')])
        events = len(self._events(folder))
        folder.with_user(caller).write({'owner_id': self.outsider.id})
        folder.with_user(caller).write({'owner_id': self.outsider.id, 'name': 'Ceiling kept'})
        self.assertEqual((folder.name, folder.owner_id), ('Ceiling kept', self.outsider))
        self.assertEqual(len(self._events(folder)), events)
        # in a write of several folders only the ones whose owner changes are looked at
        other = self._folder('Ceiling other owner', members=[(caller, 'manage')])
        self._refused(lambda: (folder | other).with_user(caller).write(
            {'owner_id': self.outsider.id}))
        self.assertEqual((folder.owner_id, other.owner_id), (self.outsider, self.admin))

    def test_a_manager_who_cannot_read_the_parent_gets_the_same_refusal(self):
        """Whoever the owner is, a grant of a parent that the caller cannot read is not found."""
        for standing in ('none', 'owner only'):
            with self.subTest(standing):
                caller, folder = self._managed(standing)
                self.assertFalse(self._sees_parent(caller))
                answers = set()
                for owner in (self.outsider, self.sibling, self.parent_owner):
                    with self.assertRaises(ValidationError) as caught, self.cr.savepoint():
                        folder.with_user(caller).write({'owner_id': owner.id})
                    answers.add((type(caught.exception), caught.exception.args))
                self.assertEqual(answers, {(ValidationError, (_CEILING,))})

    # -- the owner is a document user -----------------------------------------------------------
    def test_the_owner_must_be_a_document_user(self):
        caller = self.callers['manage']
        values = {
            'an internal user outside the group': self.plain.id,
            'a portal user': self.portal.id,
            'a user that does not exist': self.env['res.users'].sudo().with_context(
                active_test=False).search([], order='id desc', limit=1).id + 1000,
            'text': '5', 'a float': 5.5, 'a flag': True, 'zero': 0, 'a negative number': -3}
        restricted = self._folder('Ceiling checked restricted', members=[(caller, 'manage')])
        inheriting = self._folder('Ceiling checked inheriting', restricted=False,
                                  members=[(caller, 'manage')])
        # the message of the member rows, so that the translation catalog does not grow
        with self.assertRaises(ValidationError) as by_row, self.cr.savepoint():
            self.access.with_user(self.admin).create({
                'folder_id': inheriting.id, 'subject_type': 'user', 'user_id': self.plain.id,
                'level': 'read'})
        self.assertEqual(by_row.exception.args, (_NOT_A_USER,))
        for label, value in values.items():
            for who in (caller, self.admin):
                for folder in (restricted, inheriting):
                    with self.subTest(label, who=who.login, restricted=not folder.inherit_access):
                        self._refused(lambda: folder.with_user(who).write({'owner_id': value}),
                                      _NOT_A_USER)
                        self.assertEqual(folder.owner_id, self.admin)
                for inherit in (True, False):
                    with self.subTest(label, who=who.login, create=inherit):
                        self._refused(lambda: self.folders.with_user(who).create({
                            'name': 'Ceiling checked create', 'parent_id': self.parent.id,
                            'owner_id': value, 'inherit_access': inherit}), _NOT_A_USER)

    def test_clearing_the_owner_is_not_limited_and_the_next_owner_is(self):
        caller, folder = self._managed('manage')
        for value in (False, None):
            with self.subTest(value):
                folder.sudo().write({'owner_id': self.admin.id})
                folder.with_user(caller).write({'owner_id': value})
                self.assertFalse(folder.owner_id)
                self.assertEqual(self._events(folder)[-1:].detail,
                                 {'owner_id': 0, 'previous_owner_id': self.admin.id})
        # a folder without an owner takes the first one under the same rules
        self._refused(lambda: folder.with_user(caller).write({'owner_id': self.outsider.id}))
        folder.with_user(caller).write({'owner_id': self.sibling.id})
        self.assertEqual(folder.owner_id, self.sibling)

    # -- the ceiling cannot be passed in two steps ----------------------------------------------
    def test_an_owner_without_manage_on_the_parent_cannot_keep_a_folder_that_stops_inheriting(self):
        caller = self.callers['manage']
        for flag in (False, 0, None):
            with self.subTest(flag):
                # The caller manages the folder through the parent: no row of its own to look at.
                folder = self._folder('Ceiling two steps %s' % flag, restricted=False)
                self.assertTrue(folder.with_user(caller)._woow_has_rank(4))
                self.assertFalse(folder.access_ids)
                # step one: an owner while the folder inherits is not limited
                folder.with_user(caller).write({'owner_id': self.outsider.id})
                # step two: the restriction, alone or with the owner that the folder has
                self._refused(lambda: folder.with_user(caller).write({'inherit_access': flag}))
                self._refused(lambda: folder.with_user(caller).write(
                    {'owner_id': self.outsider.id, 'inherit_access': flag}))
                self.assertTrue(folder.inherit_access)
                self.assertEqual(folder.owner_id, self.outsider)
        # the same restriction for an owner who has the right on the parent
        folder = self._folder('Ceiling restricted at last', restricted=False, owner=self.sibling)
        folder.with_user(caller).write({'inherit_access': False})
        self.assertFalse(folder.inherit_access)
        self.assertIn(self.sibling, folder.eff_manage_user_ids)

    def test_the_owner_of_a_restricted_folder_must_have_manage_on_the_new_parent(self):
        caller = self.callers['manage']
        folder = self._folder('Ceiling mover', owner=self.sibling, members=[(caller, 'manage')])
        self._refused(lambda: folder.with_user(caller).write({'parent_id': self.other_parent.id}))
        self.assertEqual(folder.parent_id, self.parent)
        # once the owner has the right on the new parent the same move is made
        self.access.sudo().create({'folder_id': self.other_parent.id, 'subject_type': 'user',
                                   'user_id': self.sibling.id, 'level': 'manage'})
        folder.with_user(caller).write({'parent_id': self.other_parent.id})
        self.assertEqual(folder.parent_id, self.other_parent)
        # a write that keeps the parent is not a move, and an inheriting folder has no ceiling
        stays = self._folder('Ceiling stays', owner=self.outsider, members=[(caller, 'manage')])
        stays.with_user(caller).write({'parent_id': self.parent.id})
        inherits = self._folder('Ceiling moves', owner=self.outsider, restricted=False,
                                members=[(caller, 'manage')])
        inherits.with_user(caller).write({'parent_id': self.other_parent.id})
        self.assertEqual((stays.parent_id, inherits.parent_id), (self.parent, self.other_parent))
        # the parent after the write counts for an owner that the same write names
        mover = self._folder('Ceiling mover and owner', members=[(caller, 'manage')])
        self._refused(lambda: mover.with_user(caller).write(
            {'owner_id': self.parent_owner.id, 'parent_id': self.other_parent.id}))
        mover.with_user(caller).write({'owner_id': caller.id, 'parent_id': self.other_parent.id})
        self.assertEqual((mover.owner_id, mover.parent_id), (caller, self.other_parent))

    def test_the_move_wizard_is_held_to_the_same_check(self):
        caller = self.callers['manage']
        folder = self._folder('Ceiling wizard', owner=self.sibling, members=[(caller, 'manage')])

        def wizard():
            return self.env['woow.document.move.wizard'].with_user(caller).create({
                'folder_ids': [Command.set(folder.ids)], 'target_folder_id': self.other_parent.id})

        self._refused(lambda: wizard().action_move())
        self.assertEqual(folder.parent_id, self.parent)
        self.access.sudo().create({'folder_id': self.other_parent.id, 'subject_type': 'user',
                                   'user_id': self.sibling.id, 'level': 'manage'})
        wizard().action_move()
        self.assertEqual(folder.parent_id, self.other_parent)

    # -- what is as it was ----------------------------------------------------------------------
    def test_a_caller_who_is_no_document_user_is_refused_by_the_access_rights(self):
        """The access rights of the model have their say before the owner is looked at."""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.allow_root_create', 'True')
        for who in (self.plain, self.portal):
            with self.subTest(who.login), self.assertRaises(AccessError), self.cr.savepoint():
                self.folders.with_user(who).create({'name': 'Ceiling no access %s' % who.id})

    def test_an_editor_still_cannot_choose_the_owner_or_restrict_a_folder(self):
        folders = self.folders.with_user(self.editor)
        child = folders.create({'name': 'Ceiling editor child', 'parent_id': self.parent.id})
        self.assertEqual((child.owner_id, child.inherit_access), (self.parent_owner, True))
        for label, extra in (('an owner', {'owner_id': self.editor.id}),
                             ('a restriction', {'inherit_access': False})):
            with self.subTest(label), self.assertRaises(AccessError), self.cr.savepoint():
                folders.create({'name': 'Ceiling editor %s' % label,
                                'parent_id': self.parent.id, **extra})
        # the owner of the parent is taken as it is, whoever that is
        odd = self.folders.sudo().create({'name': 'Ceiling odd parent', 'owner_id': self.plain.id})
        self.access.sudo().create({'folder_id': odd.id, 'subject_type': 'user',
                                   'user_id': self.editor.id, 'level': 'edit'})
        self.env.flush_all()
        child = folders.create({'name': 'Ceiling odd child', 'parent_id': odd.id})
        self.assertEqual(child.owner_id, self.plain)

    def test_a_stored_default_is_not_a_way_around_the_check(self):
        """A default owner that a caller stored is checked as an owner that the call names."""
        caller = self.callers['manage']
        folders = self.folders.with_user(caller)
        defaults = self.env['ir.default'].with_user(caller)
        defaults.set('woow.document.folder', 'owner_id', self.outsider.id, user_id=True)
        self._refused(lambda: folders.create({
            'name': 'Ceiling default restricted', 'parent_id': self.parent.id,
            'inherit_access': False}))
        # a folder that inherits has no ceiling, and takes the stored owner as it always did
        created = folders.create({'name': 'Ceiling default inheriting',
                                  'parent_id': self.parent.id})
        self.assertEqual(created.owner_id, self.outsider)
        defaults.set('woow.document.folder', 'owner_id', self.plain.id, user_id=True)
        self._refused(lambda: folders.create({
            'name': 'Ceiling default plain', 'parent_id': self.parent.id}), _NOT_A_USER)
        defaults.set('woow.document.folder', 'owner_id', self.sibling.id, user_id=True)
        created = folders.create({'name': 'Ceiling default sibling', 'parent_id': self.parent.id,
                                  'inherit_access': False})
        self.assertEqual(created.owner_id, self.sibling)
        # an editor never chooses the owner, a stored default included
        self.env['ir.default'].with_user(self.editor).set(
            'woow.document.folder', 'owner_id', self.outsider.id, user_id=True)
        child = self.folders.with_user(self.editor).create({
            'name': 'Ceiling default editor', 'parent_id': self.parent.id})
        self.assertEqual(child.owner_id, self.parent_owner)

    # -- the audit row and the cost -------------------------------------------------------------
    def test_a_new_owner_is_one_audit_row_for_each_folder(self):
        caller = self.callers['manage']
        folder = self._folder('Ceiling audited', members=[(caller, 'manage')])
        other = self._folder('Ceiling audited too', owner=self.parent_owner,
                             members=[(caller, 'manage')])
        marked = max(self._events().ids or [0])
        (folder | other).with_user(caller).write({'owner_id': self.sibling.id})
        rows = self._events().filtered(lambda row: row.id > marked)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.folder_id: row.detail for row in rows}, {
            folder: {'owner_id': self.sibling.id, 'previous_owner_id': self.admin.id},
            other: {'owner_id': self.sibling.id, 'previous_owner_id': self.parent_owner.id}})
        self.assertEqual({(row.user_id, row.channel, row.company_id) for row in rows},
                         {(caller, 'rpc', folder.company_id)})
        # no row for an owner that stays, a refused write or a folder that is made
        marked = max(self._events().ids or [0])
        folder.with_user(caller).write({'owner_id': self.sibling.id, 'name': 'Ceiling audited 2'})
        self._refused(lambda: folder.with_user(caller).write({'owner_id': self.outsider.id}))
        self.folders.with_user(caller).create({
            'name': 'Ceiling audited made', 'parent_id': self.parent.id,
            'owner_id': self.sibling.id})
        self.assertFalse(self._events().filtered(lambda row: row.id > marked))
        # the superuser leaves one as well, under the name of the cron
        folder.sudo().write({'owner_id': self.admin.id})
        row = self._events(folder)[-1]
        self.assertEqual((row.channel, row.detail), (
            'cron', {'owner_id': self.admin.id, 'previous_owner_id': self.sibling.id}))

    def test_the_checks_are_made_only_when_due_and_cost_one_search(self):
        caller = self.callers['manage']
        model = type(self.folders)
        original = model.search_count
        calls = []

        def counting(records, *args, **kwargs):
            calls.append(records._name)
            return original(records, *args, **kwargs)

        def searches(call):
            del calls[:]
            with patch.object(model, 'search_count', counting):
                call()
            return calls.count('woow.document.folder')

        restricted = [self._folder('Ceiling cost %d' % number, members=[(caller, 'manage')])
                      for number in range(3)]
        # a write that names none of the three fields asks nothing of the owner
        with patch.object(model, '_woow_check_owners', autospec=True) as check:
            restricted[0].with_user(caller).write({'name': 'Ceiling cost renamed'})
        check.assert_not_called()
        # nor is the superuser, whatever the value
        su_folder = self._folder('Ceiling cost superuser', members=[(caller, 'manage')])
        with patch.object(model, '_woow_check_owners', autospec=True) as check:
            su_folder.sudo().write({'owner_id': self.plain.id, 'inherit_access': False})
        check.assert_not_called()
        self.assertEqual(searches(lambda: restricted[0].with_user(caller).write(
            {'owner_id': self.sibling.id})), 1)
        # the same answer is not asked for twice, and the owner that stays costs nothing
        self.assertEqual(searches(lambda: self.folders.browse(
            [folder.id for folder in restricted[1:]]).with_user(caller).write(
                {'owner_id': self.sibling.id})), 1)
        self.assertEqual(searches(lambda: restricted[0].with_user(caller).write(
            {'owner_id': self.sibling.id})), 0)
        inheriting = self._folder('Ceiling cost inheriting', restricted=False,
                                  members=[(caller, 'manage')])
        self.assertEqual(searches(lambda: inheriting.with_user(caller).write(
            {'owner_id': self.outsider.id, 'parent_id': self.other_parent.id})), 0)
        self.assertEqual(searches(lambda: restricted[0].with_user(self.admin).write(
            {'owner_id': self.outsider.id})), 0)

    def test_the_rank_domain_is_the_callers_unless_a_user_is_named(self):
        caller = self.callers['manage']
        folders = self.folders.with_user(caller)
        self.assertEqual(folders._woow_rank_domain(4), [
            '|', ('eff_manage_user_ids', 'in', [caller.id]),
            ('eff_manage_group_ids', 'in', caller.groups_id.ids)])
        self.assertEqual(folders._woow_rank_domain(2, self.sibling), [
            '|', ('eff_upload_user_ids', 'in', [self.sibling.id]),
            ('eff_upload_group_ids', 'in', self.sibling.groups_id.ids)])
        # nobody is not the caller
        self.assertEqual(folders._woow_rank_domain(4, self.env['res.users']), [
            '|', ('eff_manage_user_ids', 'in', [False]), ('eff_manage_group_ids', 'in', [])])
        with self.assertRaises(ValueError):
            folders._woow_rank_domain(5, self.sibling)
