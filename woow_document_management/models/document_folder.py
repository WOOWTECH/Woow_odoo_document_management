# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Folders (IF-MODEL, owned by core-models-acl).

Day-0 stub: fields follow arch section 3.1 and 3.3; every computed permission is empty, every
rank probe is False and names are folded (fail closed).
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..services.constants import TREE_QUERY_LIMIT
from ..services.folding import ELLIPSIS

_MANAGER = 'woow_document_management.group_woow_document_manager'
_EFF_DEPENDS = (
    'inherit_access', 'owner_id', 'parent_id',
    'parent_id.eff_read_user_ids', 'parent_id.eff_upload_user_ids',
    'parent_id.eff_edit_user_ids', 'parent_id.eff_manage_user_ids',
    'parent_id.eff_read_group_ids', 'parent_id.eff_upload_group_ids',
    'parent_id.eff_edit_group_ids', 'parent_id.eff_manage_group_ids',
    'parent_id.eff_read_partner_ids', 'parent_id.eff_upload_partner_ids',
    'access_ids.level', 'access_ids.subject_type', 'access_ids.user_id',
    'access_ids.group_id', 'access_ids.partner_id',
)


def _eff(comodel, relation, column):
    return fields.Many2many(
        comodel, relation, 'folder_id', column, compute='_compute_effective', store=True,
        recursive=True, groups='base.group_user')


