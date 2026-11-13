# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Mail thread guard for documents and folders (IF-GUARD, owned by core-models-acl; ER-20).

``_get_thread_with_access`` never calls super, so the portal hash/pid/token fallback is never
reached for module models. Non-internal users cannot post, subscribe, remove other followers,
edit messages or write folders. Documents and folders are never created from incoming email,
and only the superuser or a system administrator may route email to them. An email reply to a
document or a folder is accepted only from an active internal user who can read it, because core
posts the reply as OdooBot and the checks above judge OdooBot, not the sender. Attachments that
back a binary field never become chatter attachments. A follower whose account can no longer
read the record is not notified, and folder chatter only ever names the folder itself.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import email_normalize

from .ir_attachment import is_module_model, stored_char

_THREAD_MODELS = ('woow.document', 'woow.document.folder')
_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'


def _deny_non_internal(records):
    if not records.env.user._is_internal():
        raise AccessError(_('Only internal users can do this.'))


def _deny_other_followers(records, partner_ids):
    """Users outside the company may only stop following a record themselves."""
    if partner_ids and not records.env.su and not records.env.user._is_internal() \
            and set(partner_ids) != {records.env.user.partner_id.id}:
        raise AccessError(_('Only internal users can do this.'))


def _deny_field_attachments(records, attachment_ids):
    """Core links pending attachments as superuser, so check them before it does."""
    if isinstance(attachment_ids, models.BaseModel):
        attachment_ids = attachment_ids.ids
    elif type(attachment_ids) is int:
        attachment_ids = [attachment_ids]
    if attachment_ids and records.env['ir.attachment']._woow_field_attachment_ids(
            list(attachment_ids)):
        raise AccessError(_('This attachment cannot be added to the discussion.'))


def _recipient_scope(records):
    """Groups and companies the access rules need, for the accounts being notified."""
    env = records.env
    return {'user': env.ref(_USER).id, 'manager': env.ref(_MANAGER).id,
            'portal': env.ref('base.group_portal').id}


def _readers_only(record, recipients, message, msg_vals):
    """Keep followers only while their account can read ``record``.

    Explicit recipients of the message and contacts without an active account
    (HD-10) are kept as core sends them.
    """
    followers = [data for data in recipients if data.get('is_follower') and data.get('uid')]
    if not followers or len(record) != 1:
        return recipients
    if msg_vals and 'partner_ids' in msg_vals:
        mentioned = {value for value in msg_vals.get('partner_ids') or () if type(value) is int}
    else:
        try:
            mentioned = set(message.partner_ids.ids)
        except AccessError:
            mentioned = set()
    scope = record._woow_follower_scope()
    scope.update(_recipient_scope(record))
    scope['companies'] = record._woow_user_companies([data['uid'] for data in followers])
    return [data for data in recipients
            if not data.get('is_follower') or not data.get('uid') or data['id'] in mentioned
            or record._woow_recipient_reads(data, scope)]


def _reply_sender_reads(record, msg_dict):
    """Whether the sender of an incoming email is an active internal user who can read ``record``.

    The sender is the author partner that core found for the From address and put in ``msg_dict``
    (``_routing_check_route``): a follower of the record first, then a partner who has a user,
    then any partner; nobody when the address is unknown, and the first address when the header
    names several. The partner must carry that very address, and one of its users must be active
    and internal and pass the read rules of the record. The rules are asked as for a follower who
    is notified (``_woow_recipient_reads``, no identity switch). A partner shared by several users
    is accepted when one of them qualifies, since the email does not say which one wrote it.
    Everything else is a refusal: portal and public users, a contact without an account (staff
    may add one as a follower, HD-10), a partner whose user is archived, left the document group
    or has no right on the record.
    """
    author_id = msg_dict.get('author_id') if isinstance(msg_dict, dict) else None
    sent_from = msg_dict.get('email_from') if isinstance(msg_dict, dict) else None
    if type(author_id) is not int or author_id < 1 or not isinstance(sent_from, str):
        return False
    partner = record.env['res.partner'].browse(author_id).exists()
    if not partner or not partner.email_normalized \
            or partner.email_normalized != email_normalize(sent_from, strict=False):
        return False
    users = partner.user_ids.filtered(lambda user: user.active and user._is_internal())
    if not users:
        return False
    scope = record._woow_follower_scope()
    scope.update(_recipient_scope(record))
    scope['companies'] = record._woow_user_companies(users.ids)
    return any(
        record._woow_recipient_reads(
            {'id': partner.id, 'uid': user.id, 'groups': user.groups_id.ids, 'ushare': False},
            scope)
        for user in users)


