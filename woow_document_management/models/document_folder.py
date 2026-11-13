# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Folders (IF-MODEL, owned by core-models-acl).

Fields follow arch section 3.1 and 3.3; permissions, rank probes and display names follow the
record rules and grants of the user.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError

from ..services.constants import TREE_QUERY_LIMIT
from ..services import folding

_MANAGER = 'woow_document_management.group_woow_document_manager'
_USER = 'woow_document_management.group_woow_document_user'
_LEVEL_RANK = {'read': 1, 'upload': 2, 'edit': 3, 'manage': 4}
_EFFECTIVE_FIELDS = {
    'user': (('eff_read_user_ids', 1), ('eff_upload_user_ids', 2),
             ('eff_edit_user_ids', 3), ('eff_manage_user_ids', 4)),
    'group': (('eff_read_group_ids', 1), ('eff_upload_group_ids', 2),
              ('eff_edit_group_ids', 3), ('eff_manage_group_ids', 4)),
    'partner': (('eff_read_partner_ids', 1), ('eff_upload_partner_ids', 2)),
}
_EFF_FIELDS = frozenset(name for lines in _EFFECTIVE_FIELDS.values() for name, _rank in lines)
_SUBJECT_FIELD = {'user': 'user_id', 'group': 'group_id', 'partner': 'partner_id'}
_SUBJECT_MODEL = {'user': 'res.users', 'group': 'res.groups', 'partner': 'res.partner'}
_SERVER_ONLY_FIELDS = frozenset({
    'parent_path', 'complete_name', 'in_trash',
    'has_restricted_descendant', 'subtree_legal_hold', 'woow_user_rank',
    'woow_has_portal_members', 'document_count', 'subfolder_count',
    'effective_access_lines', 'child_ids', 'document_ids',
})
_EFF_LOCAL_DEPENDS = (
    'inherit_access', 'owner_id', 'parent_id',
    'access_ids.level', 'access_ids.subject_type', 'access_ids.user_id',
    'access_ids.group_id', 'access_ids.partner_id',
)


def _eff(comodel, relation, column, field_name):
    return fields.Many2many(
        comodel, relation, 'folder_id', column, compute='_compute_effective', store=True,
        recursive=True, groups='base.group_user',
        depends=(*_EFF_LOCAL_DEPENDS, f'parent_id.{field_name}'))


def _m2o_value(value, comodel):
    """The id that a many2one to ``comodel`` keeps for ``value`` (False for none), None for no id.

    This is the one reading of the value of a many2one that every guard of a folder uses (the
    parent, the owner and the company), and it follows ``Many2one.convert_to_cache``: a number is an
    id only when it is an ``int`` (a bool is not one), a record of the comodel is its id (several
    records, or a record of another model, have none), a tuple is its first element (the pair of an
    id and a name) and ``None``, ``False`` and an empty list are no record. Anything else (a list with
    something in it, a text, a float, a dict) has no id: the ORM keeps NULL for it, so a guard that
    read an id from it would judge a record that is not the one that is stored.
    """
    if isinstance(value, models.BaseModel):
        if value._name != comodel or len(value) > 1:
            return None
        if not value:
            return False
        return value.id if type(value.id) is int else None
    if isinstance(value, list):
        return False if not value else None
    if isinstance(value, tuple):
        value = value[0] if value else False
    if value is None or value is False:
        return False
    return value if type(value) is int else None


def _company_value(value):
    """The company a write names, as an id or False; None when the value has no known shape."""
    return _m2o_value(value, 'res.company')


def _stored_flag(vals, name, default):
    """What the ORM keeps for the Boolean ``name`` that the values of a create or a write name.

    A Boolean column holds ``bool(value)``: ``0``, ``0.0``, ``None``, ``''`` and ``[]`` are False as
    ``False`` is, and any other number or text (``'false'`` too) is True. A guard decides on that,
    never on the type of the value that came in: ``value is False`` is true for the object ``False``
    and for no other value that the column keeps as False. ``default`` stands for a key that the
    values leave out.
    """
    return bool(vals[name]) if name in vals else default


