# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Uninstall hook (IF-UNINST, owned by core-models-acl).

Day-0 stub, fail closed: uninstalling is always refused.
"""
from odoo import _
from odoo.exceptions import UserError


def uninstall_hook(env):
    """Refuse while legal holds or unconfirmed documents exist; then delete with the ORM."""
    raise UserError(_(  # WOOW-STUB
        'This site still has documents. Download what you need, then enter the confirmation '
        'code in Documents settings before uninstalling.'))
