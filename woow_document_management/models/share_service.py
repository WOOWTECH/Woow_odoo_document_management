# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Share link service (IF-SHARE, owned by share). S2 lives in ``_resolve_request_access`` (ER-44).

Day-0 stub, fail closed: nothing resolves, nothing validates, no link is issued.
The stub contains no sudo at all.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class WoowDocumentShareService(models.Model):
    _inherit = 'woow.document.share'          # no _name on purpose (check_sudo, ER-44)

    download_count = fields.Integer(compute='_compute_download_count')

    def _compute_download_count(self):
        """Counted from ok share_download log rows in the user env. Day-0 stub: 0."""
        for share in self:  # WOOW-STUB
            share.download_count = 0

    @api.model
    def _issue(self, vals):
        """Create a link and return (share, plaintext token); the token exists only in the result."""
        raise NotImplementedError  # WOOW-STUB

    def _check_create_rights(self, vals):
        """DEC-09 creator checks. Day-0 stub: always refuse."""
        raise AccessError(_('You cannot create share links.'))  # WOOW-STUB

    @api.model
    def _resolve_request_access(self, token=None, share_id=None):
        """S2: return (share_sudo, scope_env), or (empty recordset, None) when not found."""
        return self.browse(), None  # WOOW-STUB

    def _validate_for_access(self, scope_env):
        """Re-validate a link for one request; never raises. Day-0 stub: False."""
        return False  # WOOW-STUB

    def _scope_domain(self):
        """Document domain of the link scope (arch section 8.5 step 6)."""
        raise NotImplementedError  # WOOW-STUB

    def _scope_documents(self, scope_env, extra_domain=None):
        """Documents in scope, searched in the creator env. Day-0 stub: none."""
        return self.env['woow.document'].browse()  # WOOW-STUB

    def _woow_link_url(self, token):
        """Public link: configured base URL or get_base_url(), then '/woow-docs/s#' + token."""
        raise NotImplementedError  # WOOW-STUB

    def _woow_send_link(self, token, note):
        """Queue the link mail (force_send=False) and log share_send."""
        raise NotImplementedError  # WOOW-STUB

    def action_revoke(self):
        """Revoke the link."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB

    def action_reissue(self):
        """Revoke the link and issue a new one with the same scope."""
        self.check_access('write')
        raise NotImplementedError  # WOOW-STUB

    def _maintenance_run(self):
        """Hourly: expire, clean up old links and failed mails (arch section 8.6)."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        return None  # WOOW-STUB


class WoowDocumentShareButton(models.Model):
    _inherit = 'woow.document'

    def action_open_share_wizard(self):
        """Open the share link wizard for the selected documents."""
        self.check_access('read')
        raise NotImplementedError  # WOOW-STUB


class WoowDocumentFolderShareButton(models.Model):
    _inherit = 'woow.document.folder'

    def action_open_share_wizard(self):
        """Open the share link wizard for the folder."""
        self.check_access('read')
        raise NotImplementedError  # WOOW-STUB