def _sender_reads_all(records, msg_dict):
    """Whether the sender of an incoming email may read every record in ``records``.

    Core calls ``message_update`` for every route to an existing record, before it posts the
    email and before anything is written, so a refusal there leaves no message, no file and no
    follower. The two models raise the same error for every cause, and it does not depend on
    the email.
    """
    return all(_reply_sender_reads(record, msg_dict) for record in records)


class WoowDocumentThreadGuard(models.Model):
    _inherit = 'woow.document'

    # A domain term that starts at one of these fields (``message_ids.body``, say) is
    # answered by a sub-select on mail_message that skips the message record rules
    # (R-M0), so a share user could test the text of a log note. Core checks field
    # groups for the first segment of a term before it searches (guard_mixin), which
    # refuses these fields to everyone outside the internal group.
    message_ids = fields.One2many(groups='base.group_user')
    website_message_ids = fields.One2many(groups='base.group_user')
    has_message = fields.Boolean(groups='base.group_user')

    @api.model
    def _get_thread_with_access(self, thread_id, mode="read", **kwargs):
        if not self.env.user._is_internal():
            return self.browse()
        thread = self.browse(thread_id)
        if thread.exists() and thread.sudo(False).has_access(mode):
            return thread
        return self.browse()

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Documents are created through the upload policy only, never from email.

        The error is the one of ``message_update``: a refusal does not say whether the record exists.
        """
        raise AccessError(_('Documents and folders do not accept this email.'))

    def message_update(self, msg_dict, update_vals=None):
        """An email reaches a document only from an internal user who can read it (policy B)."""
        if not _sender_reads_all(self, msg_dict):
            raise AccessError(_('Documents and folders do not accept this email.'))
        return super().message_update(msg_dict, update_vals=update_vals)

    def message_post(self, **kwargs):
        _deny_non_internal(self)
        return super().message_post(**kwargs)

    def message_subscribe(self, *args, **kwargs):
        _deny_non_internal(self)
        return super().message_subscribe(*args, **kwargs)

    def message_unsubscribe(self, partner_ids=None):
        _deny_other_followers(self, partner_ids)
        return super().message_unsubscribe(partner_ids=partner_ids)

    def _message_update_content(self, *args, **kwargs):
        _deny_non_internal(self)
        return super()._message_update_content(*args, **kwargs)

    def _process_attachments_for_post(self, attachments, attachment_ids, message_values):
        _deny_field_attachments(self, attachment_ids)
        return super()._process_attachments_for_post(attachments, attachment_ids, message_values)

    def _notify_get_recipients(self, message, msg_vals, **kwargs):
        recipients = super()._notify_get_recipients(message, msg_vals, **kwargs)
        return _readers_only(self, recipients, message, msg_vals)

    def _woow_follower_scope(self):
        """The stored fields behind the document read rules R-D0, R-D1, R-D4 and R-D5."""
        self.ensure_one()
        self.flush_recordset(['active', 'folder_in_trash', 'owner_id', 'company_id', 'folder_id',
                              'shared_user_ids', 'shared_partner_ids'])
        self.env.cr.execute(  # sql-ok: Q18
            """SELECT d.active, d.folder_in_trash, d.owner_id, d.company_id, d.folder_id,
                      ARRAY(SELECT user_id FROM woow_document_shared_user_rel
                            WHERE document_id = d.id),
                      ARRAY(SELECT partner_id FROM woow_document_shared_partner_rel
                            WHERE document_id = d.id)
               FROM woow_document d WHERE d.id = %s""", (self.id,))
        row = self.env.cr.fetchone()
        if not row:
            return {'missing': True}
        active, in_trash, owner, company, folder_id, shared_users, shared_partners = row
        scope = self.env['woow.document.folder'].browse(folder_id)._woow_follower_scope()
        if scope.get('missing'):
            return scope
        scope.update(active=bool(active), folder_in_trash=bool(in_trash), owner=owner,
                     company=company, shared_users=set(shared_users),
                     shared_partners=set(shared_partners))
        return scope

    @api.model
    def _woow_user_companies(self, user_ids):
        return self.env['woow.document.folder']._woow_user_companies(user_ids)

    def _woow_recipient_reads(self, data, scope):
        """Whether the notified account passes the document read rules (no identity switch).

        The company rule R-D0 has no groups, so portal accounts are held to it as
        well; R-D5 is the portal group's rule.
        """
        if scope.get('missing'):
            return False
        uid, groups = data['uid'], set(data.get('groups') or ())
        if scope['company'] and scope['company'] not in scope['companies'].get(uid, ()):
            return False
        if data.get('ushare'):
            return scope['portal'] in groups and scope['active'] and not scope['folder_in_trash'] \
                and (data['id'] in scope['read_partners'] or data['id'] in scope['shared_partners'])
        if scope['manager'] in groups:
            return True
        if scope['user'] not in groups or scope['folder_in_trash']:
            return False

        def member(level):
            return uid in scope[level + '_users'] or bool(groups & scope[level + '_groups'])

        if member('edit') or (scope['owner'] == uid and member('upload')):
            return True
        return scope['active'] and (member('read') or uid in scope['shared_users'])


class WoowDocumentFolderThreadGuard(models.Model):
    _inherit = 'woow.document.folder'

    message_ids = fields.One2many(groups='base.group_user')
    website_message_ids = fields.One2many(groups='base.group_user')
    has_message = fields.Boolean(groups='base.group_user')

    @api.model
    def _get_thread_with_access(self, thread_id, mode="read", **kwargs):
        if not self.env.user._is_internal():
            return self.browse()
        thread = self.browse(thread_id)
        if thread.exists() and thread.sudo(False).has_access(mode):
            return thread
        return self.browse()

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Folders follow the site root policy and parent grants, never email."""
        raise AccessError(_('Documents and folders do not accept this email.'))

    def message_update(self, msg_dict, update_vals=None):
        """An email reaches a folder only from an internal user who can read it (policy B)."""
        if not _sender_reads_all(self, msg_dict):
            raise AccessError(_('Documents and folders do not accept this email.'))
        return super().message_update(msg_dict, update_vals=update_vals)

    def message_post(self, **kwargs):
        _deny_non_internal(self)
        # Core stores the poster's view of the folder path for every recipient;
        # a folder message names only the folder itself (I-08).
        self.ensure_one()
        kwargs['record_name'] = self._woow_chatter_name()
        return super().message_post(**kwargs)

    def message_subscribe(self, *args, **kwargs):
        _deny_non_internal(self)
        return super().message_subscribe(*args, **kwargs)

    def message_unsubscribe(self, partner_ids=None):
        _deny_other_followers(self, partner_ids)
        return super().message_unsubscribe(partner_ids=partner_ids)

    def _message_update_content(self, *args, **kwargs):
        _deny_non_internal(self)
        return super()._message_update_content(*args, **kwargs)

    def _message_compute_subject(self):
        """Mail subjects name the folder itself, never its ancestors."""
        self.ensure_one()
        return self._woow_chatter_name()

    def _woow_chatter_name(self):
        """The folder's own name, or the restricted label when the caller cannot read it."""
        self.ensure_one()
        try:
            return self.name
        except AccessError:
            return _('Restricted folder')

    def _process_attachments_for_post(self, attachments, attachment_ids, message_values):
        _deny_field_attachments(self, attachment_ids)
        return super()._process_attachments_for_post(attachments, attachment_ids, message_values)

    def _notify_get_recipients(self, message, msg_vals, **kwargs):
        recipients = super()._notify_get_recipients(message, msg_vals, **kwargs)
        return _readers_only(self, recipients, message, msg_vals)

    def _woow_follower_scope(self):
        """The stored grants behind the folder read rules R-F0, R-F1, R-F4 and R-F5."""
        self.ensure_one()
        self.flush_recordset([
            'in_trash', 'company_id', 'eff_read_user_ids', 'eff_read_group_ids',
            'eff_upload_user_ids', 'eff_upload_group_ids', 'eff_edit_user_ids',
            'eff_edit_group_ids', 'eff_read_partner_ids'])
        self.env.cr.execute(  # sql-ok: Q18
            """SELECT f.in_trash, f.company_id,
                      ARRAY(SELECT user_id FROM woow_doc_folder_eff_r_user_rel WHERE folder_id = f.id),
                      ARRAY(SELECT group_id FROM woow_doc_folder_eff_r_group_rel WHERE folder_id = f.id),
                      ARRAY(SELECT user_id FROM woow_doc_folder_eff_u_user_rel WHERE folder_id = f.id),
                      ARRAY(SELECT group_id FROM woow_doc_folder_eff_u_group_rel WHERE folder_id = f.id),
                      ARRAY(SELECT user_id FROM woow_doc_folder_eff_e_user_rel WHERE folder_id = f.id),
                      ARRAY(SELECT group_id FROM woow_doc_folder_eff_e_group_rel WHERE folder_id = f.id),
                      ARRAY(SELECT partner_id FROM woow_doc_folder_eff_r_partner_rel
                            WHERE folder_id = f.id)
               FROM woow_document_folder f WHERE f.id = %s""", (self.id,))
        row = self.env.cr.fetchone()
        if not row:
            return {'missing': True}
        (in_trash, company, read_users, read_groups, upload_users, upload_groups, edit_users,
         edit_groups, read_partners) = row
        return {'in_trash': bool(in_trash), 'company': company,
                'read_users': set(read_users), 'read_groups': set(read_groups),
                'upload_users': set(upload_users), 'upload_groups': set(upload_groups),
                'edit_users': set(edit_users), 'edit_groups': set(edit_groups),
                'read_partners': set(read_partners)}

    @api.model
    def _woow_user_companies(self, user_ids):
        """Companies of each user (the company rule of the notified account)."""
        ids = [value for value in user_ids if type(value) is int]
        if not ids:
            return {}
        self.env['res.users'].flush_model(['company_ids'])
        self.env.cr.execute(  # sql-ok: Q18
            'SELECT user_id, array_agg(cid) FROM res_company_users_rel '
            'WHERE user_id = ANY(%s) GROUP BY user_id', (ids,))
        return {user_id: set(companies) for user_id, companies in self.env.cr.fetchall()}

    def _woow_recipient_reads(self, data, scope):
        """Whether the notified account passes the folder read rules (no identity switch).

        The company rule R-F0 has no groups, so portal accounts are held to it as
        well; R-F5 is the portal group's rule.
        """
        if scope.get('missing'):
            return False
        uid, groups = data['uid'], set(data.get('groups') or ())
        if scope['company'] and scope['company'] not in scope['companies'].get(uid, ()):
            return False
        if data.get('ushare'):
            return scope['portal'] in groups and not scope['in_trash'] \
                and data['id'] in scope['read_partners']
        if scope['manager'] in groups:
            return True
        if scope['user'] not in groups:
            return False
        reads = uid in scope['read_users'] or bool(groups & scope['read_groups'])
        edits = uid in scope['edit_users'] or bool(groups & scope['edit_groups'])
        return (not scope['in_trash'] and reads) or edits

    def write(self, vals):
        """Portal users only probe the folder write rule (HD-07); real writes are refused."""
        _deny_non_internal(self)
        return super().write(vals)


