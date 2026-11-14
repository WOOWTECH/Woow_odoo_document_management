# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The file of a version is streamed under the module's own type and headers."""
import os

import werkzeug.security

from odoo import _, models
from odoo.exceptions import MissingError
from odoo.tools import config

from .ir_binary import _DocumentStream


class IrBinaryVersions(models.AbstractModel):
    _inherit = 'ir.binary'

    def _record_to_stream(self, record, field_name):
        """Stream the file of a version from its own stored attachment, and only that one.

        Core streams the newest attachment that claims the field, as superuser, and lets the
        request choose the type of the response. A version is served like a document: the type
        that the upload policy kept, the headers of the module, and only the attachment that
        backs the field of this row (found under the caller's own rights, which follow the
        record rules of the version). The caller has checked access to the version.
        """
        if record._name != 'woow.document.version' or field_name != 'datas':
            return super()._record_to_stream(record, field_name)
        if len(record) != 1:
            raise MissingError(_('The document is unavailable.'))
        record.check_field_access_rights('read', [field_name])
        attachment = self.env['ir.attachment'].search([
            ('res_model', '=', record._name), ('res_id', '=', record.id),
            ('res_field', '=', field_name)], order='id', limit=1)
        if not attachment or attachment.type != 'binary' or attachment.url:
            raise MissingError(_('The document is unavailable.'))
        served = self.env['woow.document.upload.policy']._served_mimetype(record)
        stream = _DocumentStream(mimetype=served.split(';', 1)[0], download_name=record.name,
                                 etag=attachment.checksum, public=False)
        if attachment.store_fname:
            stream.type = 'path'
            stream.path = werkzeug.security.safe_join(
                os.path.abspath(config.filestore(self.env.cr.dbname)), attachment.store_fname)
            if not stream.path:
                raise MissingError(_('The document is unavailable.'))
            stat = os.stat(stream.path)
            stream.last_modified = stat.st_mtime
            stream.size = stat.st_size
        else:
            stream.type = 'data'
            stream.data = bytes(attachment.db_datas or b'')
            stream.last_modified = attachment.write_date
            stream.size = len(stream.data)
        return stream
