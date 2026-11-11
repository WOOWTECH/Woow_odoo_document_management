# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Public share routes; token exchange never changes the login identity."""
import hmac
import time
from datetime import timedelta, timezone
from typing import NamedTuple

from odoo import fields, http
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.http import Response, request

from ..services import tokens
from ..services.client_ip import hmac_ip, request_client_ip
from ..services.constants import (MAX_GRANTS, SESSION_GRANTS_KEY,
                                 SHARE_UNLOCK_FAIL_PER_HOUR)
from ..services.locks import LockBusy, lock
from ..services.zip_builder import ZipLimitExceeded
from . import streaming


class ShareAccess(NamedTuple):
    share_sudo: object
    scope_env: object
    ip: str
    ip_hash: str
    grant: object


def _save_grant(share, grant):
    now = int(time.time())
    grants = {key: value for key, value in dict(request.session.get(SESSION_GRANTS_KEY) or {}).items()
              if isinstance(value, dict) and type(value.get('exp')) is int and value['exp'] > now}
    grants[str(share.id)] = grant
    while len(grants) > MAX_GRANTS:
        grants.pop(next(iter(grants)))
    request.session[SESSION_GRANTS_KEY] = grants
    request.session.should_rotate = True


class WoowDocShareController(http.Controller):

    def _denied(self, ip, share_id=None, event='share_access'):
        log = request.env['woow.document.log']
        if log._count_denied_across_shares(event, ip) >= request.env[
                'woow.document.settings']._get_settings().denied_rate_per_min:
            log._log_event('rate_limited', result='denied', channel='share', ip=ip)
            return streaming.too_many_requests()
        written = log._log_event(
            event, result='denied', channel='share', share_id=share_id, ip=ip)
        if not written:
            return streaming.too_many_requests()
        count = log._count_denied_across_shares(event, ip)
        if count > request.env['woow.document.settings']._get_settings().denied_rate_per_min:
            return streaming.too_many_requests()
        return streaming.uniform_404('share')

    def _share_entry(self, *, token=None, share_id=None, require_grant=True,
                     require_unlocked=True, resolve=True):
        """Resolve a valid link and short-lived session grant, or return a refusal Response."""
        if not resolve:
            return ShareAccess(request.env['woow.document.share'].browse(), None, '', '', None)
        try:
            if request.env['woow.document.log']._denied_budget_exhausted():
                return streaming.too_many_requests()
            settings = request.env['woow.document.settings']._get_settings()
            ip = request_client_ip(request.httprequest, settings)
            ip_hash = hmac_ip(request.env['woow.document.settings']._share_keys().k_ip, ip)
            if token is not None:
                code = tokens.extract_code(token)
                if code is None:
                    return self._denied(ip)
                share_sudo, scope_env = request.env['woow.document.share']._resolve_request_access(token=code)
            else:
                share_sudo, scope_env = request.env['woow.document.share']._resolve_request_access(
                    share_id=share_id)
            if not share_sudo or not share_sudo._validate_for_access(scope_env):
                return self._denied(ip)
            grant = None
            if require_grant:
                grant = dict(request.session.get(SESSION_GRANTS_KEY) or {}).get(str(share_sudo.id))
                if not isinstance(grant, dict) or type(grant.get('exp')) is not int \
                        or grant['exp'] <= int(time.time()) or not isinstance(grant.get('h'), str) \
                        or not hmac.compare_digest(grant['h'], share_sudo.token_hash[:16]):
                    return self._denied(ip, share_sudo.id)
                if require_unlocked and share_sudo.has_password and grant.get('pw') is not True:
                    return self._denied(ip, share_sudo.id)
            return ShareAccess(share_sudo, scope_env, ip, ip_hash, grant)
        except (AccessError, MissingError, UserError, ValueError, TypeError):
            return streaming.uniform_404('share')

    @http.route('/woow-docs/s', type='http', auth='public', methods=['GET'])
    def share_landing(self, **kw):
        return streaming.apply_common_headers(
            request.render('woow_document_management.share_landing', {}), 'share')

    @http.route('/woow-docs/s/open', type='http', auth='public', methods=['POST'],
                max_content_length=4096)
    def share_open(self, **post):
        access = self._share_entry(token=post.get('token') or post.get('link') or '',
                                   require_grant=False)
        if isinstance(access, Response):
            return access
        share = access.share_sudo
        expiry = min(int(time.time()) + request.env[
            'woow.document.settings']._get_settings().share_session_minutes * 60,
            int(fields.Datetime.to_datetime(share.expire_at).replace(tzinfo=timezone.utc).timestamp()))
        _save_grant(share, {'exp': expiry, 'h': share.token_hash[:16], 'pw': False})
        access.scope_env['woow.document.log']._log_event(
            'share_open', channel='share', share_id=share.id,
            company_id=share.company_id.id, ip=access.ip)
        return streaming.apply_common_headers(
            request.redirect('/woow-docs/s/%d' % share.id, code=303, local=True), 'share')

    @http.route('/woow-docs/s/<int:share_id>', type='http', auth='public', methods=['GET'])
    def share_page(self, share_id, **kw):
        access = self._share_entry(share_id=share_id, require_unlocked=False)
        if isinstance(access, Response):
            return access
        if access.share_sudo.has_password and access.grant.get('pw') is not True:
            body = request.env['ir.ui.view']._render_template(
                'woow_document_management.share_password', {'share_id': share_id})
            return streaming.apply_common_headers(
                Response(body, content_type='text/html; charset=utf-8'), 'share')
        try:
            max_files = access.scope_env['woow.document.settings']._get_settings().zip_max_files
            docs = access.share_sudo._scope_documents(
                access.scope_env, max_candidates=max_files)
        except ZipLimitExceeded:
            return streaming.limit_page('M-41', 400)
        body = request.env['ir.ui.view']._render_template(
            'woow_document_management.share_page',
            {'share_id': share_id, 'docs': docs,
             'company_name': access.scope_env['res.company'].browse(
                 access.share_sudo.company_id.id).name})
        access.scope_env['woow.document.log']._log_event(
            'share_access', channel='share', share_id=share_id,
            company_id=access.share_sudo.company_id.id, ip=access.ip)
        return streaming.apply_common_headers(
            Response(body, content_type='text/html; charset=utf-8'), 'share')

    @http.route('/woow-docs/s/<int:share_id>/file/<int:document_id>', type='http',
                auth='public', methods=['GET'])
    def share_file(self, share_id, document_id, **kw):
        access = self._share_entry(share_id=share_id)
        if isinstance(access, Response):
            return access
        try:
            share = access.share_sudo
            docs = share._scope_documents(access.scope_env, [('id', '=', document_id)])
            if len(docs) != 1 or docs.document_type != 'binary':
                return self._denied(access.ip, share.id)
            if not share._download_slot_available(access.scope_env):
                return streaming.uniform_404('share')
            response = streaming.serve(docs, inline=False, channel='share', event=None)
            # A byte-range response transfers file content too. Count it before
            # releasing the share lock so one-use links cannot be replayed with
            # Range requests; 304/416 responses do not transfer content.
            if response.status_code in (200, 206):
                access.scope_env['woow.document.log']._log_event(
                    'share_download', channel='share', share_id=share.id,
                    company_id=share.company_id.id, ip=access.ip,
                    detail={'files': 1, 'bytes': docs.file_size})
            return response
        except LockBusy:
            return streaming.service_busy()
        except (AccessError, MissingError):
            return streaming.uniform_404('share')

    @http.route('/woow-docs/s/<int:share_id>/unlock', type='http', auth='public', methods=['POST'],
                max_content_length=4096)
    def share_unlock(self, share_id, **post):
        access = self._share_entry(share_id=share_id, require_unlocked=False)
        if isinstance(access, Response):
            return access
        share = access.share_sudo
        if not share.has_password:
            return streaming.uniform_404('share')
        try:
            # The lock covers both counters, password work, and the denied-event
            # write. Concurrent guesses cannot all observe the same old count.
            lock(access.scope_env.cr, 'share:%d' % share.id)
            settings = request.env['woow.document.settings']._get_settings()
            log = access.scope_env['woow.document.log']
            failures = log._count_share_unlock_failures(
                share.id, ip_hash=access.ip_hash,
                since=fields.Datetime.now() - timedelta(minutes=settings.share_unlock_cooldown_min))
            if failures >= settings.share_unlock_max_fail:
                return streaming.too_many_requests()
            share_failures = log._count_share_unlock_failures(
                share.id, since=fields.Datetime.now() - timedelta(hours=1))
            if share_failures >= SHARE_UNLOCK_FAIL_PER_HOUR:
                return streaming.too_many_requests()
            password = post.get('password') or ''
            if not tokens.check_password(password, share.password_hash):
                return self._denied(access.ip, share.id, event='share_unlock_failed')
            grant = dict(access.grant)
            grant['pw'] = True
            _save_grant(share, grant)
            return streaming.apply_common_headers(
                request.redirect('/woow-docs/s/%d' % share.id, code=303, local=True), 'share')
        except LockBusy:
            return streaming.service_busy()

    @http.route('/woow-docs/s/<int:share_id>/zip', type='http', auth='public', methods=['GET'])
    def share_zip(self, share_id, **kw):
        access = self._share_entry(share_id=share_id)
        if isinstance(access, Response):
            return access
        share = access.share_sudo
        try:
            max_files = access.scope_env['woow.document.settings']._get_settings().zip_max_files
            docs = share._scope_documents(access.scope_env, max_candidates=max_files)
        except ZipLimitExceeded:
            return streaming.limit_page('M-41', 400)

        def before_build():
            if not share._download_slot_available(access.scope_env):
                return streaming.uniform_404('share')

        def before_stream(stats):
            access.scope_env['woow.document.log']._log_event(
                'share_download', channel='share', share_id=share.id,
                company_id=share.company_id.id, ip=access.ip,
                detail={'zip': 1, 'files': stats.files, 'bytes': stats.bytes})

        return streaming.zip_response(docs, root_folder=share.folder_id if share.share_type == 'folder' else None,
                                      channel='share', share_id=share.id,
                                      ip_hash=access.ip_hash, before_build=before_build,
                                      before_stream=before_stream)

    @http.route('/woow-docs/s/<int:share_id>/close', type='http', auth='public', methods=['POST'],
                max_content_length=4096)
    def share_close(self, share_id, **post):
        access = self._share_entry(share_id=share_id, resolve=False)
        grants = dict(request.session.get(SESSION_GRANTS_KEY) or {})
        grants.pop(str(share_id), None)
        request.session[SESSION_GRANTS_KEY] = grants
        return streaming.apply_common_headers(
            request.redirect('/woow-docs/s', code=303, local=True), 'share')
