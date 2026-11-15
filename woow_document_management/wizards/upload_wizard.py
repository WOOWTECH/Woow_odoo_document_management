# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Upload several files to one folder in one step, through the normal document create."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.document_folder import _m2o_value
from ..models.quota import LEDGER_KEY, QuotaLedger
from ..models.upload_policy import WoowUploadRejected
from ..services.constants import MIB

# Most refused files that the result message names; the count of the others is still given.
_NAMED_REFUSALS = 10
# Longest file name that the message of a refusal repeats.
_NAME_IN_MESSAGE = 80
# Longest file name that a line keeps (the usual limit of a file system).
LINE_NAME_MAX_CHARS = 255
# Upload dialogs of one user that keep their files: when a newer one is made, the files of the
# older ones are removed (they were abandoned, and the rows would stay until the transient
# vacuum), so that what a user parks in the database stays bounded.
WIZARDS_PER_USER = 5


class WoowDocumentUploadWizard(models.TransientModel):
    _name = 'woow.document.upload.wizard'
    _inherit = ['woow.document.wizard.refs.mixin', 'woow.document.server.fields.mixin']
    _description = 'Upload documents'
    _woow_own_default_fields = frozenset()      # every field is filled in by the user in the form

    folder_id = fields.Many2one('woow.document.folder', required=True, ondelete='cascade')
    tag_ids = fields.Many2many('woow.document.tag', 'woow_document_upload_wizard_tag_rel',
                               'wizard_id', 'tag_id')
    line_ids = fields.One2many('woow.document.upload.wizard.line', 'wizard_id', string='Files')

    def _woow_visible_refs(self):
        return {
            'folder_id': ('woow.document.folder', _('The target folder is unavailable.')),
            'tag_ids': ('woow.document.tag', _('One or more selected tags are unavailable.')),
        }

    @api.model_create_multi
    def create(self, vals_list):
        """Keep the files of the newest dialogs of the user only (see ``WIZARDS_PER_USER``)."""
        if not self.env.su and len(vals_list) > WIZARDS_PER_USER:
            raise UserError(_('Too many dialogs at once.'))
        wizards = super().create(vals_list)
        if not self.env.su:
            mine = self.search([('create_uid', '=', self.env.uid)], order='id desc')
            abandoned = mine[WIZARDS_PER_USER:]
            if abandoned:
                self.env['woow.document.upload.wizard.line'].search(
                    [('wizard_id', 'in', abandoned.ids)]).unlink()
        return wizards

    def action_upload(self):
        """Create one document per file in the chosen folder; a refused file does not stop the others."""
        self.check_access('write')
        self.ensure_one()
        user = self.env.user
        if not user.has_group('woow_document_management.group_woow_document_user'):
            raise AccessError(_('You cannot upload documents.'))
        lines = self.line_ids
        if not lines:
            raise UserError(_('Add at least one file.'))
        if len(lines) > self.env['woow.document.settings']._get_settings().max_files_per_upload:
            raise UserError(_('Invalid number of files.'))
        folder = self.env['woow.document.folder'].search([
            ('id', '=', self.folder_id.id), ('in_trash', '=', False)], limit=1)
        if not folder:
            raise AccessError(_('The target folder is unavailable.'))
        folder.check_access('read')
        folder._woow_require_rank(2)
        tags = self.env['woow.document.tag'].search([('id', 'in', self.tag_ids.ids)])
        if len(tags) != len(self.tag_ids):
            raise AccessError(_('One or more selected tags are unavailable.'))

        created = self.env['woow.document']
        refused = []
        # One quota measurement for the whole upload, and the bytes it adds as it goes.
        documents = self.env['woow.document'].with_context(
            woow_log_channel='backend', **{LEDGER_KEY: QuotaLedger()})
        ledger = documents.env.context[LEDGER_KEY]
        for line in lines:
            label = line.name or _('(unnamed file)')
            if not line.name or not line.datas:
                refused.append((label, _('The file has no name or no content.')))
                continue
            try:
                with self.env.cr.savepoint():
                    created |= documents.create({
                        'name': line.name, 'folder_id': folder.id,
                        'datas': line.datas, 'tag_ids': [(6, 0, tags.ids)]})
            except (WoowUploadRejected, UserError, AccessError) as error:
                refused.append((label, str(error)))
                # The savepoint released the locks that the failed line took, perhaps after the
                # quota had accepted it: the next line measures the stored bytes again (what the
                # run has added so far is kept).
                ledger.base = None
        # The temporary rows are not needed any more, whatever the result.
        lines.unlink()
        if refused and not created:
            raise UserError(self._refusal_message(refused, 0))
        message = _('%(added)s added.', added=len(created))
        if refused:
            message = _('%(added)s added; %(refused)s refused. %(names)s',
                        added=len(created), refused=len(refused),
                        names=self._refusal_message(refused, len(created), names_only=True))
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Upload finished'), 'message': message,
                           'type': 'warning' if refused else 'success', 'sticky': bool(refused),
                           'next': {'type': 'ir.actions.act_window_close'}}}

    @api.model
    def _short_name(self, name):
        """A file name as the messages repeat it: long names are cut."""
        name = name or ''
        return name if len(name) <= _NAME_IN_MESSAGE else name[:_NAME_IN_MESSAGE] + '\u2026'

    @api.model
    def _refusal_message(self, refused, added, names_only=False):
        """The refused files by name (the first few), each with its reason."""
        shown = '; '.join('%s: %s' % (self._short_name(name), reason)
                          for name, reason in refused[:_NAMED_REFUSALS])
        if len(refused) > _NAMED_REFUSALS:
            shown += _('; and %(count)s more', count=len(refused) - _NAMED_REFUSALS)
        if names_only:
            return shown
        return _('No files were uploaded: %(reasons)s', reasons=shown)