class WoowDocumentFolder(models.Model):
    _name = 'woow.document.folder'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'woow.document.guard.mixin']
    _description = 'Document folder'
    _parent_store = True
    _order = 'sequence, name, id'
    _check_company_auto = True
    _rec_names_search = ['name']
    _woow_allowed_defaults = frozenset({'default_parent_id'})

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

    eff_read_user_ids = _eff('res.users', 'woow_doc_folder_eff_r_user_rel', 'user_id')
    eff_upload_user_ids = _eff('res.users', 'woow_doc_folder_eff_u_user_rel', 'user_id')
    eff_edit_user_ids = _eff('res.users', 'woow_doc_folder_eff_e_user_rel', 'user_id')
    eff_manage_user_ids = _eff('res.users', 'woow_doc_folder_eff_m_user_rel', 'user_id')
    eff_read_group_ids = _eff('res.groups', 'woow_doc_folder_eff_r_group_rel', 'group_id')
    eff_upload_group_ids = _eff('res.groups', 'woow_doc_folder_eff_u_group_rel', 'group_id')
    eff_edit_group_ids = _eff('res.groups', 'woow_doc_folder_eff_e_group_rel', 'group_id')
    eff_manage_group_ids = _eff('res.groups', 'woow_doc_folder_eff_m_group_rel', 'group_id')
    eff_read_partner_ids = _eff('res.partner', 'woow_doc_folder_eff_r_partner_rel', 'partner_id')
    eff_upload_partner_ids = _eff('res.partner', 'woow_doc_folder_eff_u_partner_rel', 'partner_id')

    document_properties_definition = fields.PropertiesDefinition(groups='base.group_user')
    document_ids = fields.One2many('woow.document', 'folder_id')
    document_count = fields.Integer(compute='_compute_counts')
    subfolder_count = fields.Integer(compute='_compute_counts')
    effective_access_lines = fields.Json(compute='_compute_effective_access_lines')

    active = fields.Boolean(default=True)
    in_trash = fields.Boolean(compute='_compute_in_trash', store=True, recursive=True, index=True)
    trashed_date = fields.Datetime(groups='base.group_user')
    trashed_by_id = fields.Many2one('res.users', ondelete='set null', groups='base.group_user')
    restored_date = fields.Datetime(groups='base.group_user')
    subtree_restored_at = fields.Datetime(compute='_compute_subtree_restored_at', store=True,
                                          recursive=True, groups='base.group_user')
    has_restricted_descendant = fields.Boolean(compute='_compute_has_restricted_descendant',
                                               store=True, recursive=True, groups='base.group_user')
    legal_hold = fields.Boolean(groups=_MANAGER)
    subtree_legal_hold = fields.Boolean(compute='_compute_subtree_legal_hold', store=True,
                                        recursive=True, groups=_MANAGER)

    woow_user_rank = fields.Integer(compute='_compute_woow_user_rank')
    woow_has_portal_members = fields.Boolean(compute='_compute_woow_has_portal_members')

    @api.depends(*_EFF_DEPENDS)
    def _compute_effective(self):
        """Effective permission sets. Day-0 stub: empty (fail closed)."""
        for folder in self:  # WOOW-STUB
            folder.eff_read_user_ids = folder.eff_upload_user_ids = False
            folder.eff_edit_user_ids = folder.eff_manage_user_ids = False
            folder.eff_read_group_ids = folder.eff_upload_group_ids = False
            folder.eff_edit_group_ids = folder.eff_manage_group_ids = False
            folder.eff_read_partner_ids = folder.eff_upload_partner_ids = False

    @api.depends('name', 'parent_id.complete_name')
    def _compute_complete_name(self):
        for folder in self:  # WOOW-STUB
            folder.complete_name = False

    @api.depends('active', 'parent_id.in_trash')
    def _compute_in_trash(self):
        """Day-0 stub: every folder counts as trashed (fail closed)."""
        for folder in self:  # WOOW-STUB
            folder.in_trash = True

    @api.depends('restored_date', 'parent_id.subtree_restored_at')
    def _compute_subtree_restored_at(self):
        for folder in self:  # WOOW-STUB
            folder.subtree_restored_at = False

    @api.depends('inherit_access', 'child_ids.inherit_access', 'child_ids.has_restricted_descendant')
    def _compute_has_restricted_descendant(self):
        """Day-0 stub: True, so only administrators may trash a subtree (fail closed)."""
        for folder in self:  # WOOW-STUB
            folder.has_restricted_descendant = True

    @api.depends('legal_hold', 'child_ids.subtree_legal_hold')
    def _compute_subtree_legal_hold(self):
        """Day-0 stub: True, so nothing is purged (fail closed)."""
        for folder in self:  # WOOW-STUB
            folder.subtree_legal_hold = True

    @api.depends_context('uid')
    def _compute_display_name(self):
        """Folded name (I-08). Day-0 stub: always the restricted label."""
        label = _('Restricted folder')
        for folder in self:  # WOOW-STUB
            folder.display_name = label

    def _compute_counts(self):
        for folder in self:  # WOOW-STUB
            folder.document_count = 0
            folder.subfolder_count = 0

    def _compute_effective_access_lines(self):
        for folder in self:  # WOOW-STUB
            folder.effective_access_lines = []

    @api.depends_context('uid')
    def _compute_woow_user_rank(self):
        """0..4 for the current user. Day-0 stub: 0."""
        for folder in self:  # WOOW-STUB
            folder.woow_user_rank = 0

    def _compute_woow_has_portal_members(self):
        for folder in self:  # WOOW-STUB
            folder.woow_has_portal_members = False

    @api.model
    def _woow_rank_domain(self, n):
        """E(n) of arch section 4.4. Day-0 stub: matches nothing."""
        return [(0, '=', 1)]  # WOOW-STUB

    def _woow_has_rank(self, n):
        """True when the current user has rank >= n on this folder. Day-0 stub: False."""
        return False  # WOOW-STUB

    def _woow_require_rank(self, n):
        """AccessError unless every folder in self passes _woow_has_rank(n)."""
        if not self.env.su:
            raise AccessError(_('You do not have enough rights on this folder.'))  # WOOW-STUB

    def _woow_portal_can_upload(self):
        """Portal upload probe (rule R-F6). Day-0 stub: False."""
        return False  # WOOW-STUB

    def _woow_breadcrumb(self):
        """Folded breadcrumb of the folder. Day-0 stub: one ellipsis."""
        return [(None, ELLIPSIS)]  # WOOW-STUB

    @api.model
    def _woow_portal_roots(self, limit=TREE_QUERY_LIMIT):
        """Readable root folders for the portal. Day-0 stub: none."""
        return self.browse()  # WOOW-STUB

    def _woow_portal_children(self, limit=TREE_QUERY_LIMIT):
        """Readable child folders for the portal. Day-0 stub: none."""
        return self.browse()  # WOOW-STUB
