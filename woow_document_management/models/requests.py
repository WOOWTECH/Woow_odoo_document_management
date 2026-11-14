# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Requested files: a placeholder document that a colleague is asked to fill with a file.

A placeholder is a document of type ``request``. It has no content and is "no file" on every
content path (download, preview, ZIP, stream). The person asked owns it: the existing write rule
of documents (R-D2: the owner of a document in a folder where the user may upload) is what lets
that person store the file, so no right is ever added. The requester needs the same rule while
the activity is scheduled (core asks for write access on the document), so the placeholder is
created in the requester's name and handed over to the person asked, in this order, by the
server, in one transaction.
"""
import base64
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import plaintext2html

from ..services.constants import REQUEST_REMINDER_MIN_HOURS
from ..services.naming import sanitize_filename
from .document import WoowDocument
from .ir_attachment import NO_INDEX_CTX_KEY
from .quota import LEDGER_KEY, QuotaLedger
from .upload_policy import WoowUploadRejected

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_ACTIVITY_TYPE = 'mail.mail_activity_data_upload_document'
_REMINDER_TEMPLATE = 'woow_document_management.mail_template_woow_document_request_reminder'

# Only a context that carries this in-process sentinel may create a placeholder: a client cannot
# send a Python object, so no RPC call can reach the creation path.
REQUEST_CTX_KEY = 'woow_request_create'
_REQUEST_SENTINEL = object()

REQUEST_DAYS_MAX = 365
REQUEST_NOTE_MAX = 500
# The server sets these; a client may neither pass nor change them.
REQUEST_SERVER_FIELDS = frozenset({
    'request_user_id', 'request_deadline', 'request_note', 'request_activity_id',
    'request_last_reminder'})
# What the creation path of a placeholder accepts.
_CREATE_KEYS = frozenset({
    'name', 'document_type', 'folder_id', 'tag_ids', 'request_user_id', 'request_deadline',
    'request_note'})


class WoowDocumentRequest(models.Model):
    _inherit = 'woow.document'

    document_type = fields.Selection(
        selection_add=[('request', 'Requested file')], ondelete={'request': 'cascade'})
    request_user_id = fields.Many2one(
        'res.users', string='Asked of', ondelete='set null', index='btree_not_null',
        copy=False, groups='base.group_user')
    request_deadline = fields.Date(string='Due date', copy=False, groups='base.group_user')
    request_note = fields.Char(string='Request note', size=REQUEST_NOTE_MAX, copy=False,
                               groups='base.group_user')
    request_activity_id = fields.Many2one(
        'mail.activity', string='Request activity', ondelete='set null', copy=False,
        groups='base.group_user')
    # Kept on the activity: the requester does not write the placeholder (the person asked owns
    # it), but wrote the activity and may write it again.
    request_last_reminder = fields.Datetime(
        related='request_activity_id.woow_request_last_reminder', string='Last reminder',
        groups='base.group_user')

    # ------------------------------------------------------------------ guards
    @api.model_create_multi
    def create(self, vals_list):
        """A placeholder is made by ``_woow_open_request`` only; a client sets none of its fields."""
        marked = self.env.context.get(REQUEST_CTX_KEY) is _REQUEST_SENTINEL
        if not marked:
            if not self.env.su and any(REQUEST_SERVER_FIELDS.intersection(vals) for vals in vals_list):
                raise AccessError(_('File metadata is set by the server.'))
            # a value 'request' without the marker is refused by the document type check
            return super().create(vals_list)
        if len(vals_list) != 1 or vals_list[0].get('document_type') != 'request':
            raise UserError(_('Unknown document type.'))
        vals = dict(vals_list[0])
        if set(vals) - _CREATE_KEYS:
            raise AccessError(_('File metadata is set by the server.'))
        internal = self.env.su or self.env.user._is_internal()
        if not internal:
            raise AccessError(_('You cannot upload to this folder.'))
        folder = self.env['woow.document.folder'].browse(vals.get('folder_id')).exists()
        if not folder or folder.in_trash:
            raise AccessError(_('The target folder is unavailable.'))
        if not self.env.su:
            folder.check_access('read')
            folder._woow_require_rank(2)
        vals['folder_id'] = folder.id
        context = {'mail_create_nolog': True, 'mail_create_nosubscribe': True,
                   NO_INDEX_CTX_KEY: True}
        # the checks of ``WoowDocument.create`` that apply to a document without content are
        # the ones above; the guard mixin below it still takes the ambient defaults away
        return super(WoowDocument, self.with_context(**context)).create([vals])

    def write(self, vals):
        """The fields of a request are set by the server only."""
        if vals and not self.env.su and REQUEST_SERVER_FIELDS.intersection(vals):
            raise AccessError(_('File metadata is set by the server.'))
        return super().write(vals)

    # ------------------------------------------------------------------ helpers
    @api.model
    def _woow_user_can_store(self, folder, user):
        """Whether ``user`` may store a file in ``folder`` under the existing rules.

        An active internal document user of the folder's company with upload level (or more)
        in the folder, or a document administrator. Nothing is granted.
        """
        if not user or not user.active or not user._is_internal() or not user.has_group(_USER):
            return False
        if folder.company_id and folder.company_id not in user.company_ids:
            return False
        if user.has_group(_MANAGER):
            return True
        return bool(folder.search([('id', '=', folder.id)] + folder._woow_rank_domain(2, user=user)))

    def _woow_is_pending_request(self):
        self.ensure_one()
        return self.document_type == 'request' and self.active and not self.folder_in_trash


    # ------------------------------------------------------------------ request
    @api.model
    def _woow_open_request(self, folder, name, asked, days, tags=None, note=None):
        """Create a placeholder in ``folder`` and ask ``asked`` for the file; logs ``request_create``.

        The caller needs upload level in the folder. The person asked must be able to store a file
        there under the existing rules (``_woow_user_can_store``); otherwise the request is refused.
        The placeholder is created in the caller's name, the activity is scheduled while the caller
        may write it, and then the placeholder is handed over to the person asked: the owner of a
        document in a folder where the owner may upload can write it (rule R-D2).
        """
        user = self.env.user
        if not self.env.su and not (user._is_internal() and user.has_group(_USER)):
            raise AccessError(_('You cannot request files.'))
        folder = self.env['woow.document.folder'].search([
            ('id', '=', folder.id), ('in_trash', '=', False)], limit=1)
        if not folder:
            raise AccessError(_('The target folder is unavailable.'))
        folder._woow_require_rank(2)
        try:
            clean = sanitize_filename(name)
        except ValueError:
            raise UserError(_('Enter what you need.')) from None
        if not asked:
            raise UserError(_('Choose the person who is asked.'))
        if asked == user:
            raise UserError(_('Ask a colleague, not yourself.'))
        if not self._woow_user_can_store(folder, asked):
            raise UserError(_('This person cannot add files to this folder.'))
        if type(days) is not int or not 1 <= days <= REQUEST_DAYS_MAX:
            raise UserError(_('Choose between 1 and %(max)s days.', max=REQUEST_DAYS_MAX))
        note = str(note or '').strip()
        if len(note) > REQUEST_NOTE_MAX:
            raise UserError(_('The note is too long.'))
        tags = tags if tags is not None else self.env['woow.document.tag']
        if len(self.env['woow.document.tag'].search([('id', 'in', tags.ids)])) != len(tags):
            raise AccessError(_('One or more tags are unavailable.'))
        deadline = fields.Date.context_today(self) + timedelta(days=days)
        documents = self.env['woow.document']
        doc = documents.with_context(**{REQUEST_CTX_KEY: _REQUEST_SENTINEL}).create({
            'name': clean, 'document_type': 'request', 'folder_id': folder.id,
            'tag_ids': [(6, 0, tags.ids)], 'request_user_id': asked.id,
            'request_deadline': deadline, 'request_note': note or False})
        doc = documents.browse(doc.id)      # without the marker
        with self.env.cr.savepoint():
            activity = doc.activity_schedule(
                _ACTIVITY_TYPE, date_deadline=deadline, summary=clean,
                note=plaintext2html(note) if note else False, user_id=asked.id)
            # the person asked becomes the owner: from here only that person (and an editor of
            # the folder) writes the placeholder
            super(WoowDocument, doc.with_context(tracking_disable=True)).write({
                'owner_id': asked.id, 'request_activity_id': activity.id})
        self.env['woow.document.log']._log_event(
            'request_create', channel=doc._woow_log_channel(), document_id=doc.id,
            folder_id=folder.id, company_id=folder.company_id.id, detail={'days': days})
        return doc

    def action_open_fulfil_wizard(self):
        """Open the wizard that takes the requested file."""
        self.check_access('read')
        self.ensure_one()
        if not self._woow_is_pending_request():
            raise UserError(_('This file is not requested any more.'))
        if not self._filtered_access('write'):
            raise AccessError(_('You cannot upload to this request.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Upload the requested file'),
            'res_model': 'woow.document.request.fulfil.wizard', 'view_mode': 'form',
            'target': 'new', 'context': {'default_document_id': self.id},
        }

    def _woow_fulfil_request(self, filename, data):
        """Store ``data`` on the placeholder; it becomes a document of type file.

        Allowed for whoever may write the placeholder (the person asked, through the owner rule,
        or an editor of the folder). The file passes the upload policy and the quota like any
        upload; the activity is marked done, a note is posted and ``request_done`` is logged.
        """
        self.ensure_one()
        if not self.env.su and not (self.env.user._is_internal() and self._filtered_access('write')):
            raise AccessError(_('You cannot upload to this request.'))
        if not self._woow_is_pending_request():
            raise UserError(_('This file is not requested any more.'))
        # the content is stored below with a write that skips ``write`` of the lock extension, so
        # the lock of another user is judged here (the superuser and the lock holder pass)
        self._woow_refuse_foreign_lock('write')
        policy = self.env['woow.document.upload.policy'].with_context(
            **{LEDGER_KEY: QuotaLedger()})
        stem, _dot, extension = policy._safe_name(filename).rpartition('.')
        if not stem or not extension:
            raise WoowUploadRejected('extension', ext='')
        # the requested name is kept; the file's own extension decides the type
        name = self.name
        if not name.lower().endswith('.' + extension.lower()):
            name = '%s.%s' % (name, extension)
        with self.env.cr.savepoint():
            checked = policy._validate(name, data)
            activity = self.request_activity_id
            super(WoowDocument, self.with_context(tracking_disable=True, **{
                NO_INDEX_CTX_KEY: True})).write({
                    'document_type': 'binary', 'datas': base64.b64encode(data),
                    'name': checked.name, 'mimetype': checked.mimetype,
                    'request_activity_id': False})
            note = _('The requested file was uploaded.')
            if activity.exists():
                activity.action_feedback(feedback=note)
            else:
                self.message_post(body=note, subtype_xmlid='mail.mt_note')
            self.env['woow.document.log']._log_event(
                'request_done', channel=self._woow_log_channel(), document_id=self.id,
                folder_id=self.folder_id.id, company_id=self.company_id.id,
                detail={'bytes': len(data)})
            self.env.flush_all()
        return True

    def action_send_reminder(self):
        """Notify the person asked again (the requester or a folder manager); once per period.

        The time of the last reminder is kept on the activity of the request. Cancelling a request
        is trashing the placeholder (``action_trash``): core removes the activities of an archived
        record, so the activity of the request goes with it.
        """
        self.check_access('read')
        self.ensure_one()
        if not self._woow_is_pending_request():
            raise UserError(_('This file is not requested any more.'))
        if self.create_uid != self.env.user and not self.folder_id._woow_has_rank(4):
            raise AccessError(_('Only the requester or a folder manager can send a reminder.'))
        activity = self.request_activity_id
        if not activity.exists():
            raise UserError(_('This request has no open task any more.'))
        asked = self.request_user_id
        if not asked or not asked.partner_id.email:
            raise UserError(_('The person asked cannot be reached by email.'))
        if not self._woow_user_can_store(self.folder_id, asked):
            raise UserError(_('This person cannot add files to this folder.'))
        sender = self.env.user.email_formatted or self.company_id.email
        if not sender:
            raise UserError(_('Set a sender email address before sending a reminder.'))
        now = fields.Datetime.now()
        last = activity.woow_request_last_reminder
        if last and now - last < timedelta(hours=REQUEST_REMINDER_MIN_HOURS):
            raise UserError(_('A reminder was sent recently. Try again later.'))
        base = self.env['woow.document.notify.wizard'].new(
            {'folder_id': self.folder_id.id})._base_url()
        # the limit is stored first: a second reminder that runs at the same time fails here
        activity.write({'woow_request_last_reminder': now})
        self.env.flush_all()
        template = self.env.ref(_REMINDER_TEMPLATE)
        template.with_context(
            woow_notice_url='%s/web#id=%d&model=woow.document&view_type=form' % (base, self.id)
        ).send_mail(self.id, force_send=False, email_values={
            'recipient_ids': [(6, 0, asked.partner_id.ids)], 'email_to': False,
            'email_from': sender, 'auto_delete': True})
        return True


class MailActivityRequest(models.Model):
    _inherit = 'mail.activity'

    woow_request_last_reminder = fields.Datetime(copy=False)
