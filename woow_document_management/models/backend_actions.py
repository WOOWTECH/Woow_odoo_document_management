# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Back-office actions with live access checks."""
from odoo import _, fields, models


class WoowDocumentBackend(models.Model):
    _inherit = 'woow.document'

    woow_has_portal_members = fields.Boolean(compute='_compute_backend_portal_members')

    def _compute_backend_portal_members(self):
        for document in self:
            document.woow_has_portal_members = bool(document.folder_id.eff_read_partner_ids)

    def action_open_move_wizard(self):
        self.check_access('write')
        return {
            'type': 'ir.actions.act_window', 'name': 'Move documents',
            'res_model': 'woow.document.move.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_document_ids': [(6, 0, self.ids)]},
        }


class WoowDocumentFolderBackend(models.Model):
    _inherit = 'woow.document.folder'

    def action_open_move_wizard(self):
        self.check_access('write')
        self._woow_require_rank(4)
        return {
            'type': 'ir.actions.act_window', 'name': 'Move folders',
            'res_model': 'woow.document.move.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_folder_ids': [(6, 0, self.ids)]},
        }

    def action_notify_members(self):
        self._woow_require_rank(4)
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Notify members'),
            'res_model': 'woow.document.notify.wizard', 'view_mode': 'form',
            'target': 'new', 'context': {'default_folder_id': self.id},
        }