class WoowMailThreadRoute(models.AbstractModel):
    _inherit = 'mail.thread'

    def _process_attachments_for_post(self, attachments, attachment_ids, message_values):
        """Notifications attach files to a record of this module only for those who may write it.

        ``message_notify`` is open to every signed-in user and takes any model and id (also on an
        empty recordset of another model, so the overrides of the module's own models never run).
        Core then creates the files, or links the pending ones, as superuser and looks neither at
        the record nor at its rules. Posts are not touched: a follower who can only read a record
        may still post a comment with a file. The model is judged as the column of the file keeps
        it (``is_module_model``), so a value of another type that stores as the name of a model of
        this module is one of them.
        """
        if (attachments or attachment_ids) and not self.env.su \
                and message_values.get('message_type') == 'user_notification' \
                and is_module_model(message_values.get('model')) and message_values.get('res_id'):
            res_id = message_values['res_id']
            record = self.env[stored_char(message_values['model'])].browse(
                res_id if type(res_id) is int else [])
            if not record.exists() or not record.has_access('write'):
                raise AccessError(_('This attachment cannot be added to the discussion.'))
        return super()._process_attachments_for_post(attachments, attachment_ids, message_values)

    @api.model
    def _message_route_process(self, message, message_dict, routes):
        """Incoming email reaches documents and folders only through the real gateway.

        ``message_process`` is callable by any user and runs the routed thread
        methods as superuser with caller-chosen routes, so a route to this
        module's models needs the superuser (fetchmail) or a system administrator
        (the mail gateway script). Whose email is accepted is not decided here, where only
        the caller is known, but by ``message_update`` of the two models, which core calls for
        every route to an existing record and which sees the sender (``_reply_sender_reads``).
        """
        if not self.env.su and not self.env.user._is_system() and any(
                route and stored_char(route[0]) in _THREAD_MODELS for route in routes or ()):
            raise AccessError(_('Documents and folders do not accept this email.'))
        return super()._message_route_process(message, message_dict, routes)


