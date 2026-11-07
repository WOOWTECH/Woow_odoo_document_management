# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Settings page fields (IF-SET, owned by settings-ip).

Day-0 stub: fields are declared; saving is refused (only base.group_system may ever save, ER-14).
"""
from odoo import _, fields, models
from odoo.exceptions import AccessError

_P = 'woow_document_management.'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    woow_doc_trash_retention_days = fields.Integer(config_parameter=_P + 'trash_retention_days')
    woow_doc_share_default_days = fields.Integer(config_parameter=_P + 'share_default_days')
    woow_doc_share_max_days = fields.Integer(config_parameter=_P + 'share_max_days')
    woow_doc_share_link_policy = fields.Selection(
        [('folder_managers', 'Folder managers'), ('managers_only', 'Document administrators only')],
        config_parameter=_P + 'share_link_policy')
    woow_doc_share_require_password = fields.Boolean(config_parameter=_P + 'share_require_password')
    woow_doc_share_session_minutes = fields.Integer(config_parameter=_P + 'share_session_minutes')
    woow_doc_share_unlock_max_fail = fields.Integer(config_parameter=_P + 'share_unlock_max_fail')
    woow_doc_share_unlock_cooldown_min = fields.Integer(
        config_parameter=_P + 'share_unlock_cooldown_min')
    woow_doc_denied_rate_per_min = fields.Integer(config_parameter=_P + 'denied_rate_per_min')
    woow_doc_share_link_base_url = fields.Char(config_parameter=_P + 'share_link_base_url')
    woow_doc_allow_http_links = fields.Boolean(config_parameter=_P + 'allow_http_links')
    woow_doc_portal_max_upload_mb = fields.Integer(config_parameter=_P + 'portal_max_upload_mb')
    woow_doc_internal_max_upload_mb = fields.Integer(config_parameter=_P + 'internal_max_upload_mb')
    woow_doc_max_files_per_upload = fields.Integer(config_parameter=_P + 'max_files_per_upload')
    woow_doc_extra_extensions = fields.Char(config_parameter=_P + 'extra_extensions')
    woow_doc_zip_max_files = fields.Integer(config_parameter=_P + 'zip_max_files')
    woow_doc_zip_max_mb = fields.Integer(config_parameter=_P + 'zip_max_mb')
    woow_doc_zip_max_depth = fields.Integer(config_parameter=_P + 'zip_max_depth')
    woow_doc_zip_concurrency = fields.Integer(config_parameter=_P + 'zip_concurrency')
    woow_doc_quota_mb = fields.Integer(config_parameter=_P + 'quota_mb')
    woow_doc_quota_warn_pct = fields.Integer(config_parameter=_P + 'quota_warn_pct')
    woow_doc_quota_block_pct = fields.Integer(config_parameter=_P + 'quota_block_pct')
    woow_doc_log_retention_days = fields.Integer(config_parameter=_P + 'log_retention_days')
    woow_doc_log_ip_mode = fields.Selection(
        [('truncate', 'Shortened address'), ('hash', 'Keyed hash only'), ('none', 'Not shown')],
        config_parameter=_P + 'log_ip_mode')
    woow_doc_allow_root_create = fields.Boolean(config_parameter=_P + 'allow_root_create')
    woow_doc_tag_create_policy = fields.Selection(
        [('managers', 'Document administrators'), ('users', 'All document users')],
        config_parameter=_P + 'tag_create_policy')
    woow_doc_mail_subject_include_name = fields.Boolean(
        config_parameter=_P + 'mail_subject_include_name')
    woow_doc_trusted_proxies = fields.Char(config_parameter=_P + 'trusted_proxies')
    woow_doc_client_ip_header = fields.Char(config_parameter=_P + 'client_ip_header')
    woow_doc_uninstall_confirm = fields.Char(config_parameter=_P + 'uninstall_confirm')

    def set_values(self):
        """Only base.group_system saves; values are range-checked and logged (settings_change)."""
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only system administrators can change these settings.'))
        raise NotImplementedError  # WOOW-STUB
