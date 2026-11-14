# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Make a shortcut to a file or a link in another folder."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..models.shortcuts import SHORTCUT_CTX_KEY, _SHORTCUT_SENTINEL, TARGET_TYPES


class WoowDocumentShortcutWizard(models.TransientModel):
    _name = 'woow.document.shortcut.wizard'
    _inherit = ['woow.document.server.fields.mixin']
    _description = 'Create a shortcut'
    _woow_own_default_fields = frozenset()      # both fields are filled in by the user

    document_id = fields.Many2one('woow.document', string='Document', required=True,
                                  readonly=True)
    folder_id = fields.Many2one('woow.document.folder', string='Destination folder',
                                required=True)

    def _woow_check_visible(self, vals):
        """A document or a folder that is named here must be one that the caller can find.

        The database would answer a missing id with a constraint error and a hidden one with a new
        row, which tells the caller which ids exist. The caller's own search (record rules apply)
        gives the same refusal for both.
        """
        if self.env.su:
            return
        for name, model, message in (
                ('document_id', 'woow.document', _('The document is unavailable.')),
                ('folder_id', 'woow.document.folder', _('The target folder is unavailable.'))):
            value = vals.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and not self.env[model].with_context(
                    active_test=False).search([('id', '=', value)], limit=1):
                raise AccessError(message)

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(['document_id', 'folder_id'])
        for vals in vals_list:
            self._woow_check_visible({**defaults, **vals})
        return super().create(vals_list)

    def write(self, vals):
        self._woow_check_visible(vals)
        return super().write(vals)

    def action_create(self):
        """Create the shortcut for the caller; the rights are judged again, at this moment."""
        self.check_access('write')
        self.ensure_one()
        if not self.env.su and not self.env.user._is_internal():
            raise AccessError(_('Only internal users can create shortcuts.'))
        target = self.env['woow.document'].search([('id', '=', self.document_id.id)], limit=1)
        if not target or not target.active or target.folder_in_trash \
                or target.document_type not in TARGET_TYPES:
            raise AccessError(_('The document is unavailable.'))
        folder = self.env['woow.document.folder'].search([
            ('id', '=', self.folder_id.id), ('in_trash', '=', False)], limit=1)
        if not folder:
            raise AccessError(_('The target folder is unavailable.'))
        folder.check_access('read')
        folder._woow_require_rank(2)
        self.env['woow.document'].with_context(**{SHORTCUT_CTX_KEY: _SHORTCUT_SENTINEL}).create({
            'name': target.name, 'folder_id': folder.id, 'document_type': 'shortcut',
            'shortcut_target_id': target.id})
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Shortcut created'),
                           'message': _('The shortcut is in the destination folder.'),
                           'type': 'success', 'sticky': False,
                           'next': {'type': 'ir.actions.act_window_close'}}}
