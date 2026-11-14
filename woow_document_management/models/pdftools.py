# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Buttons that open the PDF split and merge wizards for the selected documents."""
from odoo import _, models


def _wizard_action(documents, name, model):
    """A dialog for ``model`` that starts from the documents the user selected."""
    return {
        'type': 'ir.actions.act_window', 'name': name, 'res_model': model,
        'view_mode': 'form', 'target': 'new',
        'context': {'active_model': 'woow.document', 'active_ids': documents.ids,
                    'active_id': documents.ids[0] if documents.ids else False},
    }


class WoowDocumentPdfTools(models.Model):
    _inherit = 'woow.document'

    def action_open_pdf_split_wizard(self):
        """Open the split dialog for one PDF document."""
        self.check_access('read')
        self.ensure_one()
        return _wizard_action(self, _('Split PDF'), 'woow.document.pdf.split.wizard')

    def action_open_pdf_merge_wizard(self):
        """Open the merge dialog for the selected PDF documents."""
        self.check_access('read')
        return _wizard_action(self, _('Merge PDF files'), 'woow.document.pdf.merge.wizard')