class WoowDocumentFolder(models.Model):
    _name = 'woow.document.folder'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'woow.document.guard.mixin']
    _description = 'Document folder'
    _parent_store = True
    _order = 'sequence, name, id'
    _check_company_auto = True
    _rec_names_search = ['name']
    _woow_allowed_defaults = frozenset({'default_parent_id'})
    _woow_own_default_fields = _SERVER_ONLY_FIELDS | _EFF_FIELDS | frozenset({
        'company_id', 'legal_hold', 'inherit_access', 'active', 'trashed_date', 'trashed_by_id',
        'restored_date', 'activity_ids', 'message_follower_ids', 'message_ids',
        'website_message_ids'})

    name = fields.Char(required=True, tracking=True)
    complete_name = fields.Char(compute='_compute_complete_name', store=True, recursive=True,
                                groups=_MANAGER)
    parent_id = fields.Many2one('woow.document.folder', index=True, ondelete='restrict',
                                check_company=True)
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many('woow.document.folder', 'parent_id')
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, index=True,
                                 ondelete='restrict')
    owner_id = fields.Many2one('res.users', default=lambda self: self.env.user, tracking=True,
                               check_company=True, ondelete='set null', groups='base.group_user')
    description = fields.Html(sanitize=True)
    color = fields.Integer()
    image_128 = fields.Image(max_width=128, max_height=128)
    tag_ids = fields.Many2many('woow.document.tag', 'woow_document_folder_tag_rel', 'folder_id',
                               'tag_id', groups='base.group_user')
    access_ids = fields.One2many('woow.document.folder.access', 'folder_id',
                                 groups='base.group_user')
    inherit_access = fields.Boolean(default=True, tracking=True, groups='base.group_user')

    eff_read_user_ids = _eff('res.users', 'woow_doc_folder_eff_r_user_rel', 'user_id', 'eff_read_user_ids')
    eff_upload_user_ids = _eff('res.users', 'woow_doc_folder_eff_u_user_rel', 'user_id', 'eff_upload_user_ids')
    eff_edit_user_ids = _eff('res.users', 'woow_doc_folder_eff_e_user_rel', 'user_id', 'eff_edit_user_ids')
    eff_manage_user_ids = _eff('res.users', 'woow_doc_folder_eff_m_user_rel', 'user_id', 'eff_manage_user_ids')
    eff_read_group_ids = _eff('res.groups', 'woow_doc_folder_eff_r_group_rel', 'group_id', 'eff_read_group_ids')
    eff_upload_group_ids = _eff('res.groups', 'woow_doc_folder_eff_u_group_rel', 'group_id', 'eff_upload_group_ids')
    eff_edit_group_ids = _eff('res.groups', 'woow_doc_folder_eff_e_group_rel', 'group_id', 'eff_edit_group_ids')
    eff_manage_group_ids = _eff('res.groups', 'woow_doc_folder_eff_m_group_rel', 'group_id', 'eff_manage_group_ids')
    eff_read_partner_ids = _eff('res.partner', 'woow_doc_folder_eff_r_partner_rel', 'partner_id', 'eff_read_partner_ids')
    eff_upload_partner_ids = _eff('res.partner', 'woow_doc_folder_eff_u_partner_rel', 'partner_id', 'eff_upload_partner_ids')

    document_properties_definition = fields.PropertiesDefinition(groups='base.group_user')
    document_ids = fields.One2many('woow.document', 'folder_id')
    document_count = fields.Integer(compute='_compute_counts', readonly=True)
    subfolder_count = fields.Integer(compute='_compute_counts', readonly=True)
    effective_access_lines = fields.Json(compute='_compute_effective_access_lines',
                                         groups='base.group_user', readonly=True)

    active = fields.Boolean(default=True)
    in_trash = fields.Boolean(compute='_compute_in_trash', store=True, recursive=True, index=True)
    trashed_date = fields.Datetime(groups='base.group_user')
    trashed_by_id = fields.Many2one('res.users', ondelete='set null', groups='base.group_user')
    restored_date = fields.Datetime(groups='base.group_user')
    has_restricted_descendant = fields.Boolean(compute='_compute_has_restricted_descendant',
                                               store=True, recursive=True, groups='base.group_user')
    legal_hold = fields.Boolean(groups=_MANAGER)
    subtree_legal_hold = fields.Boolean(compute='_compute_subtree_legal_hold', store=True,
                                        recursive=True, groups=_MANAGER)

    woow_user_rank = fields.Integer(compute='_compute_woow_user_rank')
    woow_has_portal_members = fields.Boolean(compute='_compute_woow_has_portal_members')

    def init(self):
        super().init()
        self.env.cr.execute(  # sql-ok: Q3
            """CREATE UNIQUE INDEX IF NOT EXISTS woow_document_folder_name_uniq
               ON woow_document_folder ((COALESCE(parent_id, 0)),
                                         (COALESCE(company_id, 0)), lower(name))
               WHERE active""")
        self._woow_recompute_subtree_flags()

    def _compute_effective(self):
        """Merge inherited and local grants; owner has manage and portal is capped at upload."""
        for folder in self:
            ranks = {subject: {} for subject in _EFFECTIVE_FIELDS}
            if folder.inherit_access and folder.parent_id:
                for subject, thresholds in _EFFECTIVE_FIELDS.items():
                    for field_name, threshold in thresholds:
                        for principal in folder.parent_id[field_name]:
                            ranks[subject][principal.id] = max(
                                ranks[subject].get(principal.id, 0), threshold)
            for line in folder.access_ids:
                subject = line.subject_type
                if subject not in ranks:
                    continue
                principal = line[_SUBJECT_FIELD[subject]]
                if not principal:
                    continue
                level = _LEVEL_RANK.get(line.level, 0)
                if subject == 'partner':
                    level = min(level, 2)
                ranks[subject][principal.id] = max(ranks[subject].get(principal.id, 0), level)
            if folder.owner_id:
                ranks['user'][folder.owner_id.id] = 4
            for subject, thresholds in _EFFECTIVE_FIELDS.items():
                model = self.env[_SUBJECT_MODEL[subject]]
                for field_name, threshold in thresholds:
                    folder[field_name] = model.browse([
                        principal_id for principal_id, rank in ranks[subject].items()
                        if rank >= threshold])

    @api.model_create_multi
    def create(self, vals_list):
        """Apply the site root policy and parent edit grant before creating folders.

        A document user who is not the superuser also has the owner of the new folder checked
        (``_woow_check_owner``): the one that the values name, or else the one that the default of
        the field gives when core makes the row, read with the context that ``create`` hands to core
        (``_woow_without_ambient_defaults``). An editor does not choose an owner: the folder gets the
        one of its parent, and does not turn inheritance off. A caller who is no document user is
        refused by the access rights of the model, so nothing is asked about the owner.
        ``inherit_access`` is read as the column keeps it (``_stored_flag``).

        The parent, the owner and the company that were judged are written into the values, each read
        once (``_woow_parent_value``, ``_woow_owner_value``, ``_company_value``), so that the folder
        is made with what was checked. What the ORM then holds is judged again, whatever the values
        were (``_woow_check_stored_state``), and that is the check that counts.
        """
        manager = self.env.su or self.env.user.has_group(_MANAGER)
        checks_owner = not self.env.su and self.env.user.has_group(_USER)
        # the defaults of the rows that core makes, not those that a key of the caller's context gives
        defaults = self._woow_without_ambient_defaults().default_get(['parent_id', 'owner_id'])
        answers = {}
        for vals in vals_list:
            if 'name' in vals:
                vals['name'] = (vals['name'] or '').strip()
                if not vals['name']:
                    raise UserError(_('A folder name is required.'))
            if not self.env.su and (_SERVER_ONLY_FIELDS.intersection(vals) or
                                    any(key.startswith('eff_') for key in vals)):
                raise AccessError(_('This folder field cannot be changed directly.'))
            if not manager and 'company_id' in vals:
                # A folder that belongs to no company is open to every company's
                # administrators (R-F0 / R-D0), so only a document administrator makes one.
                company = _company_value(vals['company_id'])
                if not company:
                    raise AccessError(_('Only document administrators can do this.'))
                vals['company_id'] = company
            if self.env.su:
                continue
            parent = self._woow_parent_value(
                vals['parent_id'] if 'parent_id' in vals else defaults.get('parent_id'))
            vals['parent_id'] = parent.id or False
            inherit = _stored_flag(vals, 'inherit_access', True)
            vals['inherit_access'] = inherit
            editor = False
            if not parent:
                self._woow_require_root_right()
                answers[('root',)] = True
            elif not manager:
                if not parent._woow_rank_once(3, answers) or parent.in_trash:
                    raise AccessError(_('You do not have enough rights on this folder.'))
                if not parent._woow_rank_once(4, answers):
                    # An editor can organize a child, but must not gain its
                    # implicit owner/manage grant above the parent's ceiling.
                    if vals.get('owner_id') or not inherit:
                        raise AccessError(_('You do not have enough rights on this folder.'))
                    vals['owner_id'] = parent.owner_id.id or False
                    editor = True
            if checks_owner and not editor:
                # The owner has the manage level of the new folder: a document user, and when the
                # folder does not inherit its access, within the grants of its parent (AC-2).
                owner = self._woow_owner_value(
                    vals['owner_id'] if 'owner_id' in vals else defaults.get('owner_id'))
                vals['owner_id'] = owner.id or False
                self._woow_ask_owner(None, owner, parent, inherit, answers)
            if vals.get('active', True):
                self._woow_check_name_free(
                    vals.get('name'), vals['parent_id'],
                    vals.get('company_id', self.env.company.id))
        records = super(WoowDocumentFolder, self.with_context(
            mail_create_nosubscribe=True, mail_create_nolog=True)).create(vals_list)
        if not self.env.su:
            records._woow_check_stored_state({}, answers)
        return records

    def write(self, vals):
        """Preserve effective access as explicit grants when inheritance is disabled.

        A caller who is not the superuser also has the owners checked when the values name the
        owner, a parent or the access (``_woow_check_owners``), and a new owner is one audit row.
        ``inherit_access`` is read as the column keeps it (``_stored_flag``): ``0``, ``None`` and
        ``''`` turn it off as ``False`` does, for the guards below and for the grants that are
        kept as member rows. The parent, the owner and the company that were judged are written
        into the values, each read once, so that the rows get what was checked; what the ORM holds
        after the write is judged again (``_woow_check_stored_state``), and that is the check that
        counts.
        """
        if 'name' in vals:
            vals = dict(vals, name=(vals['name'] or '').strip())
            if not vals['name']:
                raise UserError(_('A folder name is required.'))
        if vals and not self.env.su:
            if not self.env.user._is_internal():
                raise AccessError(_('Portal users cannot modify folders.'))
            if {'active', 'trashed_date', 'trashed_by_id', 'restored_date'}.intersection(vals):
                raise UserError(_('Use the document trash actions to change folder state.'))
            if _SERVER_ONLY_FIELDS.intersection(vals) or any(
                    key.startswith('eff_') for key in vals):
                raise AccessError(_('This folder field cannot be changed directly.'))
            privileged = {'parent_id', 'inherit_access', 'owner_id', 'company_id',
                          'access_ids', 'active', 'trashed_date', 'trashed_by_id',
                          'restored_date', 'legal_hold'}
            self._woow_require_rank(4 if privileged.intersection(vals) else 3)
            if 'company_id' in vals and not self.env.user.has_group(_MANAGER):
                # Only a document administrator moves a folder to another company or to
                # none. A member with the manage grant keeps the company of the folder,
                # because the same write on the root of a subtree would hand the whole
                # subtree to another company's administrators, folder by folder.
                new_company = _company_value(vals['company_id'])
                if any(new_company != (folder.company_id.id or False) for folder in self):
                    raise AccessError(_('Only document administrators can do this.'))
                vals = dict(vals, company_id=new_company)
        # the flag is read as the column keeps it (``_stored_flag``): 0, None and '' disable it too
        disables_inheritance = not _stored_flag(vals, 'inherit_access', True)
        if disables_inheritance and 'access_ids' in vals:
            raise UserError(_('Save member changes before disabling inherited access.'))
        if disables_inheritance and 'parent_id' in vals:
            raise UserError(_('Save the new parent before disabling inherited access.'))
        answers, before = {}, None
        if 'parent_id' in vals:
            new_parent = self._woow_parent_value(vals['parent_id'])
            vals = dict(vals, parent_id=new_parent.id or False)
            if not new_parent and self.filtered('parent_id'):
                # Moving a folder to the top level is creating a root folder.
                self._woow_require_root_right()
                answers[('root',)] = True
            for folder in self:
                if new_parent and not self.env.su:
                    new_parent._woow_require_rank(3)
                if new_parent and new_parent.in_trash:
                    raise UserError(_('Cannot move a folder into the trash.'))
                if new_parent and folder.company_id != new_parent.company_id:
                    raise UserError(_('Cannot move a folder between companies.'))
                if new_parent and (new_parent.id == folder.id or
                                   f'{folder.id}/' in (new_parent.parent_path or '')):
                    raise UserError(_('A folder cannot be moved below itself.'))
                folder.access_ids._check_parent_ceiling(new_parent)
        if not self.env.su and {'name', 'parent_id', 'company_id'}.intersection(vals):
            for folder in self.filtered('active'):
                self._woow_check_name_free(
                    vals.get('name', folder.name),
                    vals['parent_id'] if 'parent_id' in vals else folder.parent_id.id,
                    vals['company_id'] if 'company_id' in vals else folder.company_id.id,
                    exclude=folder.ids)
        if not self.env.su and {'owner_id', 'parent_id', 'inherit_access', 'company_id'}.intersection(vals):
            before = self._woow_state()
            if {'owner_id', 'parent_id', 'inherit_access'}.intersection(vals):
                if 'owner_id' in vals:
                    vals = dict(vals, owner_id=self._woow_owner_value(vals['owner_id']).id or False)
                self._woow_check_owners(vals, answers)
        access_model = self.env['woow.document.folder.access']
        if disables_inheritance:
            for folder in self.filtered('inherit_access'):
                folder.access_ids._check_parent_ceiling(folder.parent_id)
                ranks = {}
                for subject, thresholds in _EFFECTIVE_FIELDS.items():
                    for field_name, threshold in thresholds:
                        for principal in folder[field_name]:
                            key = (subject, principal.id)
                            ranks[key] = max(ranks.get(key, 0), threshold)
                if folder.owner_id:
                    ranks.pop(('user', folder.owner_id.id), None)
                existing = {(line.subject_type, line[_SUBJECT_FIELD[line.subject_type]].id): line
                            for line in folder.access_ids if line.subject_type in _SUBJECT_FIELD}
                for (subject, principal_id), rank in ranks.items():
                    line = existing.get((subject, principal_id))
                    level = next(name for name, value in _LEVEL_RANK.items() if value == rank)
                    if line:
                        if _LEVEL_RANK[line.level] < rank:
                            line.write({'level': level})
                    else:
                        access_model.create({
                            'folder_id': folder.id, 'subject_type': subject,
                            _SUBJECT_FIELD[subject]: principal_id, 'level': level,
                        })
        hold_before = {folder.id: folder.legal_hold for folder in self} if 'legal_hold' in vals else {}
        owner_before = ({folder.id: folder.owner_id.id or 0 for folder in self}
                        if 'owner_id' in vals else {})
        result = super().write(vals)
        if before is not None:
            self._woow_check_stored_state(before, answers)
        if hold_before:
            for folder in self:
                if hold_before[folder.id] != folder.legal_hold:
                    self.env['woow.document.log']._log_event(
                        'legal_hold', channel=self._woow_log_channel(),
                        folder_id=folder.id, company_id=folder.company_id.id,
                        detail={'hold': bool(folder.legal_hold)})
        if owner_before:
            for folder in self:
                if owner_before[folder.id] != (folder.owner_id.id or 0):
                    # The owner has the manage level of the folder: a new one is a change of access.
                    self.env['woow.document.log']._log_event(
                        'access_change', channel=self._woow_log_channel(),
                        folder_id=folder.id, company_id=folder.company_id.id,
                        detail={'owner_id': folder.owner_id.id or 0,
                                'previous_owner_id': owner_before[folder.id]})
        return result

    @api.model
    def _woow_owner_value(self, value):
        """The ``res.users`` record (empty for none) that a value of ``owner_id`` names.

        ``_m2o_value`` reads a many2one value the way the ORM keeps it. What has no id, and a number
        that no record has (the ORM would keep 0 as an id), is refused with the message of the
        member rows, so that it cannot reach the field as another kind of value.
        """
        owner_id = _m2o_value(value, 'res.users')
        if owner_id is None or (owner_id is not False and owner_id < 1):
            raise ValidationError(_('Choose a document user.'))
        return self.env['res.users'].browse(owner_id or [])

    @api.model
    def _woow_parent_value(self, value):
        """The folder (empty for none) that a value of ``parent_id`` names.

        The same reading as the owner's: ``_m2o_value`` follows what the ORM keeps for the value,
        which is NULL for a list, a text, a float, a flag or a dict, and a guard that took such a
        value for a folder (of an inactive folder, for which no other check looks) would judge a
        parent that is not the one that is stored. What has no id, and a number that no folder has,
        is refused; no value, ``False`` and ``None`` are the top level.
        """
        parent_id = _m2o_value(value, self._name)
        if parent_id is None or (parent_id is not False and parent_id < 1):
            raise ValidationError(_('Choose an available folder.'))
        return self.browse(parent_id or [])

    def _woow_state(self):
        """{id: (owner, parent, inherits its access, company)} as the records of ``self`` hold it."""
        return {folder.id: (folder.owner_id, folder.parent_id, bool(folder.inherit_access),
                            folder.company_id) for folder in self}

    def _woow_rank_once(self, n, answers):
        """``_woow_has_rank(n)`` of the one folder in ``self``, asked once for every ``answers``."""
        key = ('rank', n, self.id)
        if key not in answers:
            answers[key] = self._woow_has_rank(n)
        return answers[key]

    def _woow_check_owners(self, vals, answers):
        """Hold the owners of ``self`` to ``_woow_check_owner`` for a write of ``vals``.

        The owner has the manage level of the folder (``_compute_effective``), so the rule that a
        folder that does not inherit its access only narrows the access of its parent (AC-2) has to
        cover the owner as it covers the member rows. A new owner passes the whole check. The owner
        who stays is held to the ceiling when the write moves the folder below another parent or
        stops it from inheriting, so that the ceiling cannot be passed in two steps: first an owner
        while the folder inherits, then the restriction. The parent and the access that count are
        the ones the folder has after the write, and a flag is read as the ORM stores it. This asks
        before the write, for a refusal that costs nothing; ``_woow_check_stored_state`` asks again
        of what was stored, and ``answers`` keeps what was asked, so that nothing is asked twice.
        """
        if not self:
            return
        owner = self._woow_owner_value(vals['owner_id']) if 'owner_id' in vals else None
        parent = self._woow_parent_value(vals['parent_id']) if 'parent_id' in vals else None
        for folder in self:
            self._woow_ask_owner(
                (folder.owner_id, folder.parent_id, bool(folder.inherit_access)),
                folder.owner_id if owner is None else owner,
                folder.parent_id if parent is None else parent,
                _stored_flag(vals, 'inherit_access', folder.inherit_access), answers)

    def _woow_ask_owner(self, was, owner, parent, inherit, answers):
        """Ask for the check that is due when a folder gets this owner, parent and access.

        ``was`` is (owner, parent, inherits its access) of the folder before the call, None for a
        folder that is made. A new owner passes ``_woow_check_owner``; the owner who stays is held
        to the ceiling (``_woow_check_owner_ceiling``) when the folder moves or stops inheriting;
        nothing else is asked. One answer is kept for the folders that have the same owner, parent
        and access, in ``answers``, whichever of the checks before and after the write asks first.
        """
        if was is None or owner != was[0]:
            check = self._woow_check_owner
        elif parent != was[1] or (was[2] and not inherit):
            check = self._woow_check_owner_ceiling
        else:
            return
        key = (check.__name__, owner.id, parent.id, inherit)
        if key not in answers:
            check(owner, parent, inherit)
            answers[key] = True

    def _woow_check_stored_state(self, before, answers):
        """Ask the rules of the owner, the parent and the company again of what the records hold.

        Called after a create or a write of a caller who is not the superuser. The checks before the
        call judge the values that came in; this judges the state that the ORM made of them, read
        back from the records, with the same helper functions, and a violation raises and rolls the
        request back. Whatever the shape of a value was (another type, a default of another kind, a
        value that was read in one way and stored in another), no state is left that was not
        checked. ``before`` is the state of each folder before a write (``_woow_state``) and empty
        for a create; ``answers`` holds what the checks before the call asked, so that the usual
        case asks nothing again. The rules: a folder that has no company is made, and the company
        of a folder is changed, only by a document administrator; a folder at the top level needs
        the root right; the folder of an editor takes the owner of its parent and inherits its
        access; any other owner is held to ``_woow_check_owner`` and the ceiling.
        """
        if self.env.su:
            return
        manager = self.env.user.has_group(_MANAGER)
        checks_owner = self.env.user.has_group(_USER)
        for folder in self:
            was = before.get(folder.id)
            owner, parent, inherit = folder.owner_id, folder.parent_id, bool(folder.inherit_access)
            company = folder.company_id
            if not manager and (not company if was is None else company != was[3]):
                raise AccessError(_('Only document administrators can do this.'))
            if not parent and (was is None or was[1]) and ('root',) not in answers:
                self._woow_require_root_right()
                answers[('root',)] = True
            if was is None and parent and not manager and not parent._woow_rank_once(4, answers):
                if not inherit or owner != parent.owner_id:
                    raise AccessError(_('You do not have enough rights on this folder.'))
            elif was is not None or checks_owner:
                self._woow_ask_owner(was and was[:3], owner, parent, inherit, answers)

    @api.model
    def _woow_check_owner(self, owner, parent, inherit_access):
        """What a user needs to become the owner of a folder that has this parent and this access.

        ``owner`` is a ``res.users`` record (nobody needs no check), ``parent`` the folder above
        the one that ``owner`` is for and ``inherit_access`` whether that folder inherits its
        access once the call is done. The superuser is not asked. Anyone else must name a document
        user, as a member row does, and then the ceiling holds (``_woow_check_owner_ceiling``).
        There is no constraint on the field: the folders that the superuser makes take the owner
        that it names, or the superuser as the default, who is rarely a document user.
        """
        if self.env.su or not owner:
            return
        try:
            document_user = owner._is_internal() and owner.has_group(_USER)
        except MissingError:
            document_user = False
        if not document_user:
            raise ValidationError(_('Choose a document user.'))
        self._woow_check_owner_ceiling(owner, parent, inherit_access)

    @api.model
    def _woow_check_owner_ceiling(self, owner, parent, inherit_access):
        """A folder that does not inherit its access gives its owner no more than the parent does.

        The owner has the manage level of the folder, so the owner needs the manage right on the
        parent: as a user or through a group (what ``_woow_has_rank(4)`` finds for the caller,
        found for the owner), or as a document administrator, who needs no grant. The superuser
        and a caller who is a document administrator are not limited. Anyone else is refused with
        the message of the member rows (``folder_access._check_parent_ceiling``). The grants of
        the parent are found with a search under the record rules of the caller, so a caller who
        cannot read the parent gets the answer of an owner who has no right there, and learns
        nothing of the parent. A folder that inherits its access, a folder without a parent and
        nobody as the owner need no check.
        """
        if (self.env.su or inherit_access or not owner or not parent
                or self.env.user.has_group(_MANAGER) or owner.has_group(_MANAGER)):
            return
        if not parent.with_context(active_test=False).search_count(
                [('id', '=', parent.id)] + self._woow_rank_domain(4, owner)):
            raise ValidationError(
                _('A member cannot be granted more access than on the parent folder.'))

    @api.model
    def _woow_require_root_right(self):
        """A folder at the top level needs a document administrator, or the site setting.

        Used where a root folder is created and where a folder is moved to the top level,
        so that both ways to get one answer the same way (M-24).
        """
        if self.env.su or self.env.user.has_group(_MANAGER):
            return
        if not self.env['woow.document.settings']._get_settings().allow_root_create:
            raise AccessError(_('Only document administrators may create a root folder.'))

    @api.model
    def _woow_check_name_free(self, name, parent_id, company_id, exclude=()):
        """M-02 before the per-level unique name index (Q19) would refuse the row.

        The answer is the same whether the folder already using the name is
        readable by the caller or not, and it carries no database detail.
        """
        if not name:
            return
        self.flush_model(['name', 'parent_id', 'company_id', 'active'])
        self.env.cr.execute(  # sql-ok: Q19
            """SELECT 1 FROM woow_document_folder
               WHERE active AND COALESCE(parent_id, 0) = %s AND COALESCE(company_id, 0) = %s
                 AND lower(name) = lower(%s) AND NOT id = ANY(%s)
               LIMIT 1""",
            (parent_id or 0, company_id or 0, name, list(exclude)))
        if self.env.cr.fetchone():
            raise UserError(_('Another folder in this location already uses this name. '
                              'Pick a different name.'))

    def _order_to_sql(self, order, query, alias=None, reverse=False):
        """Order records of another model by folder id for callers who may not read every folder.

        Ordering or grouping a model by a many2one to folders makes core join this
        table and sort by its ``_order``. Record rules do not filter that join, so
        sorting by sequence and name would reveal those of folders the caller cannot
        read. Folder lists themselves (no alias, or this table) keep ``_order``.
        """
        if alias and alias != self._table and not self.env.su \
                and not self.env.user.has_group(_MANAGER):
            order = 'id'
        return super()._order_to_sql(order, query, alias=alias, reverse=reverse)

    def unlink(self):
        """Permanent deletion must follow trash and legal-hold checks."""
        self._require_manager()
        self.check_access('unlink')
        if not self.env.su and any(not folder.in_trash for folder in self):
            raise UserError(_('Move a folder to the trash before deleting it permanently.'))
        if any(folder.subtree_legal_hold for folder in self):
            raise UserError(_('A folder under legal hold cannot be deleted.'))
        for folder in self:
            self.env['woow.document.log']._log_event(
                'purge', channel=self._woow_log_channel(),
                folder_id=folder.id, company_id=folder.company_id.id)
        return super().unlink()

    @api.depends('name', 'parent_id.complete_name')
    def _compute_complete_name(self):
        for folder in self:
            folder.complete_name = ('%s / %s' % (folder.parent_id.complete_name, folder.name)
                                    if folder.parent_id else folder.name)

    @api.depends('active', 'parent_id.in_trash')
    def _compute_in_trash(self):
        """A folder is unavailable when it or an ancestor is in the trash."""
        for folder in self:
            folder.in_trash = not folder.active or bool(folder.parent_id.in_trash)

    @api.depends('inherit_access', 'child_ids.inherit_access', 'child_ids.has_restricted_descendant')
    def _compute_has_restricted_descendant(self):
        """Every child counts, trashed ones included (see ``_compute_subtree_legal_hold``)."""
        for folder in self:
            folder.has_restricted_descendant = (not folder.inherit_access or any(
                child.has_restricted_descendant
                for child in folder.with_context(active_test=False).child_ids))

    @api.depends('legal_hold', 'child_ids.subtree_legal_hold')
    def _compute_subtree_legal_hold(self):
        """A hold anywhere below a folder prevents subtree purge.

        Children trashed on their own count as well. The stored value must not
        depend on the context of the transaction that recomputes it: reading
        ``child_ids`` with the default context drops inactive children, so a hold
        set on a separately trashed folder, or below one, would never reach the
        folders above it.
        """
        for folder in self:
            folder.subtree_legal_hold = bool(folder.legal_hold or any(
                child.subtree_legal_hold
                for child in folder.with_context(active_test=False).child_ids))

    def _woow_recompute_subtree_flags(self):
        """Recompute ``subtree_legal_hold`` and ``has_restricted_descendant`` of every folder.

        Run on each install and update of the module (``init``): rows stored by
        earlier code may have skipped children that were trashed on their own.
        """
        names = ('subtree_legal_hold', 'has_restricted_descendant')
        folders = self.with_context(active_test=False).search([])
        if folders:
            for name in names:
                self.env.add_to_compute(self._fields[name], folders)
            folders.flush_recordset(list(names))

    @api.depends_context('uid')
    def _compute_display_name(self):
        """Only names found by the caller's record rules appear in the path."""
        paths = {folder.id: folding.parse_parent_path(folder.parent_path) for folder in self}
        ids = sorted({node for path in paths.values() for node in path})
        visible = self.sudo(False).search([('id', 'in', ids)]) if ids else self.browse()
        names = {folder.id: folder.name for folder in visible}
        label = _('Restricted folder')
        for folder in self:
            folder.display_name = folding.display_name(paths[folder.id], names, label)

    @api.depends('document_ids', 'document_ids.active', 'child_ids',
                 'access_ids', 'eff_read_user_ids', 'eff_read_group_ids')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_counts(self):
        ids = self.sudo(False).search([('id', 'in', self.ids)]).ids
        documents = self.env['woow.document'].sudo(False)._read_group(
            [('folder_id', 'in', ids)], ['folder_id'], ['__count']) if ids else []
        children = self.sudo(False)._read_group(
            [('parent_id', 'in', ids)], ['parent_id'], ['__count']) if ids else []
        doc_counts = {folder.id: count for folder, count in documents}
        child_counts = {folder.id: count for folder, count in children}
        for folder in self:
            folder.document_count = doc_counts.get(folder.id, 0)
            folder.subfolder_count = child_counts.get(folder.id, 0)

    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_effective_access_lines(self):
        """Show grants from readable sources only to a folder's managers."""
        folders = self.sudo(False)
        readable = set(folders.search([('id', 'in', self.ids)]).ids)
        for folder in folders:
            folder.effective_access_lines = []
            if folder.id not in readable or not folder._woow_has_rank(4):
                continue

            path = folding.parse_parent_path(folder.parent_path)
            if not path:
                path = (folder.id,)
            visible = folders.search([('id', 'in', list(path))])
            visible_by_id = {source.id: source for source in visible}
            # A hidden ancestor may itself be an inheritance boundary. Never
            # attribute grants from above it to the current folder.
            hidden_source = False
            for index in range(len(path) - 1, -1, -1):
                source = visible_by_id.get(path[index])
                if not source:
                    path = path[index + 1:]
                    hidden_source = True
                    break
                if source and not source.inherit_access:
                    path = path[index:]
                    break
            grants = {}
            for source_id in path:
                source = visible_by_id.get(source_id)
                if not source:
                    continue
                for line in self.env['woow.document.folder.access'].sudo(False).search(
                        [('folder_id', '=', source_id)]):
                    subject = line.subject_type
                    if subject not in _SUBJECT_FIELD:
                        continue
                    principal = line[_SUBJECT_FIELD[subject]]
                    if not principal:
                        continue
                    rank = min(_LEVEL_RANK[line.level], 2) if subject == 'partner' else _LEVEL_RANK[line.level]
                    key = (subject, principal.id)
                    if rank >= grants.get(key, {}).get('rank', 0):
                        grants[key] = {'subject_type': subject, 'principal_id': principal.id,
                                       'level': line.level, 'rank': rank,
                                       'source_folder_id': source_id,
                                       'source_folder_name': source.name}
                if source.owner_id:
                    key = ('user', source.owner_id.id)
                    grants[key] = {'subject_type': 'user', 'principal_id': source.owner_id.id,
                                   'level': 'manage', 'rank': 4,
                                   'source_folder_id': source_id,
                                   'source_folder_name': source.name}

            # A principal may be hidden by its own model's ACL or record rules.
            visible_principals = {}
            for subject, model_name in _SUBJECT_MODEL.items():
                ids = [key[1] for key in grants if key[0] == subject]
                model = self.env[model_name].sudo(False)
                if model.check_access_rights('read', raise_exception=False):
                    visible_principals[subject] = {
                        principal.id: principal.name for principal in model.search([('id', 'in', ids)])}
                else:
                    visible_principals[subject] = {}
            lines = []
            for key, grant in sorted(grants.items()):
                name = visible_principals[key[0]].get(key[1])
                if name:
                    lines.append({**grant, 'principal_name': name})
            if hidden_source:
                lines.append({'source': 'inherited', 'source_folder_name':
                              _('Inherited from parent folder')})
            folder.effective_access_lines = lines

    @api.depends_context('uid')
    def _compute_woow_user_rank(self):
        """Rank in the caller's company and record-rule scope."""
        for folder in self:
            folder.woow_user_rank = next(
                (rank for rank in (4, 3, 2, 1) if folder._woow_has_rank(rank)), 0)

    def _compute_woow_has_portal_members(self):
        for folder in self:
            folder.woow_has_portal_members = bool(folder.eff_read_partner_ids)

    @api.model
    def _woow_rank_domain(self, n, user=None):
        """Effective user or group grant at the requested rank, for ``user`` (the caller if none)."""
        level = {1: 'read', 2: 'upload', 3: 'edit', 4: 'manage'}.get(n)
        if level is None:
            raise ValueError('rank must be between 1 and 4')
        user = self.env.user if user is None else user
        return ['|', (f'eff_{level}_user_ids', 'in', [user.id]),
                (f'eff_{level}_group_ids', 'in', user.groups_id.ids)]

    def _woow_filter_rank(self, n):
        """The folders of ``self`` that pass ``_woow_has_rank(n)``, found with one search.

        The superuser keeps the folders that exist, a user who is not internal gets none, and
        anyone else gets the folders that a search under the user's record rules finds: all of
        them for a document administrator, for another user those whose effective grants name
        the user, or a group of the user, at rank ``n`` or above. Its only caller is
        ``_woow_has_rank``, which passes one folder.
        """
        if not self:
            return self
        if self.env.su:
            return self.exists()
        if not self.env.user._is_internal():
            return self.browse()
        domain = [('id', 'in', self.ids)]
        if not self.env.user.has_group(_MANAGER):
            domain += self._woow_rank_domain(n)
        return self.search(domain)

    def _woow_has_rank(self, n):
        """Check the live rule and effective grant without elevating privileges."""
        self.ensure_one()
        return bool(self._woow_filter_rank(n))

    def _woow_require_rank(self, n):
        """AccessError unless every folder in self passes _woow_has_rank(n)."""
        if any(not folder._woow_has_rank(n) for folder in self):
            raise AccessError(_('You do not have enough rights on this folder.'))

    def _woow_portal_can_upload(self):
        """Probe the portal write rule without using write as a mutation."""
        self.ensure_one()
        if self.env.user._is_internal():
            return False
        return bool(self._filtered_access('write'))

    def _woow_breadcrumb(self):
        """Return readable path elements with hidden runs collapsed."""
        self.ensure_one()
        path = folding.parse_parent_path(self.parent_path)
        visible = self.sudo(False).search([('id', 'in', list(path))])
        names = {folder.id: folder.name for folder in visible}
        return folding.breadcrumb(path, names)

    @api.model
    def _woow_portal_roots(self, limit=TREE_QUERY_LIMIT):
        """First readable folders in each visible tree."""
        candidates = self.search([], limit=min(max(int(limit), 0), TREE_QUERY_LIMIT))
        paths = {folder.id: folding.parse_parent_path(folder.parent_path)
                 for folder in candidates}
        return candidates.browse(folding.roots(paths))

    def _woow_portal_children(self, limit=TREE_QUERY_LIMIT):
        """Nearest readable descendants, skipping hidden intermediate folders."""
        self.ensure_one()
        self.check_access('read')
        candidates = self.search([], limit=min(max(int(limit), 0), TREE_QUERY_LIMIT))
        paths = {folder.id: folding.parse_parent_path(folder.parent_path)
                 for folder in candidates}
        return candidates.browse(folding.children_of(self.id, paths))
