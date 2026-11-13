# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Allowlisted, typed document settings. S3 lives only in ``_read_params``."""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import re
from urllib.parse import urlsplit

from odoo import models

from ..services.client_ip import HEADERS, parse_trusted_proxies
from ..services.mime_map import OPTIONAL_EXTENSIONS

PARAM_PREFIX = 'woow_document_management.'
_PARAM_KEYS: tuple[str, ...] = (
    'trash_retention_days', 'notice_base_url', 'allow_http_links',
    'portal_max_upload_mb', 'internal_max_upload_mb', 'max_files_per_upload', 'extra_extensions',
    'zip_max_files', 'zip_max_mb', 'zip_max_depth', 'zip_concurrency', 'quota_mb',
    'quota_warn_pct', 'quota_block_pct', 'log_retention_days', 'log_ip_mode', 'allow_root_create',
    'tag_create_policy', 'trusted_proxies', 'client_ip_header', 'uninstall_confirm',
)
_READABLE_KEYS: frozenset[str] = frozenset(PARAM_PREFIX + k for k in _PARAM_KEYS) | {'database.secret'}
_INT_RANGES = {
    'trash_retention_days': (0, 3650), 'portal_max_upload_mb': (1, 95),
    'internal_max_upload_mb': (1, 95), 'max_files_per_upload': (1, 20),
    'zip_max_files': (1, 5000), 'zip_max_mb': (1, 2048),
    'zip_max_depth': (1, 50), 'zip_concurrency': (1, 4),
    'quota_mb': (0, 10_000_000), 'quota_warn_pct': (1, 99),
    'quota_block_pct': (2, 100), 'log_retention_days': (30, 3650),
}
_BOOL_KEYS = frozenset(('allow_http_links', 'allow_root_create'))
_CHOICES = {
    'log_ip_mode': frozenset(('truncate', 'hash', 'none')),
    'tag_create_policy': frozenset(('managers', 'users')),
    'client_ip_header': frozenset(HEADERS),
}


@dataclasses.dataclass(frozen=True)
class WoowDocSettings:
    """Typed, range-checked settings. Field name = parameter key without prefix."""
    trash_retention_days: int = 30
    notice_base_url: str = ''
    allow_http_links: bool = False
    portal_max_upload_mb: int = 25
    internal_max_upload_mb: int = 95
    max_files_per_upload: int = 10
    extra_extensions: tuple[str, ...] = ()
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
    trusted_proxies: tuple = ()
    client_ip_header: str = ''
    uninstall_confirm: str = ''


def _parse_value(key, raw):
    if key in _INT_RANGES:
        if isinstance(raw, bool):
            raise ValueError(key)
        value = int(raw)
        low, high = _INT_RANGES[key]
        if not low <= value <= high:
            raise ValueError(key)
        return value
    if key in _BOOL_KEYS:
        if raw in (True, 'True', 'true', '1', 1):
            return True
        if raw in (False, 'False', 'false', '0', 0):
            return False
        raise ValueError(key)
    if key in _CHOICES:
        if key == 'client_ip_header' and raw is False:
            raw = ''
        if raw not in _CHOICES[key]:
            raise ValueError(key)
        return raw
    if key == 'notice_base_url':
        value = str(raw or '').strip().rstrip('/')
        if value:
            url = urlsplit(value)
            if url.scheme != 'https' or not url.hostname or url.username or url.password \
                    or url.path not in ('', '/') or url.query or url.fragment:
                raise ValueError(key)
            try:
                port = url.port
            except ValueError:
                raise ValueError(key) from None
            if port is not None and not 1 <= port <= 65535:
                raise ValueError(key)
        return value
    if key == 'extra_extensions':
        parts = tuple(sorted(set(re.split(r'[,\s]+', str(raw or '').strip())) - {''}))
        if not set(parts) <= OPTIONAL_EXTENSIONS:
            raise ValueError(key)
        return parts
    if key == 'trusted_proxies':
        value = str(raw or '').strip()
        parsed = parse_trusted_proxies(value)
        if value and not parsed:
            raise ValueError(key)
        return parsed
    if key == 'uninstall_confirm':
        return str(raw or '')
    raise ValueError('unknown setting key')


def parse_settings(raw, *, strict=False):
    """Convert every parameter; strict mode is used before a settings form is saved."""
    defaults = WoowDocSettings()
    values = {}
    for key in _PARAM_KEYS:
        value = raw.get(key)
        if value is None:
            values[key] = getattr(defaults, key)
            continue
        try:
            values[key] = _parse_value(key, value)
        except (ValueError, TypeError):
            if strict:
                raise ValueError(key) from None
            values[key] = getattr(defaults, key)
    if values['quota_block_pct'] <= values['quota_warn_pct']:
        if strict:
            raise ValueError('quota_block_pct')
        values['quota_warn_pct'] = defaults.quota_warn_pct
        values['quota_block_pct'] = defaults.quota_block_pct
    return WoowDocSettings(**values)


class WoowDocumentSettings(models.AbstractModel):
    _name = 'woow.document.settings'
    _description = 'Document management settings reader'

    def _read_params(self, keys):
        """S3. Keys outside _READABLE_KEYS -> ValueError. Returns raw strings only."""
        requested = tuple(keys)
        if any(key not in _READABLE_KEYS for key in requested):
            raise ValueError('unregistered setting key')
        params = self.env['ir.config_parameter'].sudo()  # sudo-ok: S3
        return {key: value if isinstance(value, str) else None
                for key in requested if (value := params.get_param(key)) is not None}

    def _get_settings(self) -> WoowDocSettings:
        """Type conversion and range checks; any failing key falls back to its safe value."""
        raw = self._read_params(PARAM_PREFIX + key for key in _PARAM_KEYS)
        return parse_settings({key: raw.get(PARAM_PREFIX + key) for key in _PARAM_KEYS})

    def _audit_ip_key(self) -> bytes:
        """HMAC key of the audit IP hashes, derived in memory from database.secret.

        HMAC-SHA256(database.secret, b"woow_document_management/audit-ip/v1"); an empty or
        unreadable database.secret -> ValueError.
        """
        secret = self._read_params(('database.secret',)).get('database.secret')
        if not secret:
            raise ValueError('database.secret is unavailable')
        return hmac.new(secret.encode('utf-8'), b'woow_document_management/audit-ip/v1',
                        hashlib.sha256).digest()
