# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Share issuance and request scope resolution (S2 is isolated here)."""
import logging
from datetime import timedelta
from ipaddress import ip_address
from urllib.parse import urlsplit

from psycopg2 import errors

from odoo import _, Command, api, fields, models
from odoo.exceptions import AccessError, UserError

from .document_share import SECRET_CTX_KEY, _SECRET_SENTINEL
from ..services import tokens
from ..services.constants import (MAIL_PER_USER_PER_HOUR, MAX_SHARE_DOCUMENTS,
                                 SHARE_RECORD_RETENTION_DAYS)
from ..services.locks import lock
from ..services.zip_builder import ZipLimitExceeded

_logger = logging.getLogger(__name__)

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'


def _selected_ids(commands):
    """Accept one explicit M2M set command, never ambient defaults or broad searches."""
    if not isinstance(commands, (tuple, list)) or len(commands) != 1:
        raise UserError(_('Select documents explicitly.'))
    command = commands[0]
    if not isinstance(command, (tuple, list)) or len(command) != 3 or command[0] != 6 \
            or not isinstance(command[2], (tuple, list)):
        raise UserError(_('Select documents explicitly.'))
    ids = list(command[2])
    if not ids or len(ids) > MAX_SHARE_DOCUMENTS or len(ids) != len(set(ids)) \
            or any(type(item) is not int or item < 1 for item in ids):
        raise UserError(_('Select documents explicitly.'))
    return ids


