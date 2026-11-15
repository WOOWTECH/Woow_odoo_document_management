# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Buttons that open the PDF split and merge wizards for the selected documents."""
from odoo import _, models
from odoo.exceptions import AccessError, MissingError, UserError

from ..services.constants import PDF_MAX_INPUTS

_USER_GROUP = 'woow_document_management.group_woow_document_user'


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

    def _woow_pdf_check_selection(self):
        """AccessError, with the same words, unless the caller reads every selected document.

        Core answers a document that exists but is hidden and a document that does not exist
        with two different errors; one answer keeps the existence of a document private.
        """
        try:
            self.check_access('read')
        except (AccessError, MissingError):
            raise AccessError(_('The selected document is unavailable.')) from None

    def action_open_pdf_split_wizard(self):
        """Open the split dialog for one PDF document."""
        if not self.env.user.has_group(_USER_GROUP):
            raise AccessError(_('Only internal users can split or merge PDF files.'))
        self._woow_pdf_check_selection()
        self.ensure_one()
        return _wizard_action(self, _('Split PDF'), 'woow.document.pdf.split.wizard')

    def action_open_pdf_merge_wizard(self):
        """Open the merge dialog for the selected PDF documents."""
        if not self.env.user.has_group(_USER_GROUP):
            raise AccessError(_('Only internal users can split or merge PDF files.'))
        self._woow_pdf_check_selection()
        if len(self) > PDF_MAX_INPUTS:
            raise UserError(_('At most %(most)s PDF files can be merged at once.',
                              most=PDF_MAX_INPUTS))
        return _wizard_action(self, _('Merge PDF files'), 'woow.document.pdf.merge.wizard')
