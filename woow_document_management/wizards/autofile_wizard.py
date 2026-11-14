# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""File the existing attachments of a filing rule's kind, one batch at a time."""
from odoo import _, fields, models
from odoo.exceptions import UserError


class WoowDocumentAutofileWizard(models.TransientModel):
    _name = 'woow.document.autofile.wizard'
    _description = 'File existing attachments'

    rule_id = fields.Many2one('woow.document.auto.rule', string='Filing rule', required=True,
                              ondelete='cascade')
    looked = fields.Integer(string='Looked at', readonly=True)
    filed = fields.Integer(string='Filed', readonly=True)
    refused = fields.Integer(string='Not accepted', readonly=True)
    skipped = fields.Integer(string='Left out', readonly=True)
    left = fields.Integer(string='Still to look at', readonly=True)
    ran = fields.Boolean(readonly=True)

    def _woow_reopen(self):
        """The action that shows this wizard (again) with its counts."""
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'views': [(False, 'form')], 'target': 'new'}

    def action_file(self):
        """File the next batch of the rule's attachments and show the counts."""
        self.check_access('write')
        self.ensure_one()
        self.env['woow.document.auto.rule']._require_manager()
        rule = self.rule_id
        if not rule.active:
            raise UserError(_('Activate the filing rule first.'))
        counts = rule._woow_backfill()
        self.write(dict(counts, ran=True))
        return self._woow_reopen()
