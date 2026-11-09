# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Restore selected items to an available folder."""
from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.document import WoowDocument
from ..models.document_folder import WoowDocumentFolder


class WoowDocumentRestoreWizard(models.TransientModel):
    _name = 'woow.document.restore.wizard'
    _description = 'Restore to another folder'

    document_ids = fields.Many2many('woow.document')
    folder_ids = fields.Many2many('woow.document.folder')
    target_folder_id = fields.Many2one('woow.document.folder')

    def action_restore_to(self):
        """Restore the selected items into the chosen folder."""
        self.check_access('write')
        self.ensure_one()
        target = self.target_folder_id
        if not target or target.in_trash:
            raise UserError(_('Choose an available folder.'))
        target.check_access('read')
        docs = self.with_context(active_test=False).document_ids
        folders = self.with_context(active_test=False).folder_ids
        if not docs and not folders:
            raise UserError(_('Select items to restore.'))
        for doc in docs:
            doc.with_context(active_test=False).check_access('write')
            target._woow_require_rank(2)
            if doc.company_id != target.company_id:
                raise AccessError(_('The target folder is unavailable.'))
        for folder in folders:
            folder.with_context(active_test=False)._woow_require_rank(4)
            target._woow_require_rank(3)
            if folder.company_id != target.company_id:
                raise AccessError(_('The target folder is unavailable.'))
        with self.env.cr.savepoint():
            for doc in docs:
                super(WoowDocument, doc).write({
                    'folder_id': target.id, 'active': True,
                    'restored_date': fields.Datetime.now()})
                self.env['woow.document.log']._log_event(
                    'restore', channel=doc._woow_log_channel(), document_id=doc.id,
                    company_id=doc.company_id.id)
            for folder in folders:
                # Use the guarded write for parent rules and cycle checks first.
                folder.write({'parent_id': target.id})
                super(WoowDocumentFolder, folder).write({
                    'active': True, 'restored_date': fields.Datetime.now()})
                self.env['woow.document.log']._log_event(
                    'restore', channel=folder._woow_log_channel(), folder_id=folder.id,
                    company_id=folder.company_id.id)
        return {'type': 'ir.actions.act_window_close'}
