# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Guard mixin inherited by every model of the module (IF-MODEL, owned by core-models-acl).

Day-0 stub, fail closed: non-superuser creation is refused until core-models-acl lands.
"""
from odoo import _, api, models
from odoo.exceptions import AccessError, UserError


class WoowDocumentGuardMixin(models.AbstractModel):
    _name = 'woow.document.guard.mixin'
    _description = 'Document management guard'

    # each model overrides this, e.g. frozenset({'default_folder_id'})
    _woow_allowed_defaults = frozenset()

    @api.model_create_multi
    def create(self, vals_list):
        """Drop context default_* keys outside the allowlist; check field groups when not su.

        Day-0 stub: any non-superuser creation with values raises AccessError.
        """
        if not self.env.su and any(vals_list):
            raise AccessError(_('This action is not available yet.'))  # WOOW-STUB
        return super().create(vals_list)

    def copy(self, default=None):
        """Duplicating folders, documents and share links is not offered."""
        raise UserError(_('Duplicating this record is not supported.'))

    def _require_manager(self):
        """AccessError unless superuser or document administrator. Day-0 stub: su only."""
        if not self.env.su:
            raise AccessError(_('Only document administrators can do this.'))  # WOOW-STUB

    def _woow_log_channel(self):
        """Audit channel of the current call: cron, portal, backend or rpc."""
        if self.env.su:
            return 'cron'
        if self.env.user.share:
            return 'portal'
        channel = self.env.context.get('woow_log_channel')
        if channel in ('backend', 'rpc'):
            return channel
        return 'rpc'
