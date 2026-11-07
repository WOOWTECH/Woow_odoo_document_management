# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Shared HTTP responses (IF-STREAM, owned by internal-routes-zip).

Module-level functions, not a controller. Every refusal is RETURNED as a Response, never raised
(ER-29). Day-0 stub: fixed minimal bodies; serve / create_batch / zip_response are stubs;
MAX_UPLOAD_BODY, max_upload_body and apply_common_headers are final.
"""
from __future__ import annotations

import json

from odoo.http import Response

from ..services.constants import MAX_UPLOAD_BODY

_HTML = 'text/html; charset=utf-8'


def max_upload_body(controller=None) -> int:
    """Fixed request body cap (ER-03, ER-47.2): 95 MiB, never multiplied by the file count."""
    return MAX_UPLOAD_BODY


def apply_common_headers(response, kind):
    """Add the security headers of one response kind: file, page, share, portal or json."""
    headers = response.headers
    headers['Cache-Control'] = 'no-store, private'
    headers['X-Content-Type-Options'] = 'nosniff'
    if kind == 'file':
        headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
        headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    elif kind == 'share':
        headers['Referrer-Policy'] = 'no-referrer'
        headers['X-Robots-Tag'] = 'noindex, nofollow'
        headers['Content-Security-Policy'] = "frame-ancestors 'none'"
    elif kind == 'portal':
        headers['Content-Security-Policy'] = "frame-ancestors 'self'"
    return response


def _fixed(body: bytes, status: int, kind: str):
    return apply_common_headers(Response(body, status=status, content_type=_HTML), kind)


def uniform_404(kind):
    """Uniform 404 (I-07): same status and body bytes for every failure of one kind."""
    return _fixed(b'<!DOCTYPE html><title>404</title>', 404,
                  'share' if kind == 'share' else 'page')  # WOOW-STUB


def too_many_requests():
    """429 after repeated failures (M-66)."""
    return _fixed(b'<!DOCTYPE html><title>429</title>', 429, 'page')  # WOOW-STUB


def service_busy():
    """503 when a lock could not be taken in time (M-45)."""
    return _fixed(b'<!DOCTYPE html><title>503</title>', 503, 'page')  # WOOW-STUB


def limit_page(message_id, status, **params):
    """ZIP limit or busy page (M-41..M-44)."""
    return _fixed(b'<!DOCTYPE html><title>%d</title>' % int(status), int(status),
                  'page')  # WOOW-STUB


def json_response(payload, status=200):
    """application/json with no-store and nosniff."""
    response = Response(json.dumps(payload), status=status,
                        content_type='application/json; charset=utf-8')
    return apply_common_headers(response, 'json')  # WOOW-STUB


def serve(doc, *, inline, channel, event, share_id=None):
    """Stream one document (caller already checked access in the right env)."""
    raise NotImplementedError  # WOOW-STUB


def create_batch(folder, uploads, *, channel):
    """Create all uploaded files in one savepoint; any failure rolls the whole batch back."""
    raise NotImplementedError  # WOOW-STUB


def zip_response(documents, *, folders=None, root_folder=None, channel, share_id=None,
                 ip_hash=None, before_stream=None):
    """Build and stream a ZIP file under the zip locks (before_stream hook for share links)."""
    raise NotImplementedError  # WOOW-STUB
