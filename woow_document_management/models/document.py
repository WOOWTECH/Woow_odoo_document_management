# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Documents and the common upload validation entry for ORM and HTTP callers."""
import base64
import binascii

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services import url_policy
from .upload_policy import WoowUploadRejected

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
    owner_id = fields.Many2one('res.users', default=lambda self: (
        self.env.user if self.env.user._is_internal() else False), tracking=True,
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
        """Identify the one attachment backing the Binary field."""
        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', self._name), ('res_id', 'in', self.ids),
            ('res_field', '=', 'datas')], order='id')
        by_doc = {}
        for attachment in attachments:
            by_doc.setdefault(attachment.res_id, attachment.id)
        for doc in self:
            doc.attachment_id = by_doc.get(doc.id, False)

    @api.depends('restored_date', 'folder_id.subtree_restored_at')
    def _compute_effective_restored_at(self):
        for doc in self:
            dates = [value for value in (
                doc.restored_date, doc.folder_id.subtree_restored_at) if value]
            doc.effective_restored_at = max(dates) if dates else False

    @api.depends('source_res_model', 'source_res_id')
    def _compute_source_display(self):
        for doc in self:
            doc.source_display = False
            model_name, record_id = doc.source_res_model, doc.source_res_id
            if not model_name or not record_id:
                continue
            model = self.env.registry.get(model_name)
            if not model:
                doc.source_display = _('No access')
                continue
            try:
                source = self.env[model_name]
                if not source.check_access_rights('read', raise_exception=False):
                    doc.source_display = _('No access')
                    continue
                source = source.search([('id', '=', record_id)], limit=1)
                doc.source_display = source.display_name if source else _('No access')
            except AccessError:
                doc.source_display = _('No access')

    def _compute_woow_user_can_write(self):
        for doc in self:
            doc.woow_user_can_write = bool(doc._filtered_access('write'))

    @api.model_create_multi
    def create(self, vals_list):
        """Validate each file before storage; keep multi-file calls atomic."""
        internal = self.env.su or self.env.user._is_internal()
        manager = self.env.su or self.env.user.has_group(_MANAGER)
        portal_fields = {'name', 'folder_id', 'datas', 'description'}
        protected = {'mimetype', 'attachment_id', 'checksum', 'file_size',
                     'uploaded_by_partner_id', 'folder_in_trash', 'company_id'}
        docs = self.browse()
        with self.env.cr.savepoint():
            for incoming in vals_list:
                vals = dict(incoming)
                if not self.env.su and protected.intersection(vals):
                    raise AccessError(_('File metadata is set by the server.'))
                if not internal and set(vals) - portal_fields:
                    raise AccessError(_('This document field is not available to portal users.'))
                if {'source_res_model', 'source_res_id'}.intersection(vals) and not self.env.su \
                        and self.env.context.get(SOURCE_CTX_KEY) is not _SOURCE_SENTINEL:
                    raise AccessError(_('A source can only be set by the attachment wizard.'))
                folder_id = vals.get('folder_id') or self.default_get(['folder_id']).get('folder_id')
                folder = self.env['woow.document.folder'].browse(folder_id).exists()
                if not folder or folder.in_trash:
                    raise AccessError(_('The target folder is unavailable.'))
                if not self.env.su:
                    folder.check_access('read')
                    if internal:
                        folder._woow_require_rank(2)
                    elif not folder._woow_portal_can_upload():
                        raise AccessError(_('You cannot upload to this folder.'))
                vals['folder_id'] = folder.id
                if not internal:
                    vals['uploaded_by_partner_id'] = self.env.user.partner_id.id
                elif 'owner_id' in vals and vals['owner_id'] != self.env.uid and not manager:
                    raise AccessError(_('You cannot assign a different owner.'))
                if {'shared_user_ids', 'shared_partner_ids'}.intersection(vals) and not self.env.su:
                    folder._woow_require_rank(4)
                kind = vals.get('document_type', 'binary')
                if kind == 'binary':
                    if vals.get('url'):
                        raise UserError(_('A file cannot also be a link.'))
                    encoded = vals.get('datas')
                    if not encoded:
                        raise WoowUploadRejected('content')
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, TypeError, ValueError):
                        raise WoowUploadRejected('content') from None
                    checked = self.env['woow.document.upload.policy']._validate(
                        vals.get('name'), data)
                    vals['name'], vals['mimetype'] = checked.name, checked.mimetype
                elif kind == 'url':
                    if vals.get('datas'):
                        raise UserError(_('A link cannot also contain a file.'))
                    try:
                        safe_url = url_policy.validate(vals.get('url'))
                    except ValueError:
                        raise UserError(_('The link address is invalid.')) from None
                    if safe_url.startswith('http://') and not self.env[
                            'woow.document.settings']._get_settings().allow_http_links:
                        raise UserError(_('The link address must use HTTPS.'))
                    vals['url'] = safe_url
                else:
                    raise UserError(_('Unknown document type.'))
                context = ({'tracking_disable': True} if not internal else
                           {'mail_create_nolog': True, 'mail_create_nosubscribe': True})
                created = super(WoowDocument, self.with_context(**context)).create([vals])
                docs |= created
                self.env['woow.document.log']._log_event(
                    'upload', channel='portal' if not internal else self._woow_log_channel(),
                    document_id=created.id, folder_id=folder.id,
                    partner_id=self.env.user.partner_id.id if not internal else None,
                    company_id=folder.company_id.id, detail={'bytes': len(data) if kind == 'binary' else 0})
                # The next policy check must see the previous file in this batch.
                self.env.flush_all()
        return docs

    def write(self, vals):
        """Validate replacements and moves in the caller's record-rule scope."""
        if not vals:
            return super().write(vals)
        internal = self.env.su or self.env.user._is_internal()
        if not internal:
            raise AccessError(_('Portal users cannot modify documents.'))
        if not self.env.su:
            protected = {'mimetype', 'attachment_id', 'checksum', 'file_size',
                         'uploaded_by_partner_id', 'folder_in_trash', 'company_id',
                         'active', 'trashed_date', 'trashed_by_id', 'restored_date',
                         'legal_hold', 'effective_restored_at', 'woow_user_can_write'}
            if protected.intersection(vals):
                raise AccessError(_('File metadata is set by the server.'))
            if {'source_res_model', 'source_res_id'}.intersection(vals) and \
                    self.env.context.get(SOURCE_CTX_KEY) is not _SOURCE_SENTINEL:
                raise AccessError(_('A source can only be set by the attachment wizard.'))
            if 'owner_id' in vals and vals['owner_id'] != self.env.uid and not \
                    self.env.user.has_group(_MANAGER):
                raise AccessError(_('You cannot assign a different owner.'))
        self.check_access('write')
        target = self.env['woow.document.folder'].browse(vals['folder_id']).exists() \
            if vals.get('folder_id') else self.env['woow.document.folder']
        if 'folder_id' in vals and not target:
            raise AccessError(_('The target folder is unavailable.'))
        if target:
            if target.in_trash:
                raise AccessError(_('The target folder is unavailable.'))
            if not self.env.su:
                target.check_access('read')
                target._woow_require_rank(2)
        if 'document_type' in vals and any(
                doc.document_type != vals['document_type'] for doc in self):
            raise UserError(_('Changing the document type is not supported.'))
        result = True
        with self.env.cr.savepoint():
            for doc in self:
                if target and doc.company_id != target.company_id:
                    raise UserError(_('Cannot move a document between companies.'))
                if {'shared_user_ids', 'shared_partner_ids'}.intersection(vals) and not self.env.su:
                    doc.folder_id._woow_require_rank(4)
                update = dict(vals)
                if 'datas' in update:
                    if doc.document_type != 'binary' or not update['datas']:
                        raise WoowUploadRejected('content')
                    try:
                        data = base64.b64decode(update['datas'], validate=True)
                    except (binascii.Error, TypeError, ValueError):
                        raise WoowUploadRejected('content') from None
                    checked = self.env['woow.document.upload.policy']._validate(
                        update.get('name', doc.name), data)
                    update['name'], update['mimetype'] = checked.name, checked.mimetype
                if 'url' in update:
                    if doc.document_type != 'url':
                        raise UserError(_('A file cannot also be a link.'))
                    try:
                        safe_url = url_policy.validate(update['url'])
                    except ValueError:
                        raise UserError(_('The link address is invalid.')) from None
                    if safe_url.startswith('http://') and not self.env[
                            'woow.document.settings']._get_settings().allow_http_links:
                        raise UserError(_('The link address must use HTTPS.'))
                    update['url'] = safe_url
                result = super(WoowDocument, doc).write(update)
                if 'datas' in update or 'folder_id' in update:
                    self.env['woow.document.log']._log_event(
                        'replace' if 'datas' in update else 'move',
                        channel=self._woow_log_channel(), document_id=doc.id,
                        folder_id=(target or doc.folder_id).id,
                        company_id=doc.company_id.id,
                        detail={'bytes': len(data) if 'datas' in update else 0})
                self.env.flush_all()
        return result

    def unlink(self):
        """Permanent deletion is an administrator operation and respects legal hold."""
        self._require_manager()
        self.check_access('unlink')
        if not self.env.su and any(doc.active and not doc.folder_in_trash for doc in self):
            raise UserError(_('Move a document to the trash before deleting it permanently.'))
        if any(doc.legal_hold for doc in self):
            raise UserError(_('A document on legal hold cannot be deleted.'))
        for doc in self:
            self.env['woow.document.log']._log_event(
                'purge', channel=self._woow_log_channel(), document_id=doc.id,
                folder_id=doc.folder_id.id, company_id=doc.company_id.id)
        return super().unlink()
