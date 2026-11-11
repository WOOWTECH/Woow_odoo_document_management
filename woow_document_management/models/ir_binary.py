# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Streaming guard for this module's records on the core content routes."""
from odoo import _, models
from odoo.exceptions import MissingError
from odoo.http import request

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
        """Serve a document file only from the one attachment the document records."""
        if record._name == 'woow.document' and field_name == 'datas' and (
                len(record) != 1 or not self.env['ir.attachment']._woow_document_file_is_unique(
                    record.id)):
            raise MissingError(_('The document is unavailable.'))
        return super()._record_to_stream(record, field_name)
