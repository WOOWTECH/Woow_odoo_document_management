# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Folder mailboxes: files that colleagues e-mail to a folder become documents in it.

A document administrator gives a folder an e-mail address (``woow.document.mail.intake``, one row
per folder, built on core's alias mixin). The alias accepts mail only from the followers of the
mailbox row (alias contact ``followers``, core's own check). The mail gateway then calls
``message_new`` of the mailbox model, which stores each attachment as a document, and the files are
stored only when the sender is, at that moment, an active internal document user with upload
level or more on the folder.

Whose rights the files are stored with. The gateway runs as the superuser, and ``create`` of a
document skips every folder check for the superuser. ``_woow_sender_documents`` therefore returns the
document model in the environment of the sender (the one ``with_user`` of this file, registered as
S4 in ``tools/check_sudo.py``): the sender's own rights decide the upload level of the folder, the
tags, the company rules, the upload policy and the quota, exactly as for an upload made in the
interface. Nothing else in this file is elevated, and the environment of the sender is never
used to read the mail or the mailbox.

What stays of a mail. Core posts the whole mail (body, sender, recipients, files) on the record that
``message_new`` returns. ``message_new`` returns the mailbox with an in-process marker
(``MailInNote``, an object that no RPC caller can build); ``message_post`` of the mailbox, seeing
the marker, writes one short internal note with the counts and nothing else. Nothing of the mail is
kept on the mailbox thread: no file, no body, no sender or recipient. Only the Message-Id header is
written on the note, because core's own duplicate check reads it and refuses a mail that was
already received.
"""
import base64
from itertools import islice

from odoo import Command, _, api, fields, models
from odoo.addons.mail.tools.alias_error import AliasError
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import email_normalize

from ..services.constants import MAIL_IN_MAX_FILES
from .ir_attachment import stored_char
from .mail_thread_guard import _deny_non_internal, _header_sender_matches

MODEL = 'woow.document.mail.intake'
DEFAULTS_KEY = 'woow_mail_intake_id'        # key of the alias defaults: the mailbox of the alias
MAIL_IN_NOTE_KEY = 'woow_mail_in_note'      # context key of the in-process marker below
MAIL_PARTS_SCAN_MAX = 20 * MAIL_IN_MAX_FILES   # parts of one mail that are looked at
_USER = 'woow_document_management.group_woow_document_user'


class MailInNote:
    """The in-process marker of a mail that was filed: what the internal note says.

    A context value that a client sends is JSON and can never be an instance of this class, so
    ``message_post`` of the mailbox only honours the marker that ``message_new`` made.
    """

    __slots__ = ('stored', 'refused', 'ignored')

    def __init__(self, stored, refused, ignored):
        self.stored, self.refused, self.ignored = stored, refused, ignored


def _mail_file(item):
    """(name, bytes) of a regular attachment of a parsed mail, or None for an inline part."""
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None
    name, content = item[0], item[1]
    info = item[2] if len(item) > 2 and isinstance(item[2], dict) else {}
    if info.get('cid'):
        return None                      # an image of the body, not a file that was sent
    if isinstance(content, str):
        try:
            content = content.encode(info.get('encoding') or 'utf-8')
        except (LookupError, UnicodeError):
            content = content.encode('utf-8', 'replace')
    if not isinstance(content, (bytes, bytearray)):
        return None
    return (name if isinstance(name, str) and name else 'attachment'), bytes(content)


class WoowDocumentMailIntake(models.Model):
    _name = 'woow.document.mail.intake'
    _inherit = ['mail.alias.mixin', 'mail.thread', 'woow.document.guard.mixin']
    _description = 'Folder mailbox'
    _rec_name = 'folder_id'
    _order = 'id'
    _woow_own_default_fields = frozenset({
        'company_id', 'alias_id', 'message_follower_ids', 'message_ids'})
    _sql_constraints = [
        ('folder_unique', 'UNIQUE(folder_id)', 'This folder already has a mailbox.'),
    ]

    folder_id = fields.Many2one('woow.document.folder', string='Folder', required=True,
                                index=True, ondelete='restrict')
    tag_ids = fields.Many2many('woow.document.tag', 'woow_document_mail_intake_tag_rel',
                               'intake_id', 'tag_id', string='Tags')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(related='folder_id.company_id', store=True, index=True)

    # ------------------------------------------------------------------ the alias

    def _alias_get_creation_values(self):
        """The alias routes to this model, takes mail from followers only and names its mailbox."""
        values = super()._alias_get_creation_values()
        values['alias_model_id'] = self.env['ir.model']._get_id(self._name)
        values['alias_contact'] = 'followers'
        values['alias_force_thread_id'] = 0
        values['alias_defaults'] = repr({DEFAULTS_KEY: self.id or 0})
        return values

    def _woow_refuse_locked_alias_values(self, vals):
        """The routing of the alias is fixed: only its name, its domain and its bounce text change."""
        for key, value in vals.items():
            field = self._fields.get(key)
            if field is None or not field.inherited:
                continue
            if key == 'alias_contact' and value == 'followers':
                continue
            if key not in self.ALIAS_WRITEABLE_FIELDS or key in ('alias_defaults', 'alias_contact'):
                raise UserError(_('The routing of a folder mailbox is fixed.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._woow_refuse_locked_alias_values(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._woow_refuse_locked_alias_values(vals)
        return super().write(vals)

    @api.constrains('folder_id', 'tag_ids')
    def _check_tags_company(self):
        for box in self:
            if any(tag.company_id and tag.company_id != box.company_id for tag in box.tag_ids):
                raise ValidationError(_('A tag of another company cannot be used here.'))

    # ------------------------------------------------------------------ who may send

    def _woow_sender_documents(self, msg_dict):
        """The document model in the environment of the sender of ``msg_dict``, or None.

        The sender is the author partner that core found for the From address; it must carry that
        very address and follow this mailbox. One of its users must be active, internal, in the
        document group and have upload level or more on the folder, judged by the folder with the
        user's own environment. An archived mailbox, a folder in the trash and every other sender
        give None.
        """
        self.ensure_one()
        author_id = msg_dict.get('author_id') if isinstance(msg_dict, dict) else None
        sent_from = msg_dict.get('email_from') if isinstance(msg_dict, dict) else None
        if not self.active or self.folder_id.in_trash or type(author_id) is not int \
                or author_id < 1 or not isinstance(sent_from, str):
            return None
        partner = self.env['res.partner'].browse(author_id).exists()
        if not partner or not partner.email_normalized \
                or partner.email_normalized != email_normalize(sent_from, strict=False) \
                or partner not in self.message_partner_ids:
            return None
        users = partner.with_context(active_test=False).user_ids.filtered(
            lambda user: user.active and user._is_internal() and user.has_group(_USER))
        for user in users.sorted('id'):
            documents = self.env['woow.document'].with_user(user)  # sudo-ok: S4
            if documents.env['woow.document.folder'].browse(self.folder_id.id)._woow_has_rank(2):
                return documents
        return None

    def _alias_get_error(self, message, message_dict, alias):
        """Core's follower check, then the sender's own right on the folder (a bounce, no error)."""
        error = super()._alias_get_error(message, message_dict, alias)
        if error:
            return error
        if len(self) != 1 or self._woow_sender_documents(message_dict) is None:
            return AliasError('woow_mailbox_refused', _('mailbox unavailable'))
        return False

    # ------------------------------------------------------------------ receiving

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Store the attachments of a mail as documents of the folder of the mailbox of the alias.

        Only the mail gateway (the superuser) reaches this, with the values of the alias. The mail is
        refused, with the same error for every cause, unless its sender may upload to the folder.
        Returns the mailbox, marked so that ``message_post`` keeps nothing of the mail.
        """
        if self.env.su and isinstance(msg_dict, dict) and isinstance(custom_values, dict):
            box_id = custom_values.get(DEFAULTS_KEY)
            box = self.browse(box_id).exists() if type(box_id) is int and box_id > 0 \
                else self.browse()
            documents = box._woow_sender_documents(msg_dict) if box else None
            if documents is not None:
                note = box._woow_store_mail_files(documents, msg_dict)
                return box.with_context(**{MAIL_IN_NOTE_KEY: note})
        raise AccessError(_('This mailbox does not accept this email.'))

    def _woow_store_mail_files(self, documents, msg_dict):
        """One document per attachment, created with the rights of the sender; the counts."""
        self.ensure_one()
        files = []
        parts = list(islice(msg_dict.get('attachments') or (), MAIL_PARTS_SCAN_MAX))
        for item in parts:
            parsed = _mail_file(item)
            if parsed is not None:
                files.append(parsed)
        ignored = len(files) - MAIL_IN_MAX_FILES if len(files) > MAIL_IN_MAX_FILES else 0
        stored = refused = 0
        for name, data in files[:MAIL_IN_MAX_FILES]:
            values = {'name': name, 'folder_id': self.folder_id.id,
                      'datas': base64.b64encode(data), 'tag_ids': [Command.set(self.tag_ids.ids)]}
            try:
                with self.env.cr.savepoint():
                    documents.create([values])
            except UserError:
                refused += 1
            except Exception:
                import logging; logging.getLogger(__name__).exception('MAILIN DEBUG')
                raise                     # one refused file does not stop the others
            else:
                stored += 1
        self.env['woow.document.log']._log_event(
            'mail_in', channel=self._woow_log_channel(), user_id=documents.env.uid,
            folder_id=self.folder_id.id, company_id=self.folder_id.company_id.id,
            detail={'files': stored, 'refused': refused, 'ignored': ignored})
        return MailInNote(stored, refused, ignored)

    def message_post(self, **kwargs):
        """The post of core after ``message_new`` writes one internal note with the counts.

        Anything else is the ordinary post of an internal user.
        """
        note = self.env.context.get(MAIL_IN_NOTE_KEY)
        if isinstance(note, MailInNote):
            self.ensure_one()
            body = _('Received by email: %(stored)s stored, %(refused)s refused, %(ignored)s '
                     'ignored.', stored=note.stored, refused=note.refused, ignored=note.ignored)
            log = self.with_context(**{MAIL_IN_NOTE_KEY: None})._message_log(body=body)
            mail_id = kwargs.get('message_id')
            if isinstance(mail_id, str) and 0 < len(mail_id) <= 255:
                log.message_id = mail_id          # the key of core's check for a mail seen before
            return self.env['mail.message']       # core must not add the recipients to the note
        _deny_non_internal(self)
        return super().message_post(**kwargs)

    def message_update(self, msg_dict, update_vals=None):
        """A reply to the note of a mailbox is not a mail for the mailbox."""
        raise AccessError(_('This mailbox does not accept this email.'))

    @api.model
    def _get_thread_with_access(self, thread_id, mode="read", **kwargs):
        if not self.env.user._is_internal():
            return self.browse()
        thread = self.browse(thread_id)
        if thread.exists() and thread.sudo(False).has_access(mode):
            return thread
        return self.browse()


class WoowMailIntakeRoute(models.AbstractModel):
    _inherit = 'mail.thread'

    @api.model
    def _message_route_process(self, message, message_dict, routes):
        """Mail reaches a folder mailbox only through the real gateway, as the header says.

        As for documents and folders (``mail_thread_guard``): the superuser (fetchmail) or a system
        administrator (the gateway script), and the first address of the From header is the one core
        resolved. Whether the sender may use the mailbox is decided by the mailbox itself.
        """
        if any(route and stored_char(route[0]) == MODEL for route in routes or ()):
            if (not self.env.su and not self.env.user._is_system()) \
                    or not _header_sender_matches(message, message_dict):
                raise AccessError(_('This mailbox does not accept this email.'))
        return super()._message_route_process(message, message_dict, routes)


class WoowDocumentFolderMailIntake(models.Model):
    _inherit = 'woow.document.folder'

    def unlink(self):
        """A folder takes its mailbox (and the alias of it) along when it is deleted for good."""
        self._require_manager()
        boxes = self.env[MODEL].with_context(active_test=False).search(
            [('folder_id', 'in', self.ids)])
        with self.env.cr.savepoint():
            boxes.unlink()
            return super().unlink()