class WoowDocumentUploadWizardLine(models.TransientModel):
    _name = 'woow.document.upload.wizard.line'
    _inherit = ['woow.document.wizard.refs.mixin', 'woow.document.server.fields.mixin']
    _description = 'File of an upload'
    _woow_own_default_fields = frozenset({'file_size'})

    wizard_id = fields.Many2one('woow.document.upload.wizard', required=True, ondelete='cascade',
                                index=True)
    name = fields.Char()
    # The bytes stay in the row of this temporary table, not in an attachment of their own.
    datas = fields.Binary(attachment=False)
    # Set by the server from ``datas``: the sum of it is what a user keeps waiting.
    file_size = fields.Integer(readonly=True)

    def _woow_visible_refs(self):
        return {
            'wizard_id': ('woow.document.upload.wizard', _('The upload is unavailable.')),
        }

    @api.model_create_multi
    def create(self, vals_list):
        """Bound the name, the size and the number of the files that wait in the database."""
        if not self.env.su:
            vals_list = [self._woow_checked_values(vals) for vals in vals_list]
        lines = super().create(vals_list)
        if not self.env.su:
            lines._woow_check_totals()
        return lines

    def write(self, vals):
        if self.env.su:
            return super().write(vals)
        vals = self._woow_checked_values(vals)
        result = super().write(vals)
        if {'datas', 'wizard_id'}.intersection(vals):
            self._woow_check_totals()
        return result

    @api.model
    def _woow_checked_values(self, vals):
        """The values of one line, checked, with the size that the server computes from the file.

        Only a length check of the content: the file itself is validated, with the upload
        policy and the quota, when the upload is confirmed, so that a refused file is named
        in the result and does not stop the others.
        """
        if 'file_size' in vals:
            raise AccessError(_('This field is set by the server.'))
        vals = dict(vals)
        if 'wizard_id' in vals:
            # The ORM keeps no record in its cache for a text or a float but the database column
            # keeps the number: the id that is counted is the id that is stored.
            wizard_id = _m2o_value(vals['wizard_id'], 'woow.document.upload.wizard')
            if not wizard_id or wizard_id < 1:
                raise AccessError(_('The upload is unavailable.'))
            vals['wizard_id'] = wizard_id
        name = vals.get('name')
        if name and (not isinstance(name, str) or len(name) > LINE_NAME_MAX_CHARS):
            raise WoowUploadRejected('filename')
        if 'datas' in vals:
            encoded = vals['datas']
            if encoded and not isinstance(encoded, (str, bytes)):
                raise WoowUploadRejected('content')
            limit = self.env['woow.document.settings']._get_settings().internal_max_upload_mb
            size = len(encoded) * 3 // 4 if encoded else 0
            if size > limit * MIB + 3:
                raise WoowUploadRejected('size', limit=limit)
            vals['file_size'] = size
        return vals

    def _woow_check_totals(self):
        """What waits in the database for one wizard and for one user stays bounded.

        The count is made on the stored rows after they were written (the caller's transaction is
        rolled back when it is refused), and not on the values that were passed.
        """
        settings = self.env['woow.document.settings']._get_settings()
        limit = settings.max_files_per_upload
        counted = self._read_group([('wizard_id', 'in', self.wizard_id.ids)], ['wizard_id'],
                                   ['__count'])
        if any(count > limit for _wizard, count in counted):
            raise UserError(_('Invalid number of files.'))
        mine = self._read_group([('create_uid', '=', self.env.uid)], [],
                                ['__count', 'file_size:sum'])
        count, size = mine[0] if mine else (0, 0)
        if count > limit * WIZARDS_PER_USER:
            raise UserError(_('Invalid number of files.'))
        # The most that one upload may hold, over all the dialogs of the user.
        if (size or 0) > limit * settings.internal_max_upload_mb * MIB:
            raise WoowUploadRejected('size', limit=limit * settings.internal_max_upload_mb)