class WoowMailActivityName(models.Model):
    _inherit = 'mail.activity'

    def _compute_res_name(self):
        """Activities on a folder name the folder itself, never its ancestors (I-08)."""
        folders = self.filtered(lambda activity: activity.res_model == 'woow.document.folder')
        super(WoowMailActivityName, self - folders)._compute_res_name()
        for activity in folders:
            folder = self.env['woow.document.folder'].browse(activity.res_id).exists()
            activity.res_name = folder._woow_chatter_name() if folder else False


class WoowMailMessageName(models.Model):
    _inherit = 'mail.message'

    @api.model
    def _get_record_name(self, values):
        """Messages on a folder store the folder's own name, never its ancestors (I-08)."""
        model = values.get('model', self.env.context.get('default_model'))
        res_id = values.get('res_id', self.env.context.get('default_res_id'))
        if model == 'woow.document.folder' and res_id:
            folder = self.env[model].browse(res_id).exists()
            return folder._woow_chatter_name() if folder else False
        return super()._get_record_name(values)

    @api.depends('record_name', 'model')
    @api.depends_context('uid')
    def _compute_display_name(self):
        """A message of a folder or a document names it only to a caller who may read it (I-08).

        Core builds the label of a many2one as superuser with the uid unchanged, and every
        internal user may read ``mail.notification``, whose message label is this name: the
        access check runs without the superuser flag, as the real caller.
        """
        super()._compute_display_name()
        threads = self.filtered(lambda message: message.model in _THREAD_MODELS)
        for message in threads - threads.sudo(False)._filtered_access('read'):
            message.display_name = (
                _('Restricted folder') if message.model == 'woow.document.folder'
                else _('Restricted document'))