class WoowDocumentShareService(models.Model):
    _inherit = 'woow.document.share'          # no _name on purpose (check_sudo, ER-44)

    download_count = fields.Integer(compute='_compute_download_count')

    def _compute_download_count(self):
        """Count successful downloads visible to the current user."""
        for share in self:
            share.download_count = self.env['woow.document.log']._sum_count([
                ('share_id', '=', share.id), ('event', '=', 'share_download'),
                ('result', '=', 'ok')])

    def _woow_check_scope_env(self, scope_env):
        """Public share work runs only in the creator's non-superuser scope."""
        self.ensure_one()
        if scope_env.su or scope_env.uid != self.create_uid.id or \
                scope_env.company.id != self.company_id.id:
            raise AccessError(_('The share is unavailable.'))

    def _woow_lock_share_row(self, scope_env):
        """Row lock that writes no row version (a barrier for stale snapshots).

        Under Repeatable Read, SELECT ... FOR NO KEY UPDATE fails with
        serialization_failure when a transaction that committed after this snapshot
        updated the row (``_woow_count_on_share_row``). A transaction that only
        locked the row never makes another one fail, so an attempt that ends without
        a counted event cannot make an owner's revoke or edit of the link fail.
        Foreign key checks of new audit rows (FOR KEY SHARE) do not wait for it.
        """
        scope_env.cr.execute(  # sql-ok: Q12
            'SELECT id FROM woow_document_share WHERE id = %s FOR NO KEY UPDATE', (self.id,))
        return bool(scope_env.cr.fetchone())

    def _woow_count_on_share_row(self, scope_env):
        """Write a new row version when a counted event of this link is recorded."""
        scope_env.cr.execute(  # sql-ok: Q12
            'UPDATE woow_document_share SET write_date = write_date '
            'WHERE id = %s RETURNING id', (self.id,))
        if not scope_env.cr.fetchone():
            raise AccessError(_('The share is unavailable.'))

    def _woow_download_count(self, scope_env):
        return scope_env['woow.document.log']._sum_count([
            ('share_id', '=', self.id), ('event', '=', 'share_download'),
            ('result', '=', 'ok')])

    def _download_slot_available(self, scope_env):
        """Decide one file or ZIP download of a limited link from a current count.

        A stale Repeatable Read snapshot can only miss downloads, so a link that is
        used up in the request snapshot is refused at once, without the share lock
        or a write. Otherwise the share lock orders the requests and the row lock
        makes one whose snapshot predates a counted download (``_record_download``
        updates the share row) fail with serialization_failure; Odoo retries it with
        a fresh transaction instead of granting an extra download from a stale count.
        """
        self._woow_check_scope_env(scope_env)
        if self.max_downloads and self._woow_download_count(scope_env) >= self.max_downloads:
            return False
        lock(scope_env.cr, 'share:%d' % self.id)
        if not self.max_downloads:
            return True
        if not self._woow_lock_share_row(scope_env):
            return False
        return self._woow_download_count(scope_env) < self.max_downloads

    def _record_download(self, scope_env, log_env, ip, detail):
        """Log one share download; for a limited link also update the share row.

        ``scope_env`` is the creator's environment (the link's scope and the count of its
        downloads); the event is recorded in ``log_env``, the environment of the request,
        so that it names the visitor and not the creator.
        """
        self._woow_check_scope_env(scope_env)
        if self.max_downloads:
            self._woow_count_on_share_row(scope_env)
        log_env['woow.document.log']._log_event(
            'share_download', channel='share', share_id=self.id,
            company_id=self.company_id.id, ip=ip, detail=detail)

    def _lock_password_attempts(self, scope_env):
        """Serialize this link's password comparisons before its failure counts are read.

        The advisory lock orders the attempts that compare a password. The row lock
        writes no row version: only a counted failure updates the share row
        (``_count_password_failure``), so an attempt whose snapshot predates one
        fails with serialization_failure and is retried before any comparison, while
        attempts without a counted failure never make the owner's updates fail.
        """
        self._woow_check_scope_env(scope_env)
        lock(scope_env.cr, 'share:%d' % self.id)
        return self._woow_lock_share_row(scope_env)

    def _count_password_failure(self, scope_env):
        """Mark a counted wrong password on the share row, under the share lock."""
        self._woow_check_scope_env(scope_env)
        self._woow_count_on_share_row(scope_env)

    @api.model
    def _issue(self, vals):
        """Create a link and return (share, plaintext token); the token exists only in the result."""
        data = dict(vals)
        password = data.pop('password', None)
        password_hash = data.pop('password_hash', None)   # hashed by the share wizard on save
        self._check_create_rights(data)
        if password:
            data['password_hash'] = tokens.hash_password(password)
        elif password_hash:
            data['password_hash'] = password_hash
        elif self.env['woow.document.settings']._get_settings().share_require_password:
            raise UserError(_('A password is required for share links.'))
        token = tokens.new_token()
        keys = self.env['woow.document.settings']._share_keys()
        data['token_hash'] = tokens.token_hash(keys.k_share, token)
        data['token_hint'] = tokens.token_hint(keys.k_hint, token)
        data.setdefault('company_id', self.env.company.id)
        share = self.with_context(**{SECRET_CTX_KEY: _SECRET_SENTINEL}).create(data)
        self.env['woow.document.log']._log_event(
            'share_create', channel=self._woow_log_channel(), share_id=share.id,
            company_id=share.company_id.id)
        return share, token

    def _check_create_rights(self, vals):
        """Require live management rights over every selected scope record."""
        if self.env.su:
            return
        user = self.env.user
        if not user._is_internal() or not user.has_group(_USER):
            raise AccessError(_('You cannot create share links.'))
        settings = self.env['woow.document.settings']._get_settings()
        if settings.share_link_policy == 'managers_only' and not user.has_group(_MANAGER):
            raise AccessError(_('You cannot create share links.'))
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        expiry = fields.Datetime.to_datetime(vals.get('expire_at'))
        if not expiry or not now < expiry <= now + timedelta(days=settings.share_max_days):
            raise UserError(_('The share expiry is outside the allowed period.'))
        if vals.get('max_downloads', 0) < 0:
            raise UserError(_('The download limit is invalid.'))
        if vals.get('company_id', self.env.company.id) != self.env.company.id:
            raise AccessError(_('The share company is unavailable.'))
        kind = vals.get('share_type')
        if kind == 'folder':
            folder = self.env['woow.document.folder'].browse(vals.get('folder_id')).exists()
            if not folder or folder.in_trash or folder.company_id != self.env.company:
                raise AccessError(_('The folder is unavailable.'))
            folder.check_access('read')
            folder._woow_require_rank(4)
        elif kind == 'documents':
            ids = _selected_ids(vals.get('document_ids'))
            docs = self.env['woow.document'].search([('id', 'in', ids)])
            if len(docs) != len(ids) or any(
                    not doc.active or doc.folder_in_trash or doc.company_id != self.env.company
                    for doc in docs):
                raise AccessError(_('The documents are unavailable.'))
            docs.check_access('read')
            for folder in docs.mapped('folder_id'):
                folder._woow_require_rank(4)
        else:
            raise UserError(_('Select a share scope.'))

    @api.model
    def _resolve_request_access(self, token=None, share_id=None):
        """S2: return (share_sudo, scope_env), or (empty recordset, None) when not found."""
        if (token is None) == (share_id is None):
            return self.browse(), None
        if token is not None and tokens.extract_code(token) != token:
            return self.browse(), None
        if share_id is not None and (type(share_id) is not int or share_id < 1):
            return self.browse(), None
        try:
            digest = tokens.token_hash(self.env['woow.document.settings']._share_keys().k_share,
                                       token) if token else None
        except ValueError:
            return self.browse(), None
        share_sudo = self.sudo().search([('token_hash', '=', digest)] if token else [('id', '=', share_id)], limit=1)  # sudo-ok: S2
        if not share_sudo:
            return self.browse(), None
        if token and not tokens.verify(self.env['woow.document.settings']._share_keys().k_share,
                                       token, share_sudo.token_hash):
            return self.browse(), None
        if not share_sudo.create_uid:
            # The creator was deleted. ``with_user`` of no user keeps the superuser
            # environment, which would run the link's scope without any record rule.
            return self.browse(), None
        scope_env = share_sudo.with_user(share_sudo.create_uid).with_context(allowed_company_ids=[share_sudo.company_id.id]).env  # sudo-drop: S2
        return share_sudo, scope_env

    def _validate_for_access(self, scope_env):
        """Re-evaluate expiry, creator, company and current document scope."""
        try:
            self.ensure_one()
            if scope_env is None:
                return False
            # The creator's own environment, never a superuser one: the checks below
            # take the user of ``scope_env`` for the creator.
            self._woow_check_scope_env(scope_env)
            now = fields.Datetime.to_datetime(fields.Datetime.now())
            creator = scope_env.user
            if self.revoked_at or self.state != 'active' or self.expire_at <= now:
                return False
            if not creator.active or not creator._is_internal() or not creator.has_group(_USER):
                return False
            settings = scope_env['woow.document.settings']._get_settings()
            if settings.share_link_policy == 'managers_only' and not creator.has_group(_MANAGER):
                return False
            company = scope_env['res.company'].browse(self.company_id.id).exists()
            if not company or not company.active or company not in creator.company_ids:
                return False
            if self.share_type == 'folder':
                folder = scope_env['woow.document.folder'].search([('id', '=', self.folder_id.id)], limit=1)
                if not folder or folder.in_trash or not folder._woow_has_rank(4):
                    return False
            # Public requests only need to know whether one live document remains.
            # Do not materialize the whole share before the page/ZIP limits run.
            domain = self._scope_domain()
            if not creator.has_group(_MANAGER):
                rank = scope_env['woow.document.folder']._woow_rank_domain(4)
                domain.append(('folder_id', 'any', rank))
            return bool(scope_env['woow.document'].search(domain, limit=1))
        except (AccessError, UserError, ValueError):
            return False

    def _scope_domain(self):
        """Document domain of the link scope (arch section 8.5 step 6)."""
        self.ensure_one()
        # Restore timestamps are stored to whole seconds. Round the link's
        # creation time down too, so a restore in the same second cannot make
        # an older link eligible again.
        created_second = fields.Datetime.to_datetime(self.create_date).replace(microsecond=0)
        scope = ([('folder_id', 'child_of' if self.include_subfolders else '=', self.folder_id.id)]
                 if self.share_type == 'folder' else
                 [('id', 'in', self.document_ids.ids)])
        return scope + [('active', '=', True), ('folder_in_trash', '=', False),
                        '|', ('effective_restored_at', '=', False),
                        ('effective_restored_at', '<', created_second)]

    def _scope_documents(self, scope_env, extra_domain=None, max_candidates=None):
        """Search under the creator's record rules and current management rights."""
        docs = scope_env['woow.document'].search(
            self._scope_domain() + (extra_domain or []),
            limit=max_candidates + 1 if max_candidates is not None else None)
        if max_candidates is not None and len(docs) > max_candidates:
            raise ZipLimitExceeded('files')
        return docs.filtered(lambda doc: doc.folder_id._woow_has_rank(4))

    def _woow_link_url(self, token):
        """Public link: configured base URL or get_base_url(), then '/woow-docs/s#' + token."""
        self.ensure_one()
        if tokens.extract_code(token) != token:
            raise ValueError('invalid share token')
        settings = self.env['woow.document.settings']._get_settings()
        base = (settings.share_link_base_url or self.get_base_url()).rstrip('/')
        parsed = urlsplit(base)
        try:
            literal_ip = ip_address(parsed.hostname or '')
        except ValueError:
            literal_ip = None
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username \
                or parsed.password or literal_ip or parsed.path not in ('', '/') \
                or parsed.query or parsed.fragment or (parsed.scheme == 'http' and not settings.allow_http_links):
            raise UserError(_('Configure a public HTTPS address for share links.'))
        return base + '/woow-docs/s#' + token

    def _woow_send_link(self, token, note):
        """Queue the link mail (force_send=False) and log share_send."""
        self.ensure_one()
        if not self.recipient_email:
            return
        # Serialize the hourly check and its queue/log writes per sender. Under
        # Odoo's repeatable-read transactions a row lock alone leaves an older
        # snapshot usable; the UPDATE makes a competing old snapshot fail with
        # serialization_failure, so the request can be retried on a fresh one.
        self.env.cr.execute(  # sql-ok: Q11
            'UPDATE res_users SET write_date = write_date '
            'WHERE id = %s RETURNING id', (self.env.uid,))
        if not self.env.cr.fetchone():
            raise AccessError(_('The sender is unavailable.'))
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        sent = self.env['woow.document.log']._sum_count([
            ('event', '=', 'share_send'), ('result', '=', 'ok'),
            ('user_id', '=', self.env.uid), ('create_date', '>=', now - timedelta(hours=1))])
        if sent >= MAIL_PER_USER_PER_HOUR:
            raise UserError(_('The hourly share email limit has been reached.'))
        sender = self.env.user.email_formatted or self.company_id.email
        if not sender:
            raise UserError(_('Set a sender email address before sharing documents.'))
        url = self._woow_link_url(token)
        template = self.env.ref('woow_document_management.mail_template_woow_document_share_link')
        template.with_context(woow_share_url=url, woow_share_note=note or '').send_mail(
            self.id, force_send=False,
            email_values={'email_to': self.recipient_email, 'recipient_ids': [],
                          'email_from': sender, 'auto_delete': True},
            email_layout_xmlid='mail.mail_notification_light')
        # Core copies the rendered mail into the queued mail's parent message, which
        # anyone who reads the share may read. Only the system-only mail.mail keeps
        # the link (HD-08); the sender is the author and may clear the copy.
        self.env['mail.message'].search([
            ('model', '=', self._name), ('res_id', '=', self.id),
            ('message_type', '=', 'email_outgoing')]).write({'body': ''})
        self.env['woow.document.log']._log_event(
            'share_send', channel=self._woow_log_channel(), share_id=self.id,
            company_id=self.company_id.id)

    def action_revoke(self):
        """Revoke the link."""
        self.check_access('write')
        for share in self:
            if share.revoked_at:
                continue
            share._woow_mark_revoked()
            self.env['woow.document.log']._log_event(
                'share_revoke', channel=self._woow_log_channel(), share_id=share.id,
                company_id=share.company_id.id)
        return True

    def action_reissue(self):
        """Revoke the link and issue a new one with the same scope."""
        self.check_access('write')
        self.ensure_one()
        if self.has_password:
            raise UserError(_('Set a new password through the share wizard before replacing this link.'))
        data = {'name': self.name, 'share_type': self.share_type,
                'company_id': self.company_id.id,
                'folder_id': self.folder_id.id if self.folder_id else False,
                'include_subfolders': self.include_subfolders,
                'document_ids': [Command.set(self.document_ids.ids)] if self.share_type == 'documents' else [],
                'expire_at': fields.Datetime.to_datetime(fields.Datetime.now()) + timedelta(
                    days=self.env['woow.document.settings']._get_settings().share_default_days),
                'max_downloads': self.max_downloads, 'recipient_email': self.recipient_email,
                'replaced_share_id': self.id}
        with self.env.cr.savepoint():
            self.action_revoke()
            fresh, token = self._issue(data)
        return {'type': 'ir.actions.client', 'tag': 'woow_document_share_result',
                'params': {'links': [fresh._woow_link_url(token)]}}

    def _maintenance_run(self):
        """Hourly: expire, clean up old links and failed mails (arch section 8.6)."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        failed_cutoff = now - timedelta(hours=24)
        failed_mails = self.env['mail.mail'].search([
            ('model', '=', 'woow.document.share'), ('state', '=', 'exception'),
            ('auto_delete', '=', True), ('is_notification', '=', False),
            ('create_date', '<=', failed_cutoff)], limit=200)
        activities = self.env['mail.activity']
        activity_type = self.env.ref('mail.mail_activity_data_todo')
        partner_model = self.env.ref('base.model_res_partner')
        for mail in failed_mails:
            with self.env.cr.savepoint():
                share = self.browse(mail.res_id).exists()
                # A template mail is not a thread notification: core mail.mail
                # unlink also removes its parent mail.message containing the URL.
                mail.unlink()
                if share and share.create_uid.active and share.create_uid.partner_id:
                    creator = share.create_uid
                    summary = _('A document share email could not be delivered')
                    existing = activities.search_count([
                        ('res_model_id', '=', partner_model.id),
                        ('res_id', '=', creator.partner_id.id),
                        ('user_id', '=', creator.id),
                        ('summary', '=', summary),
                        ('date_deadline', '=', fields.Date.context_today(self))])
                    if not existing:
                        activities.create({
                            'res_model_id': partner_model.id,
                            'res_id': creator.partner_id.id,
                            'user_id': creator.id,
                            'activity_type_id': activity_type.id,
                            'date_deadline': fields.Date.context_today(self),
                            'summary': summary,
                            'note': _('The failed share email was removed after 24 hours. '
                                      'Create a new link if the recipient still needs access.'),
                        })
        expired = self.search([('state', '=', 'active'), ('expire_at', '<=', now)], limit=200)
        for share in expired:
            with self.env.cr.savepoint():
                share.write({'state': 'expired'})
                self.env['woow.document.log']._log_event(
                    'share_expire', channel='cron', share_id=share.id,
                    company_id=share.company_id.id)
        cutoff = now - timedelta(days=SHARE_RECORD_RETENTION_DAYS)
        old = self.search(['|', '&', ('state', '=', 'expired'), ('expire_at', '<', cutoff),
                           '&', ('state', '=', 'revoked'), ('revoked_at', '<', cutoff)], limit=200)
        for share in old:
            # One link the database refuses to delete (a constraint its rows violate)
            # never stops the run, which would roll back the expiry marking and the
            # removal of failed mails above: it stays, and the next run tries again.
            try:
                with self.env.cr.savepoint():
                    share.unlink()
            except errors.IntegrityError:
                _logger.warning('A share link could not be deleted and was skipped: id %s',
                                share.id)
        return None


class WoowDocumentShareButton(models.Model):
    _inherit = 'woow.document'

    def action_open_share_wizard(self):
        """Open the share link wizard for the selected documents."""
        self.check_access('read')
        if not self or len(self) > MAX_SHARE_DOCUMENTS:
            raise UserError(_('Select between 1 and 500 documents.'))
        for folder in self.mapped('folder_id'):
            folder._woow_require_rank(4)
        return {'type': 'ir.actions.act_window', 'res_model': 'woow.document.share.wizard',
                'name': _('Share documents'), 'view_mode': 'form', 'target': 'new',
                'context': {'default_share_type': 'documents',
                            'default_document_ids': [Command.set(self.ids)]}}


class WoowDocumentFolderShareButton(models.Model):
    _inherit = 'woow.document.folder'

    def action_open_share_wizard(self):
        """Open the share link wizard for the folder."""
        self.check_access('read')
        self.ensure_one()
        self._woow_require_rank(4)
        return {'type': 'ir.actions.act_window', 'res_model': 'woow.document.share.wizard',
                'name': _('Share folder'), 'view_mode': 'form', 'target': 'new',
                'context': {'default_share_type': 'folder', 'default_folder_id': self.id}}
