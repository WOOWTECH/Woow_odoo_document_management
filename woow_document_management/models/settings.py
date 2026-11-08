# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Settings reader (IF-SET, owned by settings-ip). S3 lives in ``_read_params``.

Day-0 stub: ``_get_settings`` returns the defaults of the design, ``_read_params`` reads
nothing and ``_share_keys`` is not available.
"""
from __future__ import annotations

import dataclasses

from odoo import models

PARAM_PREFIX = 'woow_document_management.'
_PARAM_KEYS: tuple[str, ...] = (
    'trash_retention_days', 'share_default_days', 'share_max_days', 'share_link_policy',
    'share_require_password', 'share_session_minutes', 'share_unlock_max_fail',
    'share_unlock_cooldown_min', 'denied_rate_per_min', 'share_link_base_url', 'allow_http_links',
    'portal_max_upload_mb', 'internal_max_upload_mb', 'max_files_per_upload', 'extra_extensions',
    'zip_max_files', 'zip_max_mb', 'zip_max_depth', 'zip_concurrency', 'quota_mb',
    'quota_warn_pct', 'quota_block_pct', 'log_retention_days', 'log_ip_mode', 'allow_root_create',
    'tag_create_policy', 'mail_subject_include_name', 'trusted_proxies', 'client_ip_header',
    'uninstall_confirm',
)
_READABLE_KEYS: frozenset[str] = frozenset(PARAM_PREFIX + k for k in _PARAM_KEYS) | {'database.secret'}


@dataclasses.dataclass(frozen=True)
class WoowDocSettings:
    """Typed, range-checked settings. Field name = parameter key without prefix."""
    trash_retention_days: int = 30
    share_default_days: int = 14
    share_max_days: int = 90
    share_link_policy: str = 'folder_managers'
    share_require_password: bool = False
    share_session_minutes: int = 60
    share_unlock_max_fail: int = 5
    share_unlock_cooldown_min: int = 15
    denied_rate_per_min: int = 10
    share_link_base_url: str = ''
    allow_http_links: bool = False
    portal_max_upload_mb: int = 25
    internal_max_upload_mb: int = 95
    max_files_per_upload: int = 10
    extra_extensions: tuple = ()
    zip_max_files: int = 2000
    zip_max_mb: int = 500
    zip_max_depth: int = 20
    zip_concurrency: int = 2
    quota_mb: int = 0
    quota_warn_pct: int = 80
    quota_block_pct: int = 95
    log_retention_days: int = 365
    log_ip_mode: str = 'truncate'
    allow_root_create: bool = False
    tag_create_policy: str = 'managers'
    mail_subject_include_name: bool = False
    trusted_proxies: tuple = ()
    client_ip_header: str = ''
    uninstall_confirm: str = ''


@dataclasses.dataclass(frozen=True)
class ShareKeys:
    k_share: bytes   # HMAC-SHA256(database.secret, b"woow_document_management/share-token/v1")
    k_hint: bytes    # same, label b"woow_document_management/share-hint/v1"
    k_ip: bytes      # same, label b"woow_document_management/audit-ip/v1"


class WoowDocumentSettings(models.AbstractModel):
    _name = 'woow.document.settings'
    _description = 'Document management settings reader'

    def _read_params(self, keys):
        """S3. Keys outside _READABLE_KEYS -> ValueError. Returns raw strings only."""
        return {}  # WOOW-STUB

    def _get_settings(self) -> WoowDocSettings:
        """Type conversion and range checks; any failing key falls back to its safe value."""
        return WoowDocSettings()  # WOOW-STUB

    def _share_keys(self) -> ShareKeys:
        """Derive the HMAC keys in memory; empty or unreadable database.secret -> ValueError."""
        raise NotImplementedError  # WOOW-STUB
