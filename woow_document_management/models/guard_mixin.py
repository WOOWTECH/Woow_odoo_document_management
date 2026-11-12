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
    # fields that create refuses or checks when the caller passes them, or that the server sets
    _woow_own_default_fields = frozenset()

    @api.model
    def _add_missing_default_values(self, values):
        """A field of ``_woow_own_default_fields`` that was not passed takes its own default.

        ``default_get`` also reads the stored defaults of the caller (``ir.default``). Every
        internal user may store their own, an administrator may store them for the whole site,
        field groups do not apply to them, and ``create`` only checks the keys it was given.
        Such a field is completed from the field's own default, or left out when it has none.
        """
        completed = super()._add_missing_default_values(values)
        if not self.env.su:
            for name in self._woow_own_default_fields.intersection(completed).difference(values):
                field = self._fields[name]
                if field.default:
                    completed[name] = field.convert_to_write(
                        field.convert_to_cache(field.default(self), self, validate=False), self)
                else:
                    del completed[name]
        return completed

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

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        """Every field a domain starts from follows its read groups.

        Core checks field groups only for columns it reads (``_field_to_sql``);
        many2many and one2many terms query their relation directly. Record rules
        run as superuser and are not affected.
        """
        if not self.env.su and domain:
            names = [term[0].split('.', 1)[0] for term in domain
                     if isinstance(term, (list, tuple)) and term and isinstance(term[0], str)]
            if names:
                self.check_field_access_rights('read', names)
        return super()._search(domain, offset=offset, limit=limit, order=order)

    @api.model
    def _read_group_groupby(self, groupby_spec, query):
        """Group only by fields the caller may read (many2many labels come from sudo)."""
        if not self.env.su:
            self.check_field_access_rights('read', [
                groupby_spec.split(':', 1)[0].split('.', 1)[0]])
        return super()._read_group_groupby(groupby_spec, query)

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
