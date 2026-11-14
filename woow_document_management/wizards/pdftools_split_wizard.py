# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Cut one PDF document into several documents by page ranges."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import pdf_tools
from ..services.pdf_tools import PdfToolError


class WoowDocumentPdfSplitWizard(models.TransientModel):
    _name = 'woow.document.pdf.split.wizard'
    _inherit = ['woow.document.pdf.common', 'woow.document.server.fields.mixin']
    _description = 'Split a PDF document'
    _woow_own_default_fields = frozenset()      # every field is filled in by the user

    document_id = fields.Many2one('woow.document', string='Document', required=True,
                                  ondelete='cascade')
    mode = fields.Selection([('ranges', 'Page ranges'), ('pages', 'One document per page')],
                            string='Split by', required=True, default='ranges')
    ranges = fields.Char(string='Pages', size=1000)
    trash_original = fields.Boolean(string='Move the original to the trash')
    can_trash = fields.Boolean(string='Can trash the original', compute='_compute_can_trash')

    @api.model_create_multi
    def create(self, vals_list):
        self._woow_pdf_check_refs(vals_list, 'document_id', 'woow.document',
                                  _('The selected document is unavailable.'))
        return super().create(vals_list)

    def write(self, vals):
        self._woow_pdf_check_refs([vals], 'document_id', 'woow.document',
                                  _('The selected document is unavailable.'))
        return super().write(vals)

    @api.depends('document_id')
    def _compute_can_trash(self):
        """Whether the caller may write the document (live, under the record rules)."""
        for wizard in self:
            doc_id = wizard.document_id._origin.id or wizard.document_id.id
            found = self.env['woow.document'].search([('id', '=', doc_id)], limit=1) \
                if isinstance(doc_id, int) else self.env['woow.document']
            wizard.can_trash = bool(found._filtered_access('write'))

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        ids = self._woow_pdf_active_ids()
        if 'document_id' in fields_list and len(ids) == 1:
            values['document_id'] = self.env['woow.document'].search(
                [('id', 'in', ids)], limit=1).id or False
        return values

    def action_split(self):
        """Make one document per page range, in the folder of the original."""
        self.check_access('write')
        self.ensure_one()
        self._woow_pdf_require_user()
        source = self._woow_pdf_sources([self.document_id.id])
        folder = self._woow_pdf_folder(source.folder_id)
        reader, count = self._woow_pdf_read(source)
        try:
            if self.mode == 'pages':
                parts = pdf_tools.single_pages(count)
            elif self.mode == 'ranges':
                parts = pdf_tools.parse_ranges(self.ranges, count)
            else:
                raise UserError(_('Choose how to split the file.'))
            stem = self._woow_pdf_stem(source.name)
            outputs = []
            for first, last in parts:
                label = _('page %(first)s', first=first) if first == last else _(
                    'pages %(first)s-%(last)s', first=first, last=last)
                name = self._woow_pdf_name(stem, ' (%s).pdf' % label)
                data = pdf_tools.write_pdf(pdf_tools.page_range(reader, first, last))
                outputs.append((name, data, source.tag_ids.ids))
        except PdfToolError as error:
            raise UserError(self._woow_pdf_message(error)) from None
        with self.env.cr.savepoint():
            created = self._woow_pdf_store(folder, outputs)
            trashed = self._woow_pdf_trash(source) if self.trash_original else 0
            self.env['woow.document.log']._log_event(
                'pdf_split', channel=self._woow_pdf_log_channel(), document_id=source.id,
                folder_id=folder.id, company_id=folder.company_id.id,
                detail={'outputs': len(created), 'pages': count, 'trashed': trashed,
                        'output_ids': created.ids})
        message = _('%(count)s documents were made from the PDF file.', count=len(created))
        if self.trash_original:
            message = '%s %s' % (message, _('The original was moved to the trash.') if trashed
                                 else _('You cannot trash the original, so it was kept.'))
        return self._woow_pdf_result(created, message)
