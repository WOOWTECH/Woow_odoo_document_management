# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Shared constants (IF-CONST).

Pure Python, no Odoo import. Written with final values on day 0 by ci-sweep and owned by
core-models-acl afterwards (IC-R02). These are not stubs.

Note: ``services/locks.py`` has its own ``RANK`` (lock acquisition order). Always reference
these as ``constants.RANK`` / ``locks.RANK``.
"""
from __future__ import annotations

RANK: dict[str, int] = {'read': 1, 'upload': 2, 'edit': 3, 'manage': 4}
LEVELS: tuple[str, ...] = ('read', 'upload', 'edit', 'manage')
PORTAL_LEVELS: tuple[str, ...] = ('read', 'upload')
SUBJECT_TYPES: tuple[str, ...] = ('user', 'group', 'partner')

EVENTS: tuple[str, ...] = (  # 12 events, fixed order (ER-37)
    'view', 'download', 'zip', 'upload', 'replace', 'trash', 'restore', 'purge', 'move',
    'access_change', 'legal_hold', 'settings_change',
)
CHANNELS: tuple[str, ...] = ('backend', 'portal', 'rpc', 'cron')
RESULTS: tuple[str, ...] = ('ok',)

WOOW_MODELS: tuple[str, ...] = (
    'woow.document', 'woow.document.folder', 'woow.document.folder.access',
    'woow.document.tag', 'woow.document.log',
)
TRASHABLE_MODELS: tuple[str, ...] = ('woow.document', 'woow.document.folder')   # I-04

# context keys
CTX_LOG_CHANNEL = 'woow_log_channel'       # only 'backend' or 'rpc'; any other value is ignored
CTX_LEGAL_HOLD = 'woow_legal_hold'         # default for action_set_legal_hold(hold=None)
CTX_NOTICE_URL = 'woow_notice_url'         # member notice mail template
CTX_NOTICE_NOTE = 'woow_notice_note'

# limits and numbers
MIB = 1024 * 1024
UPLOAD_HARD_CAP_MB = 95
MAX_UPLOAD_BODY = 95 * 1024 * 1024         # 99_614_720 (ER-03, ER-47.2)
MAX_ACCESS_ROWS_PER_FOLDER = 500
TREE_QUERY_LIMIT = 1000
PORTAL_PAGE_SIZE = 20
ZIP_DEADLINE_S = 90
ZIP_SPOOL_MAX_BYTES = 32 * MIB
PIXEL_LIMIT = 50_000_000
FILENAME_MAX_CHARS = 200
PORTAL_DESCRIPTION_MAX_CHARS = 2000        # description of a file that a portal contact sends
SEGMENT_MAX_BYTES = 255
URL_MAX_LEN = 2048
USER_AGENT_MAX = 256
LOCK_TIMEOUT_MS = 5000
MAIL_PER_USER_PER_HOUR = 100               # most recipients of one folder notice
PURGE_BATCH = 200
LOG_RETENTION_BATCH = 5000
POISON_ALERT_DAYS = 3
