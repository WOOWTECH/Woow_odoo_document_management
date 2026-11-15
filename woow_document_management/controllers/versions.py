# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Download of an earlier version of a file."""
from odoo import http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

from .streaming import apply_common_headers, uniform_404


class WoowDocumentVersionController(http.Controller):
    @http.route('/woow_document/version/<int:version_id>/download', type='http',
                auth='user', methods=['GET'])
    def woow_version_download(self, version_id, **kw):
        """Answer as the document download does: one page for a missing and a forbidden version."""
        try:
            if version_id < 1 or version_id > 2**31 - 1:
                return uniform_404('page')
            version = request.env['woow.document.version'].browse(version_id).exists()
            if not version:
                return uniform_404('page')
            version.check_access('read')
            document = version.document_id
            if not document.active or document.folder_in_trash \
                    or document.document_type != 'binary':
                return uniform_404('page')
            policy = request.env['woow.document.upload.policy']
            stream = request.env['ir.binary']._get_stream_from(
                version, 'datas', filename=version.name, mimetype=policy._served_mimetype(version))
            stream.download_name = stream.download_name.replace('\r', '_').replace('\n', '_')
            response = stream.get_response(
                as_attachment=True, content_security_policy="default-src 'none'; sandbox")
            apply_common_headers(response, 'file')
            request.env['woow.document.log']._log_event(
                'download', channel='backend', document_id=document.id,
                folder_id=document.folder_id.id, company_id=document.company_id.id,
                detail={'number': version.number})
            return response
        except (AccessError, MissingError):
            return uniform_404('page')
