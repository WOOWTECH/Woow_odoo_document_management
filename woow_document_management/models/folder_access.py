# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Folder members (IF-MODEL, owned by core-models-acl)."""
from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, ValidationError

from ..services.locks import advisory_key, lock
from .document_folder import _company_value

_LEVEL_RANK = {'read': 1, 'upload': 2, 'edit': 3, 'manage': 4}
MAX_ACCESS_LINES = 500
_PARENT_FIELDS = {
    'user': ('user_id', ('eff_read_user_ids', 'eff_upload_user_ids',
                         'eff_edit_user_ids', 'eff_manage_user_ids')),
    'group': ('group_id', ('eff_read_group_ids', 'eff_upload_group_ids',
                           'eff_edit_group_ids', 'eff_manage_group_ids')),
    'partner': ('partner_id', ('eff_read_partner_ids', 'eff_upload_partner_ids')),
}


class WoowDocumentFolderAccess(models.Model):
    _name = 'woow.document.folder.access'
    _inherit = ['woow.document.guard.mixin']
    _description = 'Document folder member'
    _woow_allowed_defaults = frozenset({'default_folder_id'})
    _sql_constraints = [
        ('subject_exactly_one', "CHECK ((subject_type = 'user' AND user_id IS NOT NULL "
         "AND group_id IS NULL AND partner_id IS NULL) OR (subject_type = 'group' "
         "AND user_id IS NULL AND group_id IS NOT NULL AND partner_id IS NULL) "
         "OR (subject_type = 'partner' AND user_id IS NULL AND group_id IS NULL "
         "AND partner_id IS NOT NULL))", 'Select exactly one member of the chosen type.'),
        ('portal_max_upload', "CHECK (subject_type <> 'partner' OR level IN ('read', 'upload'))",
         'Portal contacts may only view or upload.'),
    ]

    folder_id = fields.Many2one('woow.document.folder', required=True, index=True,
                                ondelete='cascade')
    company_id = fields.Many2one(related='folder_id.company_id', store=True, index=True)
    subject_type = fields.Selection(
        [('user', 'User'), ('group', 'Group'), ('partner', 'Portal contact')], required=True)
    user_id = fields.Many2one('res.users', ondelete='cascade')
    group_id = fields.Many2one('res.groups', ondelete='cascade')
    partner_id = fields.Many2one('res.partner', ondelete='cascade')
    level = fields.Selection(
        [('read', 'View'), ('upload', 'Upload'), ('edit', 'Edit'), ('manage', 'Manage')],
        required=True)

    def _lock_folders(self, folder_ids):
        for folder_id in sorted(set(folder_ids), key=lambda value: advisory_key(f'access:{value}')):
            lock(self.env.cr, f'access:{folder_id}')
            # REPEATABLE READ keeps an old snapshot after the advisory wait. This row
            # update forces a serialization failure if another transaction changed
            # the same folder since that snapshot, so Odoo retries the whole request.
            self.env.cr.execute(  # sql-ok: Q8
                'UPDATE woow_document_folder SET id = id WHERE id = %s', (folder_id,))

    @api.model_create_multi
    def create(self, vals_list):
        default_folder_id = (self.default_get(['folder_id']).get('folder_id')
                             if any('folder_id' not in vals for vals in vals_list) else None)
        folder_ids = [vals.get('folder_id', default_folder_id)
                      for vals in vals_list if vals.get('folder_id', default_folder_id)]
        self._lock_folders(folder_ids)
        if not self.env.su:
            self.env['woow.document.folder'].browse(folder_ids)._woow_require_rank(4)
        lines = super().create(vals_list)
        for line in lines:
            self.env['woow.document.log']._log_event(
                'access_change', channel=self._woow_log_channel(),
                folder_id=line.folder_id.id, company_id=line.company_id.id,
                detail={'member_id': line.id})
        return lines

    def _members_by_folder(self):
        """{folder: ids of the lines in self that belong to it}, in a stable order."""
        members = {}
        for line in self:
            members.setdefault(line.folder_id, []).append(line.id)
        return members

    def _log_member_change(self, members):
        """One access_change row per folder, naming only that folder's lines."""
        for folder, line_ids in members.items():
            self.env['woow.document.log']._log_event(
                'access_change', channel=self._woow_log_channel(),
                folder_id=folder.id, company_id=folder.company_id.id,
                detail={'member_ids': sorted(set(line_ids))})

    def write(self, vals):
        if vals.get('folder_id'):
            self._lock_folders([vals['folder_id']])
            if not self.env.su:
                self.env['woow.document.folder'].browse(vals['folder_id'])._woow_require_rank(4)
        if 'company_id' in vals and not self.env.su:
            # The company of a row is the one of its folder (the new one, when the row moves):
            # a stored value of another company would take the row out of the company rule of
            # the administrators of that folder.
            company = _company_value(vals['company_id'])
            target = self.env['woow.document.folder'].browse(vals.get('folder_id'))
            if any(company != ((target or line.folder_id).company_id.id or False)
                   for line in self):
                raise AccessError(_('This folder field cannot be changed directly.'))
        before = self._members_by_folder() if vals else {}
        result = super().write(vals)
        if vals:
            # A moved line is named on the folder it left and on the one it joined.
            members = {folder: list(line_ids) for folder, line_ids in before.items()}
            for folder, line_ids in self._members_by_folder().items():
                members.setdefault(folder, []).extend(line_ids)
            self._log_member_change(members)
        return result

    def unlink(self):
        members = self._members_by_folder()
        result = super().unlink()
        self._log_member_change(members)
        return result

    def init(self):
        super().init()
        tools.create_unique_index(
            self.env.cr, 'woow_doc_folder_access_subject_uniq', self._table,
            ['folder_id', 'subject_type', 'COALESCE(user_id,0)',
             'COALESCE(group_id,0)', 'COALESCE(partner_id,0)'])

    @api.constrains('subject_type', 'user_id', 'group_id', 'partner_id', 'level', 'folder_id')
    def _check_member(self):
        for line in self:
            if line.subject_type == 'user':
                if not line.user_id._is_internal() or not line.user_id.has_group(
                        'woow_document_management.group_woow_document_user'):
                    raise ValidationError(_('Choose a document user.'))
            elif line.subject_type == 'group':
                if line.group_id.share:
                    raise ValidationError(_('Choose an internal group.'))
            elif line.subject_type == 'partner':
                if line.level not in ('read', 'upload') or not any(
                        user.active and user._is_portal() for user in line.partner_id.user_ids):
                    raise ValidationError(_('Choose a contact with an active portal account.'))
            if not line.folder_id.inherit_access:
                line._check_parent_ceiling(line.folder_id.parent_id)
        for folder_id in self.mapped('folder_id').ids:
            self.env.cr.execute(  # sql-ok: Q7
                'SELECT count(*) FROM woow_document_folder_access WHERE folder_id = %s',
                (folder_id,))
            count = self.env.cr.fetchone()[0]
            if count > MAX_ACCESS_LINES:
                raise ValidationError(_('A folder cannot have more than 500 members.'))

    def _check_parent_ceiling(self, parent):
        if (self.env.su or self.env.user.has_group(
                'woow_document_management.group_woow_document_manager') or not parent):
            return
        for line in self:
            field_name, thresholds = _PARENT_FIELDS[line.subject_type]
            principal = line[field_name]
            parent_rank = max((rank for rank, effective_field in enumerate(
                thresholds, start=1) if principal in parent[effective_field]), default=0)
            if _LEVEL_RANK[line.level] > parent_rank:
                raise ValidationError(_('A member cannot be granted more access than on the parent folder.'))
