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

EVENTS: tuple[str, ...] = (  # 21 events, fixed order (ER-37)
    'view', 'download', 'zip', 'upload', 'replace', 'trash', 'restore', 'purge', 'move',
    'access_change', 'legal_hold', 'share_create', 'share_send', 'share_open', 'share_access',
    'share_download', 'share_unlock_failed', 'share_revoke', 'share_expire', 'settings_change',
    'rate_limited',
)
CHANNELS: tuple[str, ...] = ('backend', 'portal', 'share', 'rpc', 'cron')
RESULTS: tuple[str, ...] = ('ok', 'denied')

WOOW_MODELS: tuple[str, ...] = (
    'woow.document', 'woow.document.folder', 'woow.document.folder.access',
    'woow.document.tag', 'woow.document.share', 'woow.document.log',
)
TRASHABLE_MODELS: tuple[str, ...] = ('woow.document', 'woow.document.folder')   # I-04

# context keys
CTX_LOG_CHANNEL = 'woow_log_channel'       # only 'backend' or 'rpc'; any other value is ignored
CTX_LEGAL_HOLD = 'woow_legal_hold'         # default for action_set_legal_hold(hold=None)
CTX_SHARE_URL = 'woow_share_url'           # share link mail template
CTX_SHARE_NOTE = 'woow_share_note'
CTX_NOTICE_URL = 'woow_notice_url'         # member notice mail template
CTX_NOTICE_NOTE = 'woow_notice_note'
SESSION_GRANTS_KEY = 'woow_doc_grants'     # session grant namespace (share lane)

# limits and numbers
MIB = 1024 * 1024
UPLOAD_HARD_CAP_MB = 95
MAX_UPLOAD_BODY = 95 * 1024 * 1024         # 99_614_720 (ER-03, ER-47.2)
MAX_DENIED_ROWS_PER_MIN = 1024
MAX_GRANTS = 20
MAX_RECIPIENTS = 20
MAX_SHARE_DOCUMENTS = 500
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
LINK_MAX_LEN = 2048                        # ER-12
USER_AGENT_MAX = 256
LOCK_TIMEOUT_MS = 5000
PASSWORD_MIN, PASSWORD_MAX = 8, 128
PASSWORD_ROUNDS_MAX = 200_000              # most rounds of a stored password hash that are run
SHARE_UNLOCK_FAIL_PER_HOUR = 50
SHARE_UNLOCK_LOCK_MINUTES = 60
MAIL_PER_USER_PER_HOUR = 100
FAILED_MAIL_RETENTION_HOURS = 24
SHARE_RECORD_RETENTION_DAYS = 90
PURGE_BATCH = 200
LOG_RETENTION_BATCH = 5000
POISON_ALERT_DAYS = 3
