# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Shared creation and administration guard for document models."""
from odoo import _, api, models
from odoo.exceptions import AccessError, UserError


def own_default_values(model, values, completed, names):
    """``completed``, with the own default of each field of ``names`` that ``values`` leaves out.

    ``completed`` is what ``_add_missing_default_values`` returned: ``default_get`` also reads the
    stored defaults of the caller (``ir.default``; every internal user may store their own and an
    administrator may store them for the whole site, which keep applying to a call made with
    ``sudo()``) and the ``default_<field>`` keys of the context. Create checks only the keys it is
    given, so a field that the server sets, or that a check refuses when it is passed, takes the
    field's own default, or is left out when it has none. Values that were passed stay as they are.
    """
    for name in names.intersection(completed).difference(values):
        field = model._fields[name]
        if field.default:
            completed[name] = field.convert_to_write(
                field.convert_to_cache(field.default(model), model, validate=False), model)
        else:
            del completed[name]
    return completed


class WoowServerFieldsMixin(models.AbstractModel):
    _name = 'woow.document.server.fields.mixin'
    _description = 'Fields that only the server sets'

    # The fields that only the server sets, and the ones that create refuses or checks when the
    # caller passes them. Every model of the module declares its own (``frozenset()`` when all
    # of its fields are filled in by users); ``tools/tests`` checks that none is left out.
    _woow_own_default_fields = frozenset()

    @api.model
    def _add_missing_default_values(self, values):
        """Create takes the own default of ``_woow_own_default_fields``, whoever calls it.

        Used by ``create`` only: ``default_get``, the prefill of a form and the values that
        were passed are not touched, and neither is the superuser: a call made with ``sudo()``
        keeps the uid of the caller, and so the defaults that this user stored.
        """
        return own_default_values(
            self, values, super()._add_missing_default_values(values),
            self._woow_own_default_fields)


class WoowDocumentGuardMixin(models.AbstractModel):
    _name = 'woow.document.guard.mixin'
    _inherit = ['woow.document.server.fields.mixin']
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
            for vals in vals_list:
                model.check_field_access_rights('create', list(vals))
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
        """Duplicating folders and documents is not offered."""
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
