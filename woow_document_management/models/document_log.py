# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Append-only audit events."""
from itertools import islice

from odoo import api, fields, models
from odoo.http import request

from ..services.constants import CHANNELS, EVENTS, RESULTS
from ..services.client_ip import hmac_ip, request_client_ip, truncate_ip

_MANAGER = 'woow_document_management.group_woow_document_manager'
USER_AGENT_SIZE = 256
# Longest id list one audit row may carry: a folder has at most this many members.
DETAIL_LIST_MAX = 500
_EVENT_LABELS = {
    'view': 'Previewed', 'download': 'Downloaded', 'zip': 'Downloaded as ZIP',
    'upload': 'Uploaded', 'replace': 'File replaced', 'trash': 'Moved to trash',
    'restore': 'Restored', 'purge': 'Deleted permanently', 'move': 'Moved',
    'access_change': 'Members changed', 'legal_hold': 'Legal hold changed',
    'settings_change': 'Settings changed',
}


class WoowDocumentLog(models.Model):
    _name = 'woow.document.log'
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'Document audit log'
    _order = 'id desc'
    # Only the server writes a row: no column takes a stored default (a row is made with
    # ``sudo()``, which keeps the uid of the caller and so his defaults).
    _woow_own_default_fields = frozenset({
        'event', 'result', 'channel', 'user_id', 'partner_id', 'document_id', 'folder_id',
        'company_id', 'ip_display', 'ip_hash', 'user_agent', 'detail'})

    event = fields.Selection([(e, _EVENT_LABELS[e]) for e in EVENTS], required=True, index=True)
    result = fields.Selection([(r, r.capitalize()) for r in RESULTS], required=True,
                              default='ok')
    channel = fields.Selection([(c, c.capitalize()) for c in CHANNELS], required=True)
    user_id = fields.Many2one('res.users', ondelete='set null', index=True)
    partner_id = fields.Many2one('res.partner', ondelete='set null', index=True)
    document_id = fields.Many2one('woow.document', ondelete='set null', index=True)
    folder_id = fields.Many2one('woow.document.folder', ondelete='set null', index=True)
    company_id = fields.Many2one('res.company', ondelete='set null', index=True)
    ip_display = fields.Char(size=64, groups=_MANAGER)
    ip_hash = fields.Char(size=64, index=True, groups=_MANAGER)
    user_agent = fields.Char(size=256, groups=_MANAGER)
    detail = fields.Json()

    @api.model
    def _event_values(self, event, *, channel, user_id=None, partner_id=None, document_id=None,
                      folder_id=None, company_id=None, ip=None, user_agent=None, detail=None):
        """Check one event and return its bounded column values (see ``_log_event``)."""
        if event not in EVENTS or channel not in CHANNELS:
            raise ValueError('invalid audit event')
        detail = detail or {}
        if not isinstance(detail, dict) or any(
                not isinstance(key, str) or not key.isidentifier() or len(key) > 32 or
                not (type(value) in (int, bool) or
                     isinstance(value, list) and len(value) <= DETAIL_LIST_MAX and
                     all(type(item) is int for item in value))
                for key, value in detail.items()):
            raise ValueError('audit detail must contain only numeric values')
        for value in (user_id, partner_id, document_id, folder_id):
            if value is not None and type(value) is not int:
                raise ValueError('audit identifiers must be integers')
        if company_id is not None and company_id is not False and type(company_id) is not int:
            raise ValueError('audit identifiers must be integers')
        settings = self.env['woow.document.settings']._get_settings()
        if ip is None and request:
            ip = request_client_ip(request.httprequest, settings)
        if user_agent is None and request:
            user_agent = str(request.httprequest.user_agent)
        # The header is client data. PostgreSQL text cannot hold NUL: written as is,
        # it would make the write fail and roll back the request. Other control
        # characters are dropped as well.
        user_agent = ''.join(islice(filter(str.isprintable, str(user_agent or '')),
                                    USER_AGENT_SIZE))
        ip_hash = hmac_ip(self.env['woow.document.settings']._audit_ip_key(), ip) if ip else None
        ip_display = (truncate_ip(ip) if settings.log_ip_mode == 'truncate' else
                      ip_hash if settings.log_ip_mode == 'hash' else None)
        user_id = (None if self.env.user._is_public() else self.env.uid) if user_id is None else user_id
        # None names the current company; False (what the empty company of a folder reads
        # as) is a record that belongs to no company, kept with an empty company so that
        # every company's administrators may read it (R-L0).
        company_id = self.env.company.id if company_id is None else (company_id or None)
        return {'event': event, 'result': 'ok', 'channel': channel,
                'user_id': user_id, 'partner_id': partner_id, 'document_id': document_id,
                'folder_id': folder_id, 'company_id': company_id, 'ip_hash': ip_hash,
                'ip_display': ip_display, 'user_agent': user_agent, 'detail': detail}

    @api.model
    def _log_event(self, event, *, channel, user_id=None, partner_id=None, document_id=None,
                   folder_id=None, company_id=None, ip=None, user_agent=None, detail=None):
        """Write a bounded event without storing names, addresses or credentials.

        ``company_id`` is None for the current company, False for a record without a
        company (stored as no company), or an id. The user agent is stored without control
        characters, cut to USER_AGENT_SIZE characters. Returns 1.
        """
        vals = self._event_values(
            event, channel=channel, user_id=user_id, partner_id=partner_id,
            document_id=document_id, folder_id=folder_id, company_id=company_id, ip=ip,
            user_agent=user_agent, detail=detail)
        self.sudo().create(vals)  # sudo-ok: S1
        return 1
