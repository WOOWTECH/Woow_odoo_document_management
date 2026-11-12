# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Storage quota (IF-QUOTA, owned by upload-quota). Q2 and Q5 live here.

The quota check of uploads (one measurement and one lock per run) and the warning cron.
"""
import math

from psycopg2 import OperationalError
from psycopg2.pool import PoolError

from odoo import _, fields, models
from odoo.exceptions import AccessError

from .upload_policy import WoowUploadRejected
from ..services.constants import MIB
from ..services.locks import LockBusy, lock
from ..services.locks import try_lock

# Context key of the QuotaLedger that several calls of one run share.
LEDGER_KEY = 'woow_quota_ledger'


class QuotaLedger:
    """What one run of uploads has measured and accepted, for the calls that make it up.

    A run that stores files with several ``create`` or ``write`` calls in one transaction
    (the attachment import wizard) measures the stored bytes once, after it took the quota
    lock, and adds the bytes it accepted since: the files it has written are not
    committed, so no measurement made through another connection counts them. The run
    holds the lock until its transaction ends, so no other upload can change the stored
    bytes meanwhile.
    """
    __slots__ = ('base', 'added')

    def __init__(self):
        self.base = None
        self.added = 0


class WoowDocumentQuota(models.AbstractModel):
    _name = 'woow.document.quota'
    _description = 'Document storage quota'

    def _usage_bytes(self) -> int:
        """Q2: deduplicated bytes of all stored attachments that have been committed.

        The count is read through a connection of its own, which takes its snapshot when
        it runs. The caller's transaction took its snapshot at its first statement, long
        before it got the quota lock, so it would miss the uploads that committed while
        it waited; reading it there made every concurrent upload fail with a
        serialization error and retry after a pause instead. What the caller itself has
        written is not committed: see ``QuotaLedger``. When no connection is available
        the caller is told to try again (``LockBusy``).
        """
        self.env['ir.attachment'].flush_model(['store_fname', 'file_size'])
        try:
            side = self.env.registry.cursor()
        except (PoolError, OperationalError) as exc:
            raise LockBusy('quota') from exc
        with side as cr:
            cr.execute('''
                SELECT COALESCE(SUM(file_size), 0)
                FROM (SELECT DISTINCT ON (store_fname) file_size FROM ir_attachment
                      WHERE store_fname IS NOT NULL) AS stored
            ''')  # sql-ok: Q2
            return int(cr.fetchone()[0] or 0)

    def _bytes_if_new(self, checksum, size) -> int:
        """Q5: 0 when a stored attachment with this checksum exists, else ``size``."""
        self.env['ir.attachment'].flush_model(['checksum', 'store_fname'])
        self.env.cr.execute('''
            SELECT 1 FROM ir_attachment
            WHERE checksum = %s AND store_fname IS NOT NULL LIMIT 1
        ''', (checksum,))  # sql-ok: Q5
        return 0 if self.env.cr.fetchone() else size

    def _check(self, checksum, size) -> None:
        """Refuse an upload above quota_block_pct (see ``_check_many``)."""
        self._check_many([(checksum, size)])

    def _check_many(self, items) -> None:
        """Refuse uploads above quota_block_pct under lock('quota'); LockBusy on timeout.

        ``items`` are the (checksum, size) of all the files one call stores; they are
        measured together, in order, so that each counts the ones before it. The checks
        run last, after everything else that can refuse a file, and the lock is held to
        the end of the transaction, so the whole call takes it once and measures the
        stored bytes once (``QuotaLedger``).

        An account has one upload transaction at a time: its other requests wait for
        its own lock and not for the site lock, so one account cannot queue its requests
        in front of everyone else's.

        A new upload always counts its own size. Only the attachment import wizard,
        whose source the caller has read, is credited for content already stored:
        crediting by a site-wide checksum match would tell any uploader whether a
        file they cannot read exists (accepted or refused, and the percentage).
        The exact percentage is only shown to document administrators.
        """
        from .document import SOURCE_CTX_KEY, _SOURCE_SENTINEL
        settings = self.env['woow.document.settings']._get_settings()
        if not settings.quota_mb or not items:
            return
        self.env['ir.attachment'].flush_model(['store_fname', 'file_size', 'checksum'])
        lock(self.env.cr, 'quota:u:%d' % self.env.uid)
        lock(self.env.cr, 'quota')
        ledger = self.env.context.get(LEDGER_KEY)
        if not isinstance(ledger, QuotaLedger):
            ledger = QuotaLedger()
        if ledger.base is None:
            ledger.base = self._usage_bytes()
        credited = self.env.context.get(SOURCE_CTX_KEY) is _SOURCE_SENTINEL
        capacity = settings.quota_mb * MIB
        projected = ledger.base + ledger.added
        for checksum, size in items:
            projected += self._bytes_if_new(checksum, size) if credited else size
            if projected * 100 > capacity * settings.quota_block_pct:
                if self.env.su or self.env.user.has_group(
                        'woow_document_management.group_woow_document_manager'):
                    raise WoowUploadRejected('quota', pct=math.ceil(projected * 100 / capacity))
                raise WoowUploadRejected('quota')
        ledger.added = projected - ledger.base

    def _cron_check_warn(self) -> None:
        """Daily: activity for document administrators when usage reaches quota_warn_pct."""
        if not self.env.su:
            raise AccessError(_('Scheduled actions only.'))
        settings = self.env['woow.document.settings']._get_settings()
        if not settings.quota_mb:
            return
        # Serialise cron runs without losing the warning when an upload holds quota.
        if not try_lock(self.env.cr, 'quota:warn'):
            return
        used = self._usage_bytes()
        capacity = settings.quota_mb * MIB
        if used * 100 < capacity * settings.quota_warn_pct:
            return
        pct = math.ceil(used * 100 / capacity)
        today = fields.Date.context_today(self)
        summary = _('Document storage capacity warning')
        model = self.env.ref('base.model_res_partner')
        activity_type = self._quota_activity_type()
        group = self.env.ref('woow_document_management.group_woow_document_manager')
        activities = self.env['mail.activity']
        for manager in group.users.filtered('active'):
            partner = manager.partner_id
            if not partner:
                continue
            exists = activities.with_context(active_test=False).search_count([
                ('res_model_id', '=', model.id), ('res_id', '=', partner.id),
                ('user_id', '=', manager.id), ('activity_type_id', '=', activity_type.id),
                ('date_deadline', '=', today), ('summary', '=', summary),
            ])
            if exists:
                continue
            activities.create({
                'res_model_id': model.id, 'res_id': partner.id,
                'user_id': manager.id, 'activity_type_id': activity_type.id,
                'date_deadline': today, 'summary': summary,
                'note': _('Document storage is %(pct)s%% full.', pct=pct),
            })

    def _quota_activity_type(self):
        """Keep completed warnings so the same-day cron can see its prior work."""
        xmlid = 'woow_document_management.activity_type_quota_warning'
        activity_type = self.env.ref(xmlid, raise_if_not_found=False)
        if activity_type:
            return activity_type
        activity_type = self.env['mail.activity.type'].create({
            'name': 'Document storage warning',
            'summary': 'Document storage capacity warning',
            'res_model': 'res.partner',
            'keep_done': True,
        })
        self.env['ir.model.data'].create({
            'module': 'woow_document_management',
            'name': 'activity_type_quota_warning',
            'model': 'mail.activity.type',
            'res_id': activity_type.id,
            'noupdate': True,
        })
        return activity_type
