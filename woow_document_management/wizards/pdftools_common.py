# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""What the PDF split and merge wizards share: reading sources, storing outputs, the trash option.

Every rule about who may do what is checked here against the live record rules of the caller
(``search`` under the caller's environment, ``_woow_require_rank``): a source that the caller
cannot read is reported as unavailable, in the words used for a source that does not exist.
"""
import base64
import binascii

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError

from ..models.document_folder import _m2o_value
from ..models.quota import LEDGER_KEY, QuotaLedger
from ..services import pdf_tools
from ..services.constants import FILENAME_MAX_CHARS, PDF_MAX_INPUTS
from ..services.pdf_tools import PdfToolError

_PDF = 'application/pdf'


class WoowDocumentPdfCommon(models.AbstractModel):
    _name = 'woow.document.pdf.common'
    _description = 'Shared steps of the PDF split and merge wizards'

    @api.model
    def _woow_pdf_active_ids(self):
        """The ids of the documents that the client selected (a bounded list of positive ints)."""
        ids = self.env.context.get('active_ids')
        if self.env.context.get('active_model') != 'woow.document' or not isinstance(ids, list) \
                or not ids or len(ids) > PDF_MAX_INPUTS:
            return []
        if any(type(item) is not int or item <= 0 for item in ids):
            return []
        return list(dict.fromkeys(ids))

    def _woow_pdf_check_refs(self, vals_list, name, comodel, message):
        """AccessError unless the caller's own search finds every record that ``name`` names.

        A row of these wizards keeps an id for a document or a folder. Storing an id that does
        not exist fails in the database and storing one that exists does not, which would tell
        the caller whether a record they cannot read exists. A value with no id is refused with
        the same words (the field is required, so it could not be stored anyway).
        """
        if self.env.su:
            return
        wanted = set()
        for vals in vals_list:
            if name not in vals:
                continue
            ref = _m2o_value(vals[name], comodel)
            if ref is None:
                raise AccessError(message)
            if ref:
                wanted.add(ref)
        if wanted and set(self.env[comodel].search([('id', 'in', list(wanted))]).ids) != wanted:
            raise AccessError(message)

    def _woow_pdf_require_user(self):
        """Only internal users run these wizards (the ACL already limits them to document users)."""
        if not (self.env.su or self.env.user._is_internal()):
            raise AccessError(_('Only internal users can split or merge PDF files.'))

    def _woow_pdf_message(self, error):
        """The user message of a ``PdfToolError`` (fresh text; it never names a file)."""
        code, params = error.code, error.params
        if code == 'syntax':
            return _('Type the pages like this: 1-3, 4, 5-8.')
        if code == 'range':
            return _('A page is outside the file, which has %(pages)s pages.', pages=params['pages'])
        if code == 'overlap':
            return _('The page ranges must not overlap.')
        if code == 'outputs':
            return _('At most %(limit)s documents can be made at once.', limit=params['limit'])
        if code == 'encrypted':
            return _('A password protected PDF file cannot be used.')
        if code == 'pages':
            return _('A PDF file with more than %(limit)s pages cannot be used.',
                     limit=params['limit'])
        return _('The PDF file is damaged or is not a PDF file.')

    def _woow_pdf_sources(self, ids):
        """The documents of ``ids`` (order and repeats kept) after the live checks.

        Each must be found by a search under the caller's rules, be a live file, and be a
        PDF. Anything else the caller cannot read gives one answer, whether it exists or not.
        """
        unique = list(dict.fromkeys(ids))
        model = self.env['woow.document']
        found = model.search([('id', 'in', unique)]) if unique else model
        if set(found.ids) != set(unique):
            raise AccessError(_('The selected document is unavailable.'))
        for doc in found:
            if not doc.active or doc.folder_in_trash:
                raise AccessError(_('The selected document is unavailable.'))
            mimetype = str(doc.mimetype or '').split(';', 1)[0].strip().lower()
            if doc.document_type != 'binary' or mimetype != _PDF:
                raise UserError(_('Only PDF files can be split or merged.'))
        return model.browse(ids)

    def _woow_pdf_read(self, doc):
        """``(reader, page count)`` of a source the caller may read."""
        try:
            data = base64.b64decode(doc.datas or b'')
        except (binascii.Error, TypeError, ValueError):
            data = b''
        try:
            return pdf_tools.open_pdf(data)
        except PdfToolError as error:
            raise UserError(self._woow_pdf_message(error)) from None

    def _woow_pdf_folder(self, folder):
        """The folder, when the caller sees it, it is live and the caller may add files to it."""
        found = self.env['woow.document.folder'].search([
            ('id', '=', folder.id), ('in_trash', '=', False)], limit=1) if folder else folder
        if not found:
            raise AccessError(_('The target folder is unavailable.'))
        found.check_access('read')
        found._woow_require_rank(2)
        return found

    def _woow_pdf_log_channel(self):
        return self.env['woow.document'].with_context(
            woow_log_channel='backend')._woow_log_channel()

    def _woow_pdf_store(self, folder, outputs):
        """Create ``outputs`` (a list of (name, bytes, tag ids)) in ``folder`` -> the documents.

        They go through ``woow.document.create`` in the caller's environment, so the upload
        policy, the quota and the rights on the folder apply to each file. One quota
        measurement is shared by the whole run, and ``create`` takes only as many files at a
        time as the settings allow, so the files go in as several calls of one transaction.
        """
        model = self.env['woow.document'].with_context(
            woow_log_channel='backend', **{LEDGER_KEY: QuotaLedger()})
        step = max(1, self.env['woow.document.settings']._get_settings().max_files_per_upload)
        values = []
        for name, data, tag_ids in outputs:
            vals = {'name': name, 'folder_id': folder.id, 'datas': base64.b64encode(data)}
            if tag_ids:
                vals['tag_ids'] = [(6, 0, list(tag_ids))]
            values.append(vals)
        created = model.browse()
        for start in range(0, len(values), step):
            created |= model.create(values[start:start + step])
        return created

    def _woow_pdf_trash(self, sources):
        """Move to the trash the ``sources`` that the caller may write -> number moved."""
        allowed = sources._filtered_access('write')
        allowed = allowed.browse(list(dict.fromkeys(allowed.ids)))
        if allowed:
            allowed.with_context(woow_log_channel='backend').action_trash()
        return len(allowed)

    def _woow_pdf_name(self, stem, suffix):
        """``stem + suffix`` kept within the file name limit (the stem is cut, never the suffix)."""
        return stem[:max(1, FILENAME_MAX_CHARS - len(suffix))] + suffix

    def _woow_pdf_stem(self, name):
        """The name of a PDF file without its extension."""
        name = str(name or '')
        return name[:-4] if name.lower().endswith('.pdf') else name

    def _woow_pdf_result(self, documents, message):
        """A notice that shows the new documents once the wizard closes."""
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _('PDF tools'), 'message': message, 'type': 'success', 'sticky': False,
                'next': {
                    'type': 'ir.actions.act_window', 'name': _('New documents'),
                    'res_model': 'woow.document', 'view_mode': 'list,form',
                    'views': [(False, 'list'), (False, 'form')],
                    'domain': [('id', 'in', documents.ids)], 'target': 'current'}}}
