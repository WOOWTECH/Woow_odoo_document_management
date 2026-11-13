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
from .upload_policy import INLINE_WHITELIST

_FILE_CSP = "default-src 'none'; sandbox"


class _DocumentStream(Stream):
    """A document file that is served under the module's own type and headers.

    The core content routes pass their ``mimetype`` query parameter to the stream and
    send the response inline with a policy that has no ``sandbox``, so
    ``/web/content/...?mimetype=text/html`` would show an uploaded text file as an HTML
    page that can post a form or redirect from the site's own origin. This stream keeps
    the type the upload policy served for the file, ignores any other, sends only the
    types of the inline whitelist inline, and always adds ``sandbox`` to the policy.
    """

    def __init__(self, *, mimetype, **kwargs):
        super().__init__(**kwargs)
        self._pinned_mimetype = mimetype

    @property
    def mimetype(self):
        return self._pinned_mimetype

    @mimetype.setter
    def mimetype(self, value):
        """A request cannot change the type: ``ir.binary._get_stream_from`` assigns it."""

    def get_response(self, as_attachment=None, immutable=None, content_security_policy=None,
                     **send_file_kwargs):
        if as_attachment is None:
            as_attachment = self.as_attachment
        inline = not as_attachment and self.mimetype in INLINE_WHITELIST
        return super().get_response(
            as_attachment=not inline, immutable=immutable,
            content_security_policy=_FILE_CSP, **send_file_kwargs)


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
        path (download, preview, portal, ZIP) and ``/web/content`` or
        ``/web/image`` for a document's file come here, so another attachment that
        claims the same field, however it was made, is never served: the stream is
        built from the document's own ``attachment_id`` (Q17), the attachment its
        size and checksum come from, which must be a stored binary file, never a
        URL. The caller has checked access to the document, as for core streams.
        """
        if record._name == 'ir.attachment' and field_name in ('raw', 'datas', 'db_datas') \
                and record.res_model in WOOW_MODELS and record.res_field:
            # The file of a document has one route, through the document: this one would
            # let the caller choose the type of the response by its own query.
            raise MissingError(_('The document is unavailable.'))
        if record._name != 'woow.document' or field_name != 'datas':
            return super()._record_to_stream(record, field_name)
        if len(record) != 1:
            raise MissingError(_('The document is unavailable.'))
        record.check_field_access_rights('read', [field_name])
        row = self.env['ir.attachment']._woow_document_file_row(record.id)
        if not row:
            raise MissingError(_('The document is unavailable.'))
        name, _mimetype, checksum, store_fname, db_datas, write_date = row
        # The type the upload policy serves for this file (its base type: the response adds
        # the charset), not the one of the attachment row and never one from the request.
        served = self.env['woow.document.upload.policy']._served_mimetype(record)
        stream = _DocumentStream(mimetype=served.split(';', 1)[0], download_name=name,
                                 etag=checksum, public=False)
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
