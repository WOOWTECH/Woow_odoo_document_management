# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Portal document pages use only the signed-in user's record-rule scope."""
from odoo import http
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

from ..services.constants import PORTAL_PAGE_SIZE
from . import streaming


def _portal_user():
    return request.env.user.has_group('base.group_portal')


def _visible_folder(folder_id):
    folder = request.env['woow.document.folder'].browse(folder_id).exists()
    if not folder or folder.in_trash:
        return None
    folder.check_access('read')
    return folder


def _visible_document(document_id):
    doc = request.env['woow.document'].browse(document_id).exists()
    if not doc or not doc.active or doc.folder_in_trash:
        return None
    doc.check_access('read')
    return doc


def _page(items, page):
    if page < 1:
        return None
    start = (page - 1) * PORTAL_PAGE_SIZE
    return items[start:start + PORTAL_PAGE_SIZE]


def _render(xmlid, values):
    return streaming.apply_common_headers(request.render(xmlid, values), 'portal')


class WoowDocumentPortal(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'woow_document_count' in counters and _portal_user():
            values['woow_document_count'] = request.env['woow.document'].search_count([
                ('active', '=', True), ('folder_in_trash', '=', False)])
        return values

    @http.route(['/my/docs', '/my/docs/page/<int:page>'], type='http', auth='user',
                website=True, methods=['GET'])
    def portal_my_docs(self, page=1, **kw):
        if not _portal_user():
            return streaming.uniform_404('page')
        try:
            folders = request.env['woow.document.folder']._woow_portal_roots()
            direct_docs = request.env['woow.document']._woow_portal_direct_documents(limit=1000)
            visible = _page(folders, page)
            if visible is None:
                return streaming.uniform_404('page')
            return _render('woow_document_management.portal_my_docs',
                           {'folders': visible, 'direct_docs': direct_docs, 'page': page,
                            'has_more': len(folders) > page * PORTAL_PAGE_SIZE})
        except (AccessError, MissingError):
            return streaming.uniform_404('page')

    @http.route(['/my/docs/folder/<int:folder_id>',
                 '/my/docs/folder/<int:folder_id>/page/<int:page>'],
                type='http', auth='user', website=True, methods=['GET'])
    def portal_my_docs_folder(self, folder_id, page=1, **kw):
        if not _portal_user():
            return streaming.uniform_404('page')
        try:
            folder = _visible_folder(folder_id)
            if not folder:
                return streaming.uniform_404('page')
            children = folder._woow_portal_children()
            docs = request.env['woow.document'].search([
                ('folder_id', '=', folder.id), ('active', '=', True),
                ('folder_in_trash', '=', False)], limit=1000)
            items = [('folder', child) for child in children] + [('document', doc) for doc in docs]
            visible = _page(items, page)
            if visible is None:
                return streaming.uniform_404('page')
            return _render('woow_document_management.portal_my_docs_folder',
                           {'folder': folder, 'items': visible, 'page': page,
                            'has_more': len(items) > page * PORTAL_PAGE_SIZE,
                            'crumbs': folder._woow_breadcrumb(),
                            'can_upload': folder._woow_portal_can_upload()})
        except (AccessError, MissingError):
            return streaming.uniform_404('page')

    @http.route(['/my/docs/file/<int:document_id>',
                 '/my/docs/file/<int:document_id>/download'],
                type='http', auth='user', website=True, methods=['GET'])
    def portal_my_docs_file(self, document_id, **kw):
        if not _portal_user():
            return streaming.uniform_404('page')
        try:
            doc = _visible_document(document_id)
            if not doc:
                return streaming.uniform_404('page')
            if request.httprequest.path.endswith('/download'):
                return streaming.serve(doc, inline=False, channel='portal', event='download')
            parent = request.env['woow.document.folder'].search([
                ('id', '=', doc.folder_id.id)], limit=1)
            back_url = '/my/docs/folder/%d' % parent.id if parent else '/my/docs'
            return _render('woow_document_management.portal_my_docs_file',
                           {'doc': doc, 'back_url': back_url})
        except (AccessError, MissingError):
            return streaming.uniform_404('page')

    @http.route('/my/docs/folder/<int:folder_id>/zip', type='http', auth='user',
                website=True, methods=['GET'])
    def portal_my_docs_zip(self, folder_id, **kw):
        if not _portal_user():
            return streaming.uniform_404('page')
        try:
            folder = _visible_folder(folder_id)
            if not folder:
                return streaming.uniform_404('page')
            max_files = request.env['woow.document.settings']._get_settings().zip_max_files
            docs = request.env['woow.document'].search([
                ('folder_id', 'child_of', folder.id), ('active', '=', True),
                ('folder_in_trash', '=', False)], limit=max_files + 1)
            return streaming.zip_response(docs, root_folder=folder, channel='portal')
        except (AccessError, MissingError):
            return streaming.uniform_404('page')

    @http.route('/my/docs/folder/<int:folder_id>/upload', type='http', auth='user',
                website=True, methods=['POST'], max_content_length=95 * 1024 * 1024)
    def portal_my_docs_upload(self, folder_id, **post):
        if not _portal_user():
            return streaming.uniform_404('page')
        try:
            folder = _visible_folder(folder_id)
            if not folder or not folder._woow_portal_can_upload():
                return streaming.uniform_404('page')
            return streaming.create_batch(folder, request.httprequest.files.getlist('files'),
                                          channel='portal')
        except (AccessError, MissingError):
            return streaming.uniform_404('page')
