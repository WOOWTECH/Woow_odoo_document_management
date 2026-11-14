# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Filing rules: files attached to a kind of record are also filed as documents.

A document administrator makes a rule for a kind of record (a project, say): the target folder,
the tags and the owner that the filed documents get. From then on every file that is attached to
a record of that kind is also made a document in that folder. The rule is the authorisation: the
person who attaches the file is usually no member of the folder, so the document is made as the
superuser (the one elevation of this feature, ``_woow_elevated``); the upload policy and the quota
still apply, as for every other document.

Filing must never break or slow the creation of an attachment: the kinds that have a rule are
kept in a cache that the rules clear when they change (an attachment of a kind without a rule
costs no query), every filing runs in a savepoint of its own, and any failure is swallowed after
that savepoint is rolled back.
"""
import base64
import logging

from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.constants import (
    AUTO_FILE_BACKFILL_BATCH, CTX_AUTO_FILE, MIB, WOOW_MODELS)
from .document import SOURCE_CTX_KEY, _SOURCE_SENTINEL
from .quota import LEDGER_KEY, QuotaLedger

_logger = logging.getLogger(__name__)

_MANAGER = 'woow_document_management.group_woow_document_manager'
_USER = 'woow_document_management.group_woow_document_user'

# Only a context that carries this in-process object is a filing in progress (no rule runs inside
# it). A context sent by a client can never hold it, so a client cannot switch filing off for its
# own files.
_AUTO_FILE_SENTINEL = object()

# The fields of a rule that only the server writes (the kind's name follows the kind, the
# company follows the folder).
_SERVER_FIELDS = frozenset({'res_model', 'company_id', 'backfill_cursor'})

# The kinds of record a rule cannot be made for.
_FORBIDDEN_PREFIXES = ('ir.', 'mail.', 'woow.document')


def _kind_is_allowed(registry, name):
    """Whether a rule may name the model ``name``: a real, stored model of another app."""
    if not name or name in WOOW_MODELS or name.startswith(_FORBIDDEN_PREFIXES):
        return False
    model = registry.get(name)
    return model is not None and not model._abstract and not model._transient


def _auto_state(env):
    """``_woow_auto_state`` of the rules; no kind and no trash while the rules cannot be read."""
    try:
        return env['woow.document.auto.rule']._woow_auto_state()
    except Exception as error:  # noqa: BLE001 - an attachment is made whatever happens here
        _logger.info('The filing rules could not be read (%s)', type(error).__name__)
        return {}, False


class WoowDocumentAutoRule(models.Model):
    _name = 'woow.document.auto.rule'
    _inherit = ['woow.document.guard.mixin']
    _description = 'Document filing rule'
    _order = 'name, id'
    _woow_own_default_fields = frozenset({'company_id', 'res_model', 'backfill_cursor'})

    name = fields.Char(required=True)
    model_id = fields.Many2one(
        'ir.model', string='Kind of record', required=True, ondelete='cascade', index=True,
        domain=[('transient', '=', False), '!', ('model', '=like', 'ir.%'),
                '!', ('model', '=like', 'mail.%'), '!', ('model', '=like', 'woow.document%')],
        help='Files attached to records of this kind are also filed as documents.')
    res_model = fields.Char(related='model_id.model', store=True, index=True, readonly=True)
    folder_id = fields.Many2one(
        'woow.document.folder', string='Target folder', required=True, ondelete='restrict',
        index=True)
    tag_ids = fields.Many2many(
        'woow.document.tag', 'woow_document_auto_rule_tag_rel', 'rule_id', 'tag_id',
        string='Tags')
    owner_id = fields.Many2one(
        'res.users', string='Owner', ondelete='set null',
        domain=[('share', '=', False)],
        help='The owner of the filed documents. A document user who may upload to the folder.')
    trash_on_delete = fields.Boolean(
        string='Trash when the original is deleted', default=True,
        help='Move the filed document to the trash when the attachment it came from is deleted.')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(related='folder_id.company_id', store=True, index=True)
    backfill_cursor = fields.Integer(readonly=True, copy=False)
    filed_count = fields.Integer(string='Filed documents', compute='_compute_filed_count')

    # -- validation ----------------------------------------------------------------------------
    @api.constrains('model_id')
    def _check_kind(self):
        for rule in self:
            if not _kind_is_allowed(self.env.registry, rule.res_model):
                raise ValidationError(_('Choose a kind of record from another app.'))

    @api.model
    def _woow_owner_acceptable(self, owner, folder):
        """Whether ``owner`` may own the documents that a rule files in ``folder``.

        The owner is an active internal document user. A document administrator needs no grant;
        any other owner has to hold the upload level of the folder (as a user or through a group),
        which is what the owner rule of a document needs to give the owner anything at all.
        """
        if not owner or not owner.active or not owner._is_internal() or not owner.has_group(_USER):
            return False
        if owner.has_group(_MANAGER):
            return True
        folder = folder.with_context(active_test=False)
        self.env.flush_all()    # the effective grants of the folder are stored computed fields
        return bool(folder.search_count([('id', '=', folder.id)] + folder._woow_rank_domain(2, owner)))

    @api.constrains('owner_id', 'folder_id')
    def _check_owner(self):
        for rule in self:
            if rule.owner_id and not self._woow_owner_acceptable(rule.owner_id, rule.folder_id):
                raise ValidationError(
                    _('Choose a document user who may upload to the target folder.'))

    @api.constrains('tag_ids', 'folder_id')
    def _check_tags(self):
        for rule in self:
            company = rule.company_id
            if company and any(tag.company_id and tag.company_id != company
                               for tag in rule.tag_ids):
                raise ValidationError(_('A tag of another company cannot be used here.'))

    def _compute_filed_count(self):
        counts = {}
        if self.ids:
            groups = self.env['woow.document'].with_context(active_test=False)._read_group(
                [('auto_rule_id', 'in', self.ids)], ['auto_rule_id'], ['__count'])
            counts = {rule.id: count for rule, count in groups}
        for rule in self:
            rule.filed_count = counts.get(rule.id, 0)

    # -- changes of the rules --------------------------------------------------------------------
    def _woow_audit_change(self):
        for rule in self:
            self.env['woow.document.log']._log_event(
                'settings_change', channel=self._woow_log_channel(), folder_id=rule.folder_id.id,
                company_id=rule.company_id.id, detail={'rule': rule.id})

    def _woow_refuse_server_fields(self, vals_list):
        """Only the server sets the kind's name, the company and the backfill position of a rule."""
        if not self.env.su and any(_SERVER_FIELDS.intersection(vals) for vals in vals_list):
            raise AccessError(_('This field of a filing rule is set by the server.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._woow_refuse_server_fields(vals_list)
        rules = super().create(vals_list)
        self.env.registry.clear_cache()
        rules._woow_audit_change()
        return rules

    def write(self, vals):
        self._woow_refuse_server_fields([vals])
        result = super().write(vals)
        self.env.registry.clear_cache()
        if set(vals) - {'backfill_cursor'}:
            self._woow_audit_change()
        return result

    def unlink(self):
        self._woow_audit_change()
        result = super().unlink()
        self.env.registry.clear_cache()
        return result

    # -- the cache of the kinds that have a rule -------------------------------------------------
    def _woow_elevated(self):
        """The records of ``self`` as the superuser, inside a filing (the one elevation of the lane).

        The rule made by a document administrator is the authorisation. The environment carries
        the in-process marker of a filing, so that nothing inside it triggers another filing.
        """
        return self.sudo().with_context(  # sudo-ok: A1
            **{CTX_AUTO_FILE: _AUTO_FILE_SENTINEL})

    @api.model
    @tools.ormcache()
    def _woow_auto_state(self):
        """``({kind: ((rule id, company id), ...)}, trash)`` of the rules, read once and cached.

        The first part holds the active rules by the technical name of their kind; ``trash`` tells
        whether any rule (active or not) trashes a filed document when its original is deleted.
        The rules clear the cache whenever one of them is created, changed or deleted.
        """
        # a savepoint: while the table of the rules does not exist yet (an upgrade that is half
        # way) the read fails and must not abort the transaction of the caller; nothing is cached
        with self.env.cr.savepoint(flush=False):
            rules = self._woow_elevated().with_context(active_test=False).search([])
        by_kind = {}
        for rule in rules.filtered('active'):
            by_kind.setdefault(rule.res_model, []).append((rule.id, rule.company_id.id))
        return ({kind: tuple(items) for kind, items in by_kind.items()},
                any(rules.mapped('trash_on_delete')))

    # -- filing ----------------------------------------------------------------------------------
    @api.model
    def _woow_fileable(self, attachment):
        """Whether an attachment of a record is a file that a rule may file."""
        return bool(attachment.res_model and attachment.res_id and not attachment.res_field
                    and attachment.type == 'binary' and not attachment.public
                    and attachment.file_size)

    def _woow_file_attachment(self, attachment, channel, *, ledger=None):
        """File ``attachment`` as a document of the rule and return it.

        ``self`` is the rule and ``attachment`` the file, both in the elevated environment
        (``_woow_elevated``). Everything the policy refuses raises; the callers run this in a
        savepoint and decide what a failure means.
        """
        self.ensure_one()
        folder = self.folder_id
        settings = self.env['woow.document.settings']._get_settings()
        if attachment.file_size > settings.internal_max_upload_mb * MIB:
            # the policy would refuse it; do not read a huge file just to be told so
            raise UserError(_('The file was not accepted.'))
        owner = self.owner_id if self._woow_owner_acceptable(self.owner_id, folder) \
            else self.env['res.users']
        context = {SOURCE_CTX_KEY: _SOURCE_SENTINEL}
        if ledger is not None:
            context[LEDGER_KEY] = ledger
        document = self.env['woow.document'].with_context(**context).create({
            'name': attachment.name, 'folder_id': folder.id,
            'datas': base64.b64encode(attachment.raw),
            'owner_id': owner.id or False, 'tag_ids': [(6, 0, self.tag_ids.ids)],
            'source_res_model': attachment.res_model, 'source_res_id': attachment.res_id,
            'source_attachment_id': attachment.id, 'auto_rule_id': self.id})
        self.env['woow.document.log']._log_event(
            'auto_file', channel=channel, document_id=document.id, folder_id=folder.id,
            company_id=folder.company_id.id, detail={'rule': self.id})
        return document

    @api.model
    def _woow_auto_file(self, attachments, unfiled=False):
        """File the ``attachments`` that a rule asks for; never raises, never blocks the caller.

        At most ``AUTO_FILE_BACKFILL_BATCH`` files are filed by one call; the rest of a very large
        batch is left for the backfill of the rule. New attachments pass ``unfiled=False``;
        attachments that already existed (moved onto a record) pass ``unfiled=True`` and the ones
        that a rule filed before are left out (one query, only when a kind with a rule is met).
        """
        try:
            self._woow_auto_file_checked(attachments, unfiled)
        except Exception as error:  # noqa: BLE001 - filing must never break an attachment
            _logger.info('Filing skipped (%s)', type(error).__name__)

    @api.model
    def _woow_auto_file_checked(self, attachments, unfiled):
        by_kind = _auto_state(self.env)[0]
        work = []
        for attachment in attachments:
            for rule_id, company_id in by_kind.get(attachment.res_model, ()):
                # a rule of a company files only what was attached in that company
                if (self._woow_fileable(attachment)
                        and (not company_id or not attachment.company_id
                             or attachment.company_id.id == company_id)):
                    work.append((attachment.id, rule_id))
            if len(work) >= AUTO_FILE_BACKFILL_BATCH:
                work = work[:AUTO_FILE_BACKFILL_BATCH]
                break
        if work and unfiled:
            with self.env.cr.savepoint():
                done = {
                    (document.source_attachment_id.id, document.auto_rule_id.id)
                    for document in self._woow_elevated().env['woow.document'].with_context(
                        active_test=False).search([
                            ('source_attachment_id', 'in', [item[0] for item in work]),
                            ('auto_rule_id', 'in', [item[1] for item in work])])}
            work = [item for item in work if item not in done]
        if not work:
            return
        channel = self._woow_log_channel()
        elevated = self._woow_elevated()
        for attachment_id, rule_id in work:
            try:
                with self.env.cr.savepoint():
                    elevated.browse(rule_id)._woow_file_attachment(
                        elevated.env['ir.attachment'].browse(attachment_id), channel)
            except Exception as error:  # noqa: BLE001 - filing must never break an attachment
                _logger.info('Filing rule %s skipped attachment %s (%s)',
                             rule_id, attachment_id, type(error).__name__)

    @api.model
    def _woow_trash_filed(self, attachment_ids):
        """The ids of the filed documents that go to the trash with ``attachment_ids``.

        One query. Documents under legal hold, trashed ones and the ones whose rule does not ask
        for it are left out.
        """
        documents = self._woow_elevated().env['woow.document'].with_context(active_test=False)
        return documents.search([
            ('source_attachment_id', 'in', attachment_ids), ('active', '=', True),
            ('legal_hold', '=', False), ('auto_rule_id.trash_on_delete', '=', True)]).ids

    @api.model
    def _woow_do_trash(self, document_ids):
        """Move the documents of ``document_ids`` to the trash; a failure is swallowed."""
        try:
            with self.env.cr.savepoint():
                self._woow_elevated().env['woow.document'].browse(document_ids).action_trash()
        except Exception as error:  # noqa: BLE001 - deleting an attachment must go on
            _logger.info('Trash of %s filed documents skipped (%s)',
                         len(document_ids), type(error).__name__)

    # -- the backfill ----------------------------------------------------------------------------
    def _woow_backfill_domain(self):
        """The attachments of the rule's kind that are not filed by this rule yet."""
        self.ensure_one()
        domain = [
            ('res_model', '=', self.res_model), ('res_id', '>', 0), ('res_field', '=', False),
            ('type', '=', 'binary'), ('public', '=', False), ('file_size', '>', 0),
            ('woow_filed_document_ids', 'not any', [('auto_rule_id', '=', self.id)])]
        if self.company_id:
            domain += ['|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)]
        return domain

    def _woow_backfill(self):
        """Look at the next batch of unfiled attachments of the rule's kind and file them.

        Returns ``{'looked': n, 'filed': n, 'refused': n, 'left': n}``. One run looks at no more
        than ``AUTO_FILE_BACKFILL_BATCH`` attachments after the cursor of the rule; a run that
        ends the list sets the cursor back to the start, so the next run begins again and
        files what an earlier run could not. A run can be repeated at any time: an attachment
        that a rule has filed (a trashed document included) is not filed again.
        """
        self.ensure_one()
        channel = self._woow_log_channel()
        rule = self._woow_elevated()
        attachments = rule.env['ir.attachment'].with_context(active_test=False)
        batch = attachments.search(
            rule._woow_backfill_domain() + [('id', '>', rule.backfill_cursor)],
            order='id', limit=AUTO_FILE_BACKFILL_BATCH)
        ledger = QuotaLedger()
        filed = refused = 0
        for attachment in batch:
            try:
                with self.env.cr.savepoint():
                    rule._woow_file_attachment(attachment, channel, ledger=ledger)
                filed += 1
            except Exception as error:  # noqa: BLE001 - one file never stops the run
                refused += 1
                _logger.info('Backfill of rule %s skipped attachment %s (%s)',
                             rule.id, attachment.id, type(error).__name__)
        finished = len(batch) < AUTO_FILE_BACKFILL_BATCH
        cursor = 0 if finished else batch[-1].id
        super(WoowDocumentAutoRule, rule).write({'backfill_cursor': cursor})
        left = 0 if finished else attachments.search_count(
            rule._woow_backfill_domain() + [('id', '>', cursor)])
        return {'looked': len(batch), 'filed': filed, 'refused': refused, 'left': left}

    def action_open_backfill(self):
        """Open the wizard that files the existing attachments of this rule's kind."""
        self._require_manager()
        self.ensure_one()
        wizard = self.env['woow.document.autofile.wizard'].create({'rule_id': self.id})
        return wizard._woow_reopen()


class IrAttachmentAutoFile(models.Model):
    _inherit = 'ir.attachment'

    woow_filed_document_ids = fields.One2many(
        'woow.document', 'source_attachment_id', string='Filed documents', copy=False,
        readonly=True, groups=_MANAGER)

    @api.model_create_multi
    def create(self, vals_list):
        attachments = super().create(vals_list)
        if self.env.context.get(CTX_AUTO_FILE) is not _AUTO_FILE_SENTINEL:
            if _auto_state(self.env)[0]:
                self.env['woow.document.auto.rule']._woow_auto_file(attachments)
        return attachments

    def write(self, vals):
        result = super().write(vals)
        # A file that is attached to a record after it was made (a file of a chatter post is moved
        # onto the record when the post is sent) is filed when it lands on a kind with a rule.
        if (('res_model' in vals or 'res_id' in vals) and self
                and self.env.context.get(CTX_AUTO_FILE) is not _AUTO_FILE_SENTINEL):
            if _auto_state(self.env)[0]:
                self.env['woow.document.auto.rule']._woow_auto_file(self, unfiled=True)
        return result

    def unlink(self):
        # The documents are looked up before the rows go: the link to them is cleared with the row.
        documents = []
        if self and _auto_state(self.env)[1]:
            documents = self.env['woow.document.auto.rule']._woow_trash_filed(self.ids)
        result = super().unlink()
        if documents:
            self.env['woow.document.auto.rule']._woow_do_trash(documents)
        return result


class WoowDocumentAutoFile(models.Model):
    _inherit = 'woow.document'

    source_attachment_id = fields.Many2one(
        'ir.attachment', ondelete='set null', index=True, copy=False, readonly=True,
        groups=_MANAGER, help='The attachment of another app that this document was filed from.')
    auto_rule_id = fields.Many2one(
        'woow.document.auto.rule', string='Filing rule', ondelete='set null', index=True,
        copy=False, readonly=True, groups=_MANAGER)


class WoowDocumentFolderAutoFile(models.Model):
    _inherit = 'woow.document.folder'

    @api.ondelete(at_uninstall=True)
    def _woow_unlink_auto_rules(self):
        """The rules that file into a folder go with it (through the ORM, so the cache follows)."""
        self.env['woow.document.auto.rule'].with_context(active_test=False).search(
            [('folder_id', 'in', self.ids)]).unlink()
