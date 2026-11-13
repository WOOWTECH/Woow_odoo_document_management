# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Protect held documents and remove module data before Odoo drops its models."""
from odoo import _
from odoo.exceptions import UserError

from .models.settings import PARAM_PREFIX, _PARAM_KEYS


def uninstall_hook(env):
    """Run as Odoo's uninstall superuser; never remove a held item."""
    documents = env['woow.document'].with_context(active_test=False).search([])
    folders = env['woow.document.folder'].with_context(active_test=False).search([])
    held = len(documents.filtered('legal_hold')) + len(folders.filtered('legal_hold'))
    if held:
        raise UserError(_(
            'Some items are under legal hold. Release every legal hold before uninstalling.'))
    params = env['ir.config_parameter']
    if documents and params.get_param(PARAM_PREFIX + 'uninstall_confirm') != params.get_param(
            'database.uuid'):
        raise UserError(_(
            'This site still has documents. Download what you need, then enter the confirmation '
            'code in Documents settings before uninstalling.'))
    documents.unlink()
    for folder in sorted(folders, key=lambda item: len(item.parent_path or ''), reverse=True):
        folder.unlink()
    env['ir.config_parameter'].search([
        ('key', 'in', [PARAM_PREFIX + key for key in _PARAM_KEYS])]).unlink()
