# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The data of the details panel of a document (release 18.0.2, lane ``inspector``).

The panel shows one document and lets its user act on it without leaving the screen. Everything it
shows comes from this one read, made in the environment of the caller: a record that the caller's
rules do not find is never named, a field the caller may not read is never returned, and nothing is
read as the superuser. The panel adds no right. Every button of the panel calls an existing server
method that judges the access itself; the two methods added here only describe and open.
"""
from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.constants import SHARE_ROWS_PER_DOCUMENT_MAX

_MANAGER = 'woow_document_management.group_woow_document_manager'
# The most tags a panel lists (a document carries a handful; the cap bounds an odd row).
_TAGS_MAX = 100


class WoowDocumentInspector(models.Model):
    _inherit = 'woow.document'

    # -- helpers --------------------------------------------------------------------------------
    def _woow_inspector_label(self, record):
        """The name of ``record`` when the caller's own rules find it, else False."""
        if not record:
            return False
        found = record.sudo(False)._filtered_access('read')
        return found.display_name if found else False

    def _woow_inspector_person(self, record):
        """``{'id', 'name'}`` of a person or a company that the caller may read, else False."""
        label = self._woow_inspector_label(record)
        return {'id': record.id, 'name': label} if label else False

    def _woow_readable_source(self):
        """The record this document came from, when it exists and the caller may read it.

        The source of a document is set by the server only; it is still treated as data: the model
        must be a real one, and the record is found and read in the caller's environment.
        """
        self.ensure_one()
        model_name, record_id = self.source_res_model, self.source_res_id
        if not model_name or not record_id:
            return None
        model = self.env.get(model_name)
        if model is None or model._abstract or model._transient:
            return None
        if not model.has_access('read'):
            return None
        record = model.browse(record_id).exists()
        if not record or not record.has_access('read'):
            return None
        return record

    def _woow_inspector_share_rows(self):
        """The people the document is shared with, as the caller's rules let them see the rows."""
        shares = self.env['woow.document.share'].search(
            [('document_id', '=', self.id)], limit=SHARE_ROWS_PER_DOCUMENT_MAX)
        roles = dict(shares._fields['role']._description_selection(self.env))
        rows = []
        for share in shares:
            if share.subject_type == 'user':
                label = self._woow_inspector_label(share.user_id)
            else:
                label = self._woow_inspector_label(share.partner_id)
            rows.append({
                'id': share.id, 'kind': share.subject_type, 'name': label or _('Restricted'),
                'role': share.role, 'role_label': roles.get(share.role, share.role),
                'expiry_date': fields.Date.to_string(share.expiry_date) or False})
        return rows

    # -- the panel ------------------------------------------------------------------------------
    def _woow_inspector_open(self):
        """Judge a call of the panel: one document the caller may read, found in their own rules.

        A document that is missing and one that the caller may not read answer the same. The call
        is for internal users; a portal user is refused before anything is read.
        """
        self.ensure_one()
        if not self.with_context(active_test=False).exists()._filtered_access('read'):
            raise AccessError(_('This document is unavailable.'))

    def woow_inspector_data(self):
        """What the details panel shows for this document, read as the caller.

        Needs an internal user who may read the document (the panel is not offered to portal
        users). Names of other records (owner, contact, company, people shared with, the user who
        locked) are given only when the caller's rules find those records.
        """
        if not self.env.user.has_group('base.group_user'):
            raise AccessError(_('The details are for internal users only.'))
        self._woow_inspector_open()
        doc = self
        writable = bool(doc._filtered_access('write'))
        live = bool(doc.active and not doc.folder_in_trash)
        folder = doc.folder_id
        folder_readable = bool(folder._filtered_access('read'))
        uid = self.env.uid
        types = dict(doc._fields['document_type']._description_selection(self.env))
        lock_user = doc.lock_uid
        holder_is_me = bool(lock_user) and lock_user.id == uid
        administrator = self.env.user.has_group(_MANAGER)
        can_unlock = bool(lock_user) and (
            holder_is_me or administrator or (folder_readable and folder._woow_has_rank(4)))
        data = {
            'id': doc.id,
            'name': doc.name,
            'document_type': doc.document_type,
            'type_label': types.get(doc.document_type, doc.document_type),
            'icon': doc.woow_icon,
            'mimetype': doc.mimetype or False,
            'file_size': doc.file_size if doc.document_type == 'binary' else 0,
            'url': (doc.url or False) if doc.document_type == 'url' else False,
            'active': doc.active,
            'in_trash': bool(doc.folder_in_trash),
            'write_date': fields.Datetime.to_string(doc.write_date) or False,
            'folder': {'id': folder.id, 'name': folder.display_name,
                       'selectable': folder_readable},
            'owner': self._woow_inspector_person(doc.owner_id),
            'contact': self._woow_inspector_person(doc.partner_id),
            'company': self._woow_inspector_person(doc.company_id),
            'tags': [{'id': tag.id, 'name': tag.name, 'color': tag.color}
                     for tag in doc.tag_ids[:_TAGS_MAX]],
            'can_write': writable,
            'is_favorite': bool(doc.is_favorite),
            'is_locked': bool(lock_user),
            'locked_by_me': holder_is_me,
            'locked_by': (self._woow_inspector_label(lock_user) or _('Restricted'))
            if lock_user and not holder_is_me else False,
            'can_lock': bool(writable and live and doc.document_type in ('binary', 'url')
                             and (not lock_user or holder_is_me)),
            'can_unlock': can_unlock,
            'version_count': doc.version_count if doc.document_type == 'binary' else 0,
            'has_portal_members': bool(doc.woow_has_portal_members),
            'can_share': bool(doc.woow_user_can_share),
            'shares': doc._woow_inspector_share_rows(),
            'source': False,
            'shortcut_target': False,
            'request': False,
        }
        source = doc._woow_readable_source()
        if source is not None:
            data['source'] = {'model': source._description or source._name,
                              'name': source.display_name}
        if doc.document_type == 'shortcut' and doc.shortcut_target_id.id in \
                doc._woow_visible_target_ids():
            data['shortcut_target'] = {'id': doc.shortcut_target_id.id,
                                       'name': doc.shortcut_target_id.display_name}
        if doc.document_type == 'request':
            deadline = doc.request_deadline
            data['request'] = {
                'asked': self._woow_inspector_person(doc.request_user_id),
                'deadline': fields.Date.to_string(deadline) or False,
                'overdue': bool(deadline and deadline < fields.Date.context_today(doc)),
                'note': doc.request_note or False,
                'can_upload': bool(doc._woow_is_pending_request() and writable)}
        return data

    def action_open_source(self):
        """Open the record this document came from, when the caller may read it."""
        if not self.env.user.has_group('base.group_user'):
            raise AccessError(_('The details are for internal users only.'))
        self._woow_inspector_open()
        record = self._woow_readable_source()
        if record is None:
            raise UserError(_('The source record is not available to you.'))
        return {'type': 'ir.actions.act_window', 'res_model': record._name,
                'res_id': record.id, 'view_mode': 'form', 'views': [(False, 'form')],
                'target': 'current'}

    def action_open_inspector(self):
        """Show the details panel of this document on a page of its own."""
        if not self.env.user.has_group('base.group_user'):
            raise AccessError(_('The details are for internal users only.'))
        self._woow_inspector_open()
        return {'type': 'ir.actions.client', 'tag': 'woow_document_inspector',
                'name': _('Details'), 'params': {'res_id': self.id}, 'target': 'current'}
