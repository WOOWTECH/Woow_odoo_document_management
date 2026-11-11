# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Streaming guard for this module's records on the core content routes."""
import os

import werkzeug.security

from odoo import _, models
from odoo.exceptions import MissingError
from odoo.http import Stream, request
from odoo.tools import config

from ..services.constants import WOOW_MODELS


class IrBinary(models.AbstractModel):
    _inherit = 'ir.binary'

    def _find_record(self, xmlid=None, res_model='ir.attachment', res_id=None,
                     access_token=None, field=None):
        """Module records never take an access token.

        ``/web/content`` and ``/web/image`` mark a response public (shared caches may
        keep it for a year) whenever the query carries a token, while the module's
        records are only ever read under the caller's own access rights.
        """
        record = super()._find_record(xmlid=xmlid, res_model=res_model, res_id=res_id,
                                      access_token=access_token, field=field)
        if record._name in WOOW_MODELS and (
                access_token or (request and request.httprequest.args.get('access_token'))):
            raise MissingError(_('The document is unavailable.'))
        return record

    def _record_to_stream(self, record, field_name):
        """Stream a document file from the attachment the document records, and only that one.

        Core streams the newest attachment that claims the field. Every module
        path (download, preview, portal, share, ZIP) and ``/web/content`` or
        ``/web/image`` for a document's file come here, so another attachment that
        claims the same field, however it was made, is never served: the stream is
        built from the document's own ``attachment_id`` (Q17), the attachment its
        size and checksum come from, which must be a stored binary file, never a
        URL. The caller has checked access to the document, as for core streams.
        """
        if record._name != 'woow.document' or field_name != 'datas':
            return super()._record_to_stream(record, field_name)
        if len(record) != 1:
            raise MissingError(_('The document is unavailable.'))
        record.check_field_access_rights('read', [field_name])
        row = self.env['ir.attachment']._woow_document_file_row(record.id)
        if not row:
            raise MissingError(_('The document is unavailable.'))
        name, mimetype, checksum, store_fname, db_datas, write_date = row
        stream = Stream(mimetype=mimetype, download_name=name, etag=checksum, public=False)
        if store_fname:
            stream.type = 'path'
            stream.path = werkzeug.security.safe_join(
                os.path.abspath(config.filestore(self.env.cr.dbname)), store_fname)
            if not stream.path:
                raise MissingError(_('The document is unavailable.'))
            stat = os.stat(stream.path)
            stream.last_modified = stat.st_mtime
            stream.size = stat.st_size
        else:
            stream.type = 'data'
            stream.data = bytes(db_datas or b'')
            stream.last_modified = write_date
            stream.size = len(stream.data)
        return stream
