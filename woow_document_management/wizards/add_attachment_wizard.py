# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Import readable native attachments as independent document records."""
import base64

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.document import SOURCE_CTX_KEY, _SOURCE_SENTINEL
from ..models.quota import LEDGER_KEY, QuotaLedger
from ..models.upload_policy import WoowUploadRejected


class WoowDocumentAddAttachmentWizard(models.TransientModel):
    _name = 'woow.document.add.attachment.wizard'
    _description = 'Add attachments to a folder'

    attachment_ids = fields.Many2many('ir.attachment')
    folder_id = fields.Many2one('woow.document.folder', required=True)

    def action_add(self):
        """Import only the exact active selection after native attachment checks."""
        self.check_access('write')
        self.ensure_one()
        user = self.env.user
        if not (user.has_group('woow_document_management.group_woow_document_user') and
                user.has_group('woow_document_management.group_woow_document_attachment_overview')):
            raise AccessError(_('You cannot import attachments.'))
        if self.env.context.get('active_model') != 'ir.attachment':
            raise AccessError(_('The attachment selection is unavailable.'))
        ids = self.env.context.get('active_ids')
        if not isinstance(ids, list) or not ids or len(ids) > 100 or any(
                type(item) is not int or item <= 0 for item in ids) or len(set(ids)) != len(ids):
            raise AccessError(_('The attachment selection is unavailable.'))
        source_model = self.env['ir.attachment']
        sources = source_model.browse(ids).exists()
        visible = source_model.search([('id', 'in', ids)])
        if len(sources) != len(ids) or set(visible.ids) != set(ids):
            raise AccessError(_('The attachment selection is unavailable.'))
        for source in sources:
            source.check('read')
        folder = self.env['woow.document.folder'].search([
            ('id', '=', self.folder_id.id), ('in_trash', '=', False)], limit=1)
        if not folder:
            raise AccessError(_('The target folder is unavailable.'))
        folder.check_access('read')
        folder._woow_require_rank(2)

        created = self.env['woow.document']
        rejected = {}
        # One quota measurement for the whole import, and the bytes it adds as it goes.
        imports = self.env['woow.document'].with_context(
            **{SOURCE_CTX_KEY: _SOURCE_SENTINEL, LEDGER_KEY: QuotaLedger()})
        for source in sources:
            data = source.raw if source.type == 'binary' else b''
            if not data:
                rejected[_('The source has no file content.')] = rejected.get(
                    _('The source has no file content.'), 0) + 1
                continue
            vals = {'name': source.name, 'folder_id': folder.id,
                    'datas': base64.b64encode(data)}
            if source.res_model and source.res_id:
                vals.update(source_res_model=source.res_model, source_res_id=source.res_id)
            try:
                with self.env.cr.savepoint():
                    created |= imports.create(vals)
            except WoowUploadRejected as error:
                reason = str(error)
                rejected[reason] = rejected.get(reason, 0) + 1
        reasons = '; '.join('%s (%d)' % (reason, count)
                            for reason, count in sorted(rejected.items()))
        if not created and rejected:
            raise UserError(_('No attachments were imported: %(reasons)s', reasons=reasons))
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Attachments imported'),
                           'message': _('%(added)s added; %(rejected)s rejected. %(reasons)s',
                                        added=len(created), rejected=sum(rejected.values()),
                                        reasons=reasons),
                           'type': 'success' if not rejected else 'warning', 'sticky': False}}
