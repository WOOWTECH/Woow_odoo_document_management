# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Favourites ("stars"): one row per user and document, private to the user who made it."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .document_folder import _m2o_value

# Most favourites one call may create (the star button makes one; this bounds a direct call).
FAVORITES_PER_CALL = 100


class WoowDocumentFavorite(models.Model):
    _name = 'woow.document.favorite'
    _inherit = ['woow.document.guard.mixin']
    _description = 'Document favourite'
    _order = 'id desc'
    # The user is always the caller and the company is the one of the document.
    _woow_own_default_fields = frozenset({'user_id', 'company_id'})
    _sql_constraints = [
        ('document_user_uniq', 'unique(document_id, user_id)',
         'This document is already a favourite.'),
    ]

    document_id = fields.Many2one('woow.document', required=True, index=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', required=True, index=True, ondelete='cascade',
                              default=lambda self: self.env.user)
    company_id = fields.Many2one(related='document_id.company_id', store=True, index=True)

    @api.model_create_multi
    def create(self, vals_list):
        """A user stars documents of their own, and only the ones the record rules let them read."""
        self.check_access('create')
        if not self.env.su:
            if len(vals_list) > FAVORITES_PER_CALL:
                raise UserError(_('Too many documents at once.'))
            uid = self.env.uid
            wanted = []
            prepared = []
            for incoming in vals_list:
                if set(incoming) - {'document_id', 'user_id'}:
                    raise AccessError(_('This field is set by the server.'))
                if 'user_id' in incoming and _m2o_value(incoming['user_id'], 'res.users') != uid:
                    raise AccessError(_('You can only star documents for yourself.'))
                document_id = _m2o_value(incoming.get('document_id'), 'woow.document')
                if not document_id or document_id < 1:
                    raise AccessError(_('This document is unavailable.'))
                wanted.append(document_id)
                # The ids that were judged are the ids that are stored.
                prepared.append({'document_id': document_id, 'user_id': uid})
            if len(set(wanted)) != len(wanted):
                raise UserError(_('This document is already a favourite.'))
            # A search under the caller's record rules: a document that is missing, hidden or
            # in the trash is refused with the same message.
            visible = self.env['woow.document'].search([('id', 'in', wanted)])
            if len(visible) != len(wanted):
                raise AccessError(_('This document is unavailable.'))
            if self.search_count([('document_id', 'in', wanted), ('user_id', '=', uid)]):
                raise UserError(_('This document is already a favourite.'))
            vals_list = prepared
        return super().create(vals_list)

    def write(self, vals):
        """A favourite is made or removed, never edited."""
        if not self.env.su:
            raise AccessError(_('Favourites cannot be edited.'))
        return super().write(vals)
