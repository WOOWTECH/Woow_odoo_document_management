# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Shared creation and administration guard for document models."""
from odoo import _, api, models
from odoo.exceptions import AccessError, UserError


class WoowDocumentGuardMixin(models.AbstractModel):
    _name = 'woow.document.guard.mixin'
    _description = 'Document management guard'

    # each model overrides this, e.g. frozenset({'default_folder_id'})
    _woow_allowed_defaults = frozenset()

    @api.model_create_multi
    def create(self, vals_list):
        """Prevent ambient defaults and field groups from bypassing model checks."""
        context = {key: value for key, value in self.env.context.items()
                   if not key.startswith('default_') or key in self._woow_allowed_defaults}
        model = self if len(context) == len(self.env.context) else self.with_context(context)
        if not model.env.su:
            secret_fields = frozenset()
            if model._name == 'woow.document.share':
                from .document_share import SECRET_CTX_KEY, _SECRET_SENTINEL
                if model.env.context.get(SECRET_CTX_KEY) is _SECRET_SENTINEL:
                    secret_fields = frozenset({'token_hash', 'password_hash'})
            for vals in vals_list:
                model.check_field_access_rights('create', [
                    key for key in vals if key not in secret_fields])
        return super(WoowDocumentGuardMixin, model).create(vals_list)

    def copy(self, default=None):
        """Duplicating folders, documents and share links is not offered."""
        raise UserError(_('Duplicating this record is not supported.'))

    def _require_manager(self):
        """Restrict administrative operations to the document administrator group."""
        if not self.env.su and not self.env.user.has_group(
                'woow_document_management.group_woow_document_manager'):
            raise AccessError(_('Only document administrators can do this.'))

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
