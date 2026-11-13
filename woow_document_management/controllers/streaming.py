# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Shared HTTP responses (IF-STREAM, owned by internal-routes-zip).

Module-level functions, not a controller. Every refusal is RETURNED as a Response, never raised
(ER-29): the uniform refusal pages, file serving, batch upload and ZIP streaming.
"""
from __future__ import annotations

import base64
import io
import json
import tempfile
from contextlib import closing

from odoo import _
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.http import request
from werkzeug.wsgi import wrap_file

from ..models.upload_policy import WoowUploadRejected
from ..services import folding
from ..services.constants import ZIP_SPOOL_MAX_BYTES
from ..services.locks import LockBusy, lock, try_lock
from ..services.naming import sanitize_segment
from ..services.zip_builder import (ZipEntry, ZipLimitExceeded, build_zip,
                                    filestore_stream, limits_from_settings)

from odoo.http import Response

from ..services.constants import MAX_UPLOAD_BODY

_HTML = 'text/html; charset=utf-8'


def max_upload_body(controller=None) -> int:
    """Fixed request body cap (ER-03, ER-47.2): 95 MiB, never multiplied by the file count."""
    return MAX_UPLOAD_BODY


def apply_common_headers(response, kind):
    """Add the security headers of one response kind: file, page, portal or json."""
    headers = response.headers
    headers['Cache-Control'] = 'no-store, private'
    headers['X-Content-Type-Options'] = 'nosniff'
    if kind == 'file':
        headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
        headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    elif kind == 'portal':
        headers['Content-Security-Policy'] = "frame-ancestors 'self'"
    return response


def _fixed(body: bytes, status: int, kind: str):
    return apply_common_headers(Response(body, status=status, content_type=_HTML), kind)


def uniform_404(kind):
    """Uniform 404 (I-07): same status and body bytes for every failure of one kind."""
    if kind != 'page':
        raise ValueError('invalid response kind')
    body = request.env['ir.ui.view']._render_template(
        'woow_document_management.uniform_404_' + kind, {})
    return _fixed(body.encode('utf-8') if isinstance(body, str) else body, 404, kind)


def service_busy():
    """503 when a lock could not be taken in time (M-45)."""
    body = request.env['ir.ui.view']._render_template(
        'woow_document_management.service_busy_page', {})
    return _fixed(body.encode('utf-8') if isinstance(body, str) else body, 503, 'page')


def limit_page(message_id, status, **params):
    """ZIP limit or busy page (M-41..M-44)."""
    if message_id not in ('M-41', 'M-42', 'M-43', 'M-44') or status not in (400, 429, 503):
        raise ValueError('invalid limit page')
    body = request.env['ir.ui.view']._render_template(
        'woow_document_management.zip_limit_page', {'status': status})
    return _fixed(body.encode('utf-8') if isinstance(body, str) else body, status, 'page')


def json_response(payload, status=200):
    """application/json with no-store and nosniff."""
    response = Response(json.dumps(payload), status=status,
                        content_type='application/json; charset=utf-8')
    return apply_common_headers(response, 'json')


def serve(doc, *, inline, channel, event):
    """Stream one document (caller already checked access in the right env)."""
    try:
        if len(doc) != 1 or doc.document_type != 'binary' or not doc.active or doc.folder_in_trash:
            return uniform_404('page')
        policy = doc.env['woow.document.upload.policy']
        mimetype = policy._served_mimetype(doc)
        stream = doc.env['ir.binary']._get_stream_from(
            doc, 'datas', filename=doc.name, mimetype=mimetype)
        stream.download_name = stream.download_name.replace('\r', '_').replace('\n', '_')
        response = stream.get_response(
            as_attachment=not (inline and policy._inline_allowed(mimetype)),
            content_security_policy="default-src 'none'; sandbox")
        apply_common_headers(response, 'file')
        if event:
            doc.env['woow.document.log']._log_event(
                event, channel=channel, document_id=doc.id, folder_id=doc.folder_id.id,
                company_id=doc.company_id.id)
        return response
    except (AccessError, MissingError):
        return uniform_404('page')


def create_batch(folder, uploads, *, channel):
    """Create all uploaded files in one savepoint; any failure rolls the whole batch back."""
    try:
        limit = folder.env['woow.document.settings']._get_settings().max_files_per_upload
        if not uploads or len(uploads) > limit:
            return json_response({'ok': False, 'message': _('Invalid number of files.'),
                                  'errors': []}, 400)
        vals = []
        for upload in uploads:
            # A request that is run again after a concurrency error has only the first
            # upload rewound by the framework; the others would read as empty files.
            upload.stream.seek(0)
            vals.append({'name': upload.filename, 'folder_id': folder.id,
                         'datas': base64.b64encode(upload.read()).decode('ascii')})
        with folder.env.cr.savepoint():
            documents = folder.env['woow.document'].create(vals)
        return json_response({'ok': True, 'document_ids': documents.ids})
    except LockBusy:
        return service_busy()
    except (AccessError, MissingError):
        return uniform_404('page')
    except WoowUploadRejected as exc:
        return json_response({'ok': False, 'message': str(exc),
                              'errors': [{'index': 0, 'code': exc.code, 'message': str(exc)}]}, 400)
    except (ValidationError, UserError) as exc:
        return json_response({'ok': False, 'message': str(exc),
                              'errors': [{'index': 0, 'code': 'invalid', 'message': str(exc)}]}, 400)


def _lazy_stream(doc):
    """Open the stored file of ``doc`` when the archive reaches it, not when it is listed.

    ``build_zip`` checks the announced sizes against the limits before it opens the first
    stream. A file kept in the database is loaded whole when its stream is built, so
    building the streams of a whole scope first would hold every file in memory before
    the size limit could refuse it.
    """
    def open_stream():
        env = doc.env
        stream = env['ir.binary']._get_stream_from(
            doc, 'datas', filename=doc.name,
            mimetype=env['woow.document.upload.policy']._served_mimetype(doc))
        if stream.type == 'path':
            return filestore_stream(stream.path)
        if stream.type == 'data':
            return io.BytesIO(stream.data)
        raise MissingError(_('The document is unavailable.'))
    return open_stream


def zip_response(documents, *, folders=None, root_folder=None, channel):
    """Build and stream a ZIP under zip locks."""
    env = documents.env
    archive = None
    try:
        lock(env.cr, 'zip:u:%d' % env.uid)
        settings = env['woow.document.settings']._get_settings()
        slot = next((slot for slot in range(1, settings.zip_concurrency + 1)
                     if try_lock(env.cr, 'zip:slot:%d' % slot)), None)
        if slot is None:
            return limit_page('M-43', 429)
        if len(documents) > settings.zip_max_files:
            return limit_page('M-41', 400)
        documents.check_access('read')
        docs = documents.filtered(lambda doc: doc.active and not doc.folder_in_trash)
        all_folders = env['woow.document.folder'].search([
            ('id', 'in', [int(part) for doc in docs for part in
                          (doc.folder_id.parent_path or '').split('/') if part.isdigit()])])
        readable = {folder.id: sanitize_segment(folder.name, _('Folder')) for folder in all_folders}
        entries = []
        links = []
        for doc in docs:
            path_ids = tuple(int(part) for part in (doc.folder_id.parent_path or '').split('/')
                             if part.isdigit())
            parts = folding.zip_segments(root_folder.id if root_folder else None,
                                         path_ids, readable)
            if root_folder and not parts:
                continue
            name = sanitize_segment(doc.name, _('Document'))
            if doc.document_type == 'url':
                links.append(doc.url)
                continue
            if doc.document_type != 'binary':
                continue
            entries.append(ZipEntry((*parts, name), _lazy_stream(doc), int(doc.file_size),
                                    not (doc.mimetype or '').startswith(('image/', 'application/zip',
                                                                 'application/pdf'))))
        archive = tempfile.SpooledTemporaryFile(max_size=ZIP_SPOOL_MAX_BYTES)
        stats = build_zip(entries, limits_from_settings(settings), archive, links)
        env['woow.document.log']._log_event(
            'zip', channel=channel,
            company_id=env.company.id,
            detail={'files': stats.files, 'bytes': stats.bytes})
        archive.seek(0, 2)
        length = archive.tell()
        archive.seek(0)
        response = Response(wrap_file(request.httprequest.environ, archive), status=200,
                            content_type='application/zip', direct_passthrough=True)
        response.headers['Content-Length'] = str(length)
        response.headers['Content-Disposition'] = 'attachment; filename="documents.zip"'
        response.call_on_close(archive.close)
        archive = None
        apply_common_headers(response, 'file')
        return response
    except LockBusy:
        return service_busy()
    except ZipLimitExceeded as exc:
        return limit_page('M-44' if exc.code == 'deadline' else 'M-41',
                          503 if exc.code == 'deadline' else 400)
    except (AccessError, MissingError):
        return uniform_404('page')
    finally:
        if archive is not None:
            archive.close()
