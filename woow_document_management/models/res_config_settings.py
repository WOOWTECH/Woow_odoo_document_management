# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""System administrator settings form for document management (IF-SET)."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .settings import PARAM_PREFIX, _PARAM_KEYS, parse_settings

_P = 'woow_document_management.'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    woow_doc_trash_retention_days = fields.Integer(default=30, config_parameter=_P + 'trash_retention_days')
    woow_doc_notice_base_url = fields.Char(config_parameter=_P + 'notice_base_url')
    woow_doc_allow_http_links = fields.Boolean(config_parameter=_P + 'allow_http_links')
    woow_doc_portal_max_upload_mb = fields.Integer(default=25, config_parameter=_P + 'portal_max_upload_mb')
    woow_doc_internal_max_upload_mb = fields.Integer(default=95, config_parameter=_P + 'internal_max_upload_mb')
    woow_doc_max_files_per_upload = fields.Integer(default=10, config_parameter=_P + 'max_files_per_upload')
    woow_doc_extra_extensions = fields.Char(config_parameter=_P + 'extra_extensions')
    woow_doc_zip_max_files = fields.Integer(default=2000, config_parameter=_P + 'zip_max_files')
    woow_doc_zip_max_mb = fields.Integer(default=500, config_parameter=_P + 'zip_max_mb')
    woow_doc_zip_max_depth = fields.Integer(default=20, config_parameter=_P + 'zip_max_depth')
    woow_doc_zip_concurrency = fields.Integer(default=2, config_parameter=_P + 'zip_concurrency')
    woow_doc_quota_mb = fields.Integer(default=0, config_parameter=_P + 'quota_mb')
    woow_doc_quota_warn_pct = fields.Integer(default=80, config_parameter=_P + 'quota_warn_pct')
    woow_doc_quota_block_pct = fields.Integer(default=95, config_parameter=_P + 'quota_block_pct')
    woow_doc_log_retention_days = fields.Integer(default=365, config_parameter=_P + 'log_retention_days')
    woow_doc_log_ip_mode = fields.Selection(
        [('truncate', 'Shortened address'), ('hash', 'Keyed hash only'), ('none', 'Not shown')],
        default='truncate', config_parameter=_P + 'log_ip_mode')
    woow_doc_allow_root_create = fields.Boolean(config_parameter=_P + 'allow_root_create')
    woow_doc_tag_create_policy = fields.Selection(
        [('managers', 'Document administrators'), ('users', 'All document users')],
        default='managers', config_parameter=_P + 'tag_create_policy')
    woow_doc_trusted_proxies = fields.Char(config_parameter=_P + 'trusted_proxies')
    woow_doc_client_ip_header = fields.Selection(
        [('', 'Peer address'), ('CF-Connecting-IP', 'Cloudflare address'),
         ('X-Real-IP', 'Reverse proxy address'), ('X-Forwarded-For', 'Forwarded chain')],
        default='', config_parameter=_P + 'client_ip_header')
    woow_doc_uninstall_confirm = fields.Char(config_parameter=_P + 'uninstall_confirm')

    @api.model
    def default_get(self, fields_list):
        """Only system administrators read the module settings through the form defaults.

        Core fills ``config_parameter`` fields from ``ir.config_parameter`` as the
        superuser, and ``default_get`` (also the first ``onchange`` of the form) can be
        called by any signed-in user, so the trusted-proxy list and the other values would
        be readable by portal users. Field groups do not apply to it;
        for everyone else the module's own fields are left out of the request.
        """
        if not (self.env.su or self.env.user.has_group('base.group_system')):
            fields_list = [
                name for name in fields_list or ()
                if not str(getattr(self._fields.get(name), 'config_parameter', '') or '').startswith(
                    PARAM_PREFIX)]
        return super().default_get(fields_list)

    def set_values(self):
        """Only base.group_system saves; values are range-checked and logged (settings_change)."""
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only system administrators can change these settings.'))
        self.ensure_one()
        raw = {key: self['woow_doc_' + key] for key in _PARAM_KEYS}
        try:
            parse_settings(raw, strict=True)
        except ValueError as exc:
            raise ValidationError(_('Invalid document setting: %s', str(exc))) from exc
        old = self.env['woow.document.settings']._read_params(
            PARAM_PREFIX + key for key in _PARAM_KEYS)
        changed = sum(old.get(PARAM_PREFIX + key) != str(raw[key]) for key in _PARAM_KEYS)
        result = super().set_values()
        if not raw['trash_retention_days']:
            # Core stores an integer 0 as no value and deletes the parameter, which then reads back
            # as the default of 30 days; 0 is the value that turns the automatic purge off.
            self.env['ir.config_parameter'].set_param(PARAM_PREFIX + 'trash_retention_days', '0')
        self.env['woow.document.log']._log_event(
            'settings_change', channel='backend', detail={'changed': changed})
        return result
