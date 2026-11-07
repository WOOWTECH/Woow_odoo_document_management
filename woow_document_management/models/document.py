# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Documents (IF-MODEL, owned by core-models-acl).

Day-0 stub: fields follow arch section 3.4 (without policy_flag, ER-36); computed values fail closed.
"""
from odoo import api, fields, models

_MANAGER = 'woow_document_management.group_woow_document_manager'

# Only a context carrying this module-level sentinel may write source_res_model / source_res_id.
SOURCE_CTX_KEY = 'woow_doc_source'
_SOURCE_SENTINEL = object()


class WoowDocument(models.Model):
    _name = 'woow.document'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'woow.document.guard.mixin']
    _description = 'Document'
    _order = 'create_date desc, id desc'
    _check_company_auto = True
    _woow_allowed_defaults = frozenset({'default_folder_id'})

    name = fields.Char(required=True, tracking=True, index='trigram')
    document_type = fields.Selection([('binary', 'File'), ('url', 'Link')], required=True,
                                     default='binary')
    datas = fields.Binary(attachment=True)
    attachment_id = fields.Many2one('ir.attachment', compute='_compute_attachment_id', store=True,
                                    readonly=True, ondelete='restrict', groups='base.group_system')
    url = fields.Char(size=2048)
    mimetype = fields.Char()
    file_size = fields.Integer(related='attachment_id.file_size', store=True)
    checksum = fields.Char(related='attachment_id.checksum', store=True, index=True,
                           groups='base.group_user')
    folder_id = fields.Many2one('woow.document.folder', required=True, index=True,
                                check_company=True, tracking=True, ondelete='restrict')
    company_id = fields.Many2one(related='folder_id.company_id', store=True, index=True)
    folder_in_trash = fields.Boolean(related='folder_id.in_trash', store=True, index=True)
    effective_restored_at = fields.Datetime(compute='_compute_effective_restored_at', store=True,
                                            groups='base.group_user')
    owner_id = fields.Many2one('res.users', default=lambda self: self.env.user, tracking=True,
                               ondelete='set null', groups='base.group_user')
    uploaded_by_partner_id = fields.Many2one('res.partner', ondelete='set null')
    partner_id = fields.Many2one('res.partner', ondelete='set null', groups='base.group_user')
    tag_ids = fields.Many2many('woow.document.tag', 'woow_document_tag_rel', 'document_id',
                               'tag_id', groups='base.group_user')
    description = fields.Html(sanitize=True)
    properties = fields.Properties(definition='folder_id.document_properties_definition',
                                   groups='base.group_user')
    shared_user_ids = fields.Many2many('res.users', 'woow_document_shared_user_rel', 'document_id',
                                       'user_id', readonly=True, groups='base.group_user')
    shared_partner_ids = fields.Many2many('res.partner', 'woow_document_shared_partner_rel',
                                          'document_id', 'partner_id', readonly=True,
                                          groups='base.group_user')
    source_res_model = fields.Char(groups='base.group_user')
    source_res_id = fields.Many2oneReference(model_field='source_res_model',
                                             groups='base.group_user')
    source_display = fields.Char(compute='_compute_source_display', groups='base.group_user')
    active = fields.Boolean(default=True)
    trashed_date = fields.Datetime(groups='base.group_user')
    trashed_by_id = fields.Many2one('res.users', ondelete='set null', groups='base.group_user')
    restored_date = fields.Datetime(groups='base.group_user')
    legal_hold = fields.Boolean(groups=_MANAGER)
    woow_user_can_write = fields.Boolean(compute='_compute_woow_user_can_write')

    @api.depends('datas')
    def _compute_attachment_id(self):
        """Primary field attachment (res_field='datas'). Day-0 stub: unset."""
        for doc in self:  # WOOW-STUB
            doc.attachment_id = False

    @api.depends('restored_date', 'folder_id.subtree_restored_at')
    def _compute_effective_restored_at(self):
        for doc in self:  # WOOW-STUB
            doc.effective_restored_at = False

    def _compute_source_display(self):
        for doc in self:  # WOOW-STUB
            doc.source_display = False

    def _compute_woow_user_can_write(self):
        """Whether the write rule holds for the current user. Day-0 stub: False."""
        for doc in self:  # WOOW-STUB
            doc.woow_user_can_write = False
