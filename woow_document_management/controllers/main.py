# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Authorized internal upload and document streaming routes."""
from odoo import http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

from .streaming import create_batch, serve, uniform_404, zip_response

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'


def _internal_documents():
    user = request.env.user
    return bool(user._is_internal() and (user.has_group(_USER) or user.has_group(_MANAGER)))


def _document(document_id):
    doc = request.env['woow.document'].browse(document_id).exists()
    if not doc:
        return None
    doc.check_access('read')
    if not doc.active or doc.folder_in_trash or doc.document_type != 'binary':
        return None
    return doc


def _ids(raw):
    if not raw:
        return []
    parts = raw.split(',')
    if any(not item.isdecimal() for item in parts):
        raise ValueError('invalid identifiers')
    values = [int(item) for item in parts]
    if any(value < 1 or value > 2**31 - 1 for value in values) or len(values) != len(set(values)):
        raise ValueError('invalid identifiers')
    return values


class WoowDocumentController(http.Controller):
    @http.route('/woow_document/upload', type='http', auth='user', methods=['POST'],
                max_content_length=95 * 1024 * 1024)
    def woow_upload(self, **post):
        if not _internal_documents():
            return uniform_404('page')
        try:
            folder_id = int(post.get('folder_id', ''))
            if folder_id < 1 or folder_id > 2**31 - 1:
                raise ValueError('invalid folder')
        except (TypeError, ValueError):
            return uniform_404('page')
        try:
            folder = request.env['woow.document.folder'].browse(folder_id).exists()
            if not folder or folder.in_trash:
                return uniform_404('page')
            folder.check_access('read')
            return create_batch(folder, request.httprequest.files.getlist('files'), channel='backend')
        except (AccessError, MissingError):
            return uniform_404('page')

    @http.route('/woow_document/doc/<int:document_id>/download', type='http',
                auth='user', methods=['GET'])
    def woow_download(self, document_id, **kw):
        try:
            doc = _document(document_id)
            if not doc:
                return uniform_404('page')
            return serve(doc, inline=False, channel='backend' if _internal_documents() else 'portal',
                         event='download')
        except (AccessError, MissingError):
            return uniform_404('page')

    @http.route('/woow_document/doc/<int:document_id>/preview', type='http',
                auth='user', methods=['GET'])
    def woow_preview(self, document_id, **kw):
        try:
            doc = _document(document_id)
            if not doc:
                return uniform_404('page')
            return serve(doc, inline=True, channel='backend' if _internal_documents() else 'portal',
                         event='view')
        except (AccessError, MissingError):
            return uniform_404('page')

    @http.route('/woow_document/zip', type='http', auth='user', methods=['GET'])
    def woow_zip(self, document_ids='', folder_ids='', **kw):
        if not _internal_documents():
            return uniform_404('page')
        try:
            doc_ids, folder_ids_parsed = _ids(document_ids), _ids(folder_ids)
            settings = request.env['woow.document.settings']._get_settings()
            if not (doc_ids or folder_ids_parsed) or len(doc_ids) + len(folder_ids_parsed) > settings.zip_max_files:
                return uniform_404('page')
            folders = request.env['woow.document.folder'].search([('id', 'in', folder_ids_parsed)])
            docs = request.env['woow.document'].search([('id', 'in', doc_ids)])
            if len(folders) != len(folder_ids_parsed) or len(docs) != len(doc_ids):
                return uniform_404('page')
            folders.check_access('read')
            docs.check_access('read')
            if folder_ids_parsed:
                descendants = request.env['woow.document'].search([
                    ('folder_id', 'child_of', folder_ids_parsed), ('active', '=', True),
                    ('folder_in_trash', '=', False)], limit=settings.zip_max_files + 1)
                docs |= descendants
            return zip_response(docs, folders=folders, channel='backend')
        except (TypeError, ValueError, AccessError, MissingError):
            return uniform_404('page')
