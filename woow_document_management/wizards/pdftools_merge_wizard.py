# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Join several PDF documents, in an order the user chooses, into one new document."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import pdf_tools
from ..services.constants import MIB, PDF_MAX_INPUTS
from ..services.pdf_tools import PdfToolError

PDF_MIN_INPUTS = 2


class WoowDocumentPdfMergeLine(models.TransientModel):
    _name = 'woow.document.pdf.merge.line'
    _inherit = ['woow.document.pdf.common', 'woow.document.server.fields.mixin']
    _description = 'A PDF document in the order of a merge'
    _woow_own_default_fields = frozenset()      # every field is filled in by the user
    _order = 'sequence, id'

    wizard_id = fields.Many2one('woow.document.pdf.merge.wizard', string='Merge', required=True,
                                ondelete='cascade')
    document_id = fields.Many2one('woow.document', string='Document', required=True,
                                  ondelete='cascade')
    sequence = fields.Integer(string='Sequence', default=10)

    @api.model_create_multi
    def create(self, vals_list):
        self._woow_pdf_check_refs(vals_list, 'document_id', 'woow.document',
                                  _('The selected document is unavailable.'))
        return super().create(vals_list)

    def write(self, vals):
        self._woow_pdf_check_refs([vals], 'document_id', 'woow.document',
                                  _('The selected document is unavailable.'))
        return super().write(vals)


class WoowDocumentPdfMergeWizard(models.TransientModel):
    _name = 'woow.document.pdf.merge.wizard'
    _inherit = ['woow.document.pdf.common', 'woow.document.server.fields.mixin']
    _description = 'Merge PDF documents'
    _woow_own_default_fields = frozenset()      # every field is filled in by the user

    line_ids = fields.One2many('woow.document.pdf.merge.line', 'wizard_id', string='PDF files')
    folder_id = fields.Many2one('woow.document.folder', string='Folder', required=True)
    name = fields.Char(string='File name', required=True, size=200)
    trash_originals = fields.Boolean(string='Move the originals to the trash')

    @api.model_create_multi
    def create(self, vals_list):
        self._woow_pdf_check_refs(vals_list, 'folder_id', 'woow.document.folder',
                                  _('The target folder is unavailable.'))
        return super().create(vals_list)

    def write(self, vals):
        self._woow_pdf_check_refs([vals], 'folder_id', 'woow.document.folder',
                                  _('The target folder is unavailable.'))
        return super().write(vals)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        ids = self._woow_pdf_active_ids()
        if ids and 'line_ids' in fields_list:
            seen = self.env['woow.document'].search([
                ('id', 'in', ids), ('document_type', '=', 'binary'),
                ('mimetype', '=', 'application/pdf')])
            found = set(seen.ids)
            ordered = [doc_id for doc_id in ids if doc_id in found]
            values['line_ids'] = [
                (0, 0, {'document_id': doc_id, 'sequence': 10 * (index + 1)})
                for index, doc_id in enumerate(ordered)]
            if ordered and 'folder_id' in fields_list and not values.get('folder_id'):
                values['folder_id'] = self.env['woow.document.folder'].search(
                    [('id', '=', seen.browse(ordered[0]).folder_id.id)], limit=1).id or False
        return values

    def action_merge(self):
        """Join the listed PDF documents, top to bottom, into one new document."""
        self.check_access('write')
        self.ensure_one()
        self._woow_pdf_require_user()
        lines = self.line_ids.sorted(lambda line: (line.sequence, line.id))
        ids = [line.document_id.id for line in lines]
        if not PDF_MIN_INPUTS <= len(ids) <= PDF_MAX_INPUTS:
            raise UserError(_('Choose between %(least)s and %(most)s PDF files to merge.',
                              least=PDF_MIN_INPUTS, most=PDF_MAX_INPUTS))
        name = (self.name or '').strip()
        if not name:
            raise UserError(_('Give the merged file a name.'))
        if not name.lower().endswith('.pdf'):
            name = self._woow_pdf_name(name, '.pdf')
        sources = self._woow_pdf_sources(ids)
        folder = self._woow_pdf_folder(self.folder_id)
        limit = self.env['woow.document.settings']._get_settings().internal_max_upload_mb * MIB
        # The result is about as large as its parts together, and a file above the limit is
        # refused anyway, so the parts are not read at all when they already add up to more.
        if sum(doc.file_size or 0 for doc in sources) > limit:
            raise UserError(_('The merged file would be larger than the upload limit.'))
        pages = []
        total = 0
        try:
            for doc in sources:
                reader, count = self._woow_pdf_read(doc)
                pages.extend(pdf_tools.page_range(reader, 1, count))
                total += count
            data = pdf_tools.write_pdf(pages)
        except PdfToolError as error:
            raise UserError(self._woow_pdf_message(error)) from None
        with self.env.cr.savepoint():
            created = self._woow_pdf_store(folder, [(name, data, [])])
            trashed = self._woow_pdf_trash(sources) if self.trash_originals else 0
            self.env['woow.document.log']._log_event(
                'pdf_merge', channel=self._woow_pdf_log_channel(), document_id=created.id,
                folder_id=folder.id, company_id=folder.company_id.id,
                detail={'inputs': len(sources), 'pages': total, 'trashed': trashed,
                        'source_ids': list(dict.fromkeys(sources.ids))})
        message = _('The PDF files were merged into one document of %(pages)s pages.', pages=total)
        if self.trash_originals:
            message = '%s %s' % (message, _('%(count)s of the originals were moved to the trash.',
                                            count=trashed))
        return self._woow_pdf_result(created, message)
