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
from ..services.locks import LockBusy
from .document import SOURCE_CTX_KEY, _SOURCE_SENTINEL
from .quota import LEDGER_KEY, UNCHARGED_KEY, UNCHARGED_SENTINEL, QuotaLedger

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

# One backfill run files no more than this many bytes of content (the count of files is limited
# as well), so that a run cannot hold the content of a whole batch of large files in memory.
_BACKFILL_MAX_BYTES = 256 * MIB

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
        'ir.model', string='Model of the kind of record', required=True, ondelete='cascade', index=True,
        domain=[('transient', '=', False), '!', ('model', '=like', 'ir.%'),
                '!', ('model', '=like', 'mail.%'), '!', ('model', '=like', 'woow.document%')],
        help='Files attached to records of this kind are also filed as documents.')
    res_model = fields.Char(related='model_id.model', store=True, index=True, readonly=True)
    kind_model = fields.Selection(
        selection='_woow_kind_choices', string='Kind of record', compute='_compute_kind_model',
        readonly=False, help='Files attached to records of this kind are also filed as documents.')
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
            # a rule is no way to reach records that its maker cannot read
            if not self.env.su and not self.env[rule.res_model].has_access('read'):
                raise ValidationError(_('Choose a kind of record that you may read.'))

    @api.model
    def _woow_kind_choices(self):
        """The kinds of record that a rule can name and the caller may read, for the form.

        The model catalogue (``ir.model``) is open to the technical group only, so the form does
        not pick the kind from it: it lists the registry's own models, as the choices of a field.
        """
        registry = self.env.registry
        choices = []
        for name, model in registry.items():
            if model._auto and _kind_is_allowed(registry, name) and self.env[name].has_access('read'):
                choices.append((name, '%s (%s)' % (model._description or name, name)))
        return sorted(choices, key=lambda choice: choice[1].lower())

    @api.depends('res_model')
    def _compute_kind_model(self):
        for rule in self:
            rule.kind_model = rule.res_model or False

    @api.model
    def _woow_kind_to_model(self, vals):
        """``vals`` with the kind of the form (a technical name) turned into the ``ir.model`` row."""
        if 'kind_model' not in vals:
            return vals
        vals = dict(vals)
        name = vals.pop('kind_model')
        if name:
            model_id = _kind_is_allowed(self.env.registry, name) and self.env['ir.model']._get_id(name)
            if not model_id:
                raise ValidationError(_('Choose a kind of record from another app.'))
            vals['model_id'] = model_id
        return vals

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
        vals_list = [self._woow_kind_to_model(vals) for vals in vals_list]
        rules = super().create(vals_list)
        self.env.registry.clear_cache()
        rules._woow_audit_change()
        return rules

    def write(self, vals):
        self._woow_refuse_server_fields([vals])
        vals = self._woow_kind_to_model(vals)
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

    def _woow_file_attachment(self, attachment, channel, *, ledger=None, uncharged=False):
        """File ``attachment`` as a document of the rule and return it.

        ``self`` is the rule and ``attachment`` the file, both in the elevated environment
        (``_woow_elevated``). Everything the policy refuses raises; the callers run this in a
        savepoint and decide what a failure means. ``uncharged`` tells that the caller already
        measured the stored bytes against the quota (``_check_uncharged``): the content is the
        stored file of the attachment, so the filing adds no bytes and takes no quota lock.
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
        if uncharged:
            context[UNCHARGED_KEY] = UNCHARGED_SENTINEL
        document = self.env['woow.document'].with_context(**context).create({
            'name': attachment.name, 'folder_id': folder.id,
            'datas': base64.b64encode(attachment.raw),
            'owner_id': owner.id or False, 'tag_ids': [(6, 0, self.tag_ids.ids)],
            'source_res_model': attachment.res_model, 'source_res_id': attachment.res_id,
            'source_attachment_id': attachment.id, 'auto_rule_id': self.id})
        self.env['woow.document.log']._log_event(
            'auto_file', channel=channel, document_id=document.id, folder_id=folder.id,
            company_id=folder.company_id.id, detail={'rule': self.id})
        # the content of the file is not kept in the cache of the run
        attachment.invalidate_recordset(['raw', 'datas'])
        document.invalidate_recordset(['datas'])
        return document

    # -- where an attachment belongs ---------------------------------------------------------------
    @api.model
    def _woow_record_scope(self, attachments):
        """``{attachment id: company id}`` of the attachments whose record exists.

        The company is the one of the record the file is attached to (the field of the attachment
        is the company of whoever made it, and a caller may set it freely); a record of a kind that
        has no company is judged by the company of the attachment. ``False`` means no company.
        An attachment whose record does not exist is left out. Read as the superuser: ``self`` is
        used in the elevated environment, the records are only looked at, never changed.
        """
        elevated = self._woow_elevated().env
        by_model = {}
        for attachment in elevated['ir.attachment'].browse(attachments.ids):
            by_model.setdefault(attachment.res_model, []).append(attachment)
        scope = {}
        for model_name, items in by_model.items():
            if model_name not in elevated:
                continue
            records = elevated[model_name].with_context(active_test=False).browse(
                list({item.res_id for item in items})).exists()
            field = records._fields.get('company_id')
            has_company = field is not None and field.type == 'many2one' \
                and field.comodel_name == 'res.company'
            companies = {record.id: record.company_id.id for record in records} if has_company else {}
            for item in items:
                if item.res_id in records._ids:
                    scope[item.id] = companies[item.res_id] if has_company else item.company_id.id
        return scope

    @api.model
    def _woow_already_imported(self, rule_id, attachments):
        """The ids of ``attachments`` that the attachment wizard of release 18.0.1 already filed.

        Those documents have no link to their attachment (the link is new): a document of the
        rule's folder that names the same record and has the same name and content is the same
        file. The filed copy of the rule itself is found through the link and not here.
        """
        elevated = self._woow_elevated()
        rule = elevated.browse(rule_id)
        attachments = elevated.env['ir.attachment'].browse(attachments.ids)
        documents = elevated.env['woow.document'].with_context(active_test=False).search([
            ('folder_id', '=', rule.folder_id.id), ('source_attachment_id', '=', False),
            ('source_res_model', '=', rule.res_model),
            ('source_res_id', 'in', attachments.mapped('res_id'))])
        known = {(doc.source_res_id, doc.name, doc.checksum) for doc in documents}
        return {attachment.id for attachment in attachments
                if (attachment.res_id, attachment.name, attachment.checksum) in known}

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
        except LockBusy:
            # no connection to measure with: the files stay unfiled and the backfill picks them up
            _logger.warning('Filing skipped (no connection to measure the quota); '
                            'the backfill of the rule files them later')
        except Exception as error:  # noqa: BLE001 - filing must never break an attachment
            _logger.info('Filing skipped (%s)', type(error).__name__)

    @api.model
    def _woow_creator_readable(self, work, attachments):
        """``work`` ((attachment id, rule id) pairs) without the files of records that the maker
        of the rule cannot read.

        The same test as the backfill, which asks the caller: a rule never files what the record
        rules deny its maker. The maker is the user who created the rule (``create_uid``); one who
        is gone (archived; the superuser account is always archived and always allowed) or who cannot read the kind files nothing. The records are searched in
        the maker's own environment, with the companies the maker works in (a record of another
        company than the one of the person who attached the file is still judged as the maker).
        """
        elevated = self._woow_elevated()
        kept = []
        for rule_id in dict.fromkeys(item[1] for item in work):
            rule = elevated.browse(rule_id)
            maker = rule.create_uid
            if not maker or not (maker.active or maker._is_superuser()):
                continue
            items = [item[0] for item in work if item[1] == rule_id]
            res_ids = {attachment.res_id for attachment in
                       elevated.env['ir.attachment'].browse(items)}
            try:
                records = self.env[rule.res_model].with_user(maker).with_context(  # sudo-ok: A2
                    active_test=False, allowed_company_ids=maker.company_ids.ids)
                readable = set(records.search([('id', 'in', list(res_ids))]).ids)
            except AccessError:
                continue
            attached = {a.id: a.res_id for a in elevated.env['ir.attachment'].browse(items)}
            kept.extend(item for item in work
                        if item[1] == rule_id and attached.get(item[0]) in readable)
        return [item for item in work if item in kept]

    @api.model
    def _woow_auto_file_checked(self, attachments, unfiled):
        by_kind = _auto_state(self.env)[0]
        candidates = []
        for attachment in attachments:
            if self._woow_fileable(attachment):
                for rule_id, company_id in by_kind.get(attachment.res_model, ()):
                    candidates.append((attachment, rule_id, company_id))
            if len(candidates) >= AUTO_FILE_BACKFILL_BATCH:
                candidates = candidates[:AUTO_FILE_BACKFILL_BATCH]
                break
        if not candidates:
            return
        # the record decides: it must exist, and a rule of a company files only the files of
        # records of that company
        scope = self._woow_record_scope(attachments.browse([item[0].id for item in candidates]))
        work = [(attachment.id, rule_id) for attachment, rule_id, company_id in candidates
                if attachment.id in scope
                and (not company_id or not scope[attachment.id] or scope[attachment.id] == company_id)]
        if work:
            # the record must be readable by the maker of the rule, as in the backfill
            work = self._woow_creator_readable(work, attachments)
        if work and unfiled:
            with self.env.cr.savepoint():
                done = {
                    (document.source_attachment_id.id, document.auto_rule_id.id)
                    for document in self._woow_elevated().env['woow.document'].with_context(
                        active_test=False).search([
                            ('source_attachment_id', 'in', [item[0] for item in work]),
                            ('auto_rule_id', 'in', [item[1] for item in work])])}
                work = [item for item in work if item not in done]
                for rule_id in {item[1] for item in work}:
                    imported = self._woow_already_imported(
                        rule_id, attachments.browse([item[0] for item in work if item[1] == rule_id]))
                    work = [item for item in work if item[1] != rule_id or item[0] not in imported]
        if not work:
            return
        # the content is the stored file of the attachment: no bytes are added, so the quota is
        # only looked at (no lock is taken for the rest of the caller's transaction)
        self.env['woow.document.quota']._check_uncharged()
        channel = self._woow_log_channel()
        elevated = self._woow_elevated()
        for attachment_id, rule_id in work:
            try:
                with self.env.cr.savepoint():
                    elevated.browse(rule_id)._woow_file_attachment(
                        elevated.env['ir.attachment'].browse(attachment_id), channel,
                        uncharged=True)
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
        """The attachments of the rule's kind that are not filed by this rule yet.

        The company is not part of it: the company of the record decides, and a domain on the
        attachment cannot say it (``_woow_record_scope`` does, for each batch).
        """
        self.ensure_one()
        return [
            ('res_model', '=', self.res_model), ('res_id', '>', 0), ('res_field', '=', False),
            ('type', '=', 'binary'), ('public', '=', False), ('file_size', '>', 0),
            ('woow_filed_document_ids', 'not any', [('auto_rule_id', '=', self.id)])]

    def _woow_backfill_eligible(self, batch):
        """The attachments of ``batch`` (elevated) that this run may file for its caller.

        The record must exist, be of the rule's company (or of none) and be readable by the person
        who runs the backfill: a rule never lets an administrator copy what the record rules deny
        them. A file that the attachment wizard of 18.0.1 already imported is left out.
        """
        self.ensure_one()
        scope = self._woow_record_scope(batch)
        kept = batch.filtered(lambda a: a.id in scope and (
            not self.company_id or not scope[a.id] or scope[a.id] == self.company_id.id))
        if not kept:
            return kept
        readable = self.env[self.res_model].with_context(active_test=False).search(
            [('id', 'in', kept.mapped('res_id'))]).ids
        kept = kept.filtered(lambda a: a.res_id in readable)
        imported = self._woow_already_imported(self.id, kept) if kept else set()
        return kept.filtered(lambda a: a.id not in imported)

    def _woow_backfill(self):
        """Look at the next batch of unfiled attachments of the rule's kind and file them.

        Returns ``{'looked': n, 'filed': n, 'refused': n, 'skipped': n, 'left': n}``. One run
        looks at no more than ``AUTO_FILE_BACKFILL_BATCH`` attachments after the cursor of the
        rule (and files no more than ``_BACKFILL_MAX_BYTES`` of content); a run that ends the list
        sets the cursor back to the start, so the next run begins again and files what an earlier
        run could not. A run can be repeated at any time: an attachment that a rule has filed (a
        trashed document included) is not filed again. ``skipped`` counts what the rule does not
        file for its caller (another company's record, a record that is gone or that the caller
        cannot read, a file that was imported by hand before).
        """
        self.ensure_one()
        # whoever runs a backfill must be able to read the kind of record
        self.env[self.res_model].check_access('read')
        channel = self._woow_log_channel()
        rule = self._woow_elevated()
        # Claiming the row of the rule serialises two runs of the same rule: the second one waits
        # for the first and is then retried on a fresh read of the cursor and of what is filed.
        super(WoowDocumentAutoRule, rule).write({'backfill_cursor': rule.backfill_cursor})
        attachments = rule.env['ir.attachment'].with_context(active_test=False)
        batch = attachments.search(
            rule._woow_backfill_domain() + [('id', '>', rule.backfill_cursor)],
            order='id', limit=AUTO_FILE_BACKFILL_BATCH)
        eligible = self._woow_backfill_eligible(batch)
        ledger = QuotaLedger()
        filed = refused = 0
        total = 0
        stopped = None
        for attachment in eligible:
            if total and total + attachment.file_size > _BACKFILL_MAX_BYTES:
                break       # the rest of the batch is for the next run
            total += attachment.file_size
            stopped = attachment
            try:
                with self.env.cr.savepoint():
                    rule._woow_file_attachment(attachment, channel, ledger=ledger)
                filed += 1
            except Exception as error:  # noqa: BLE001 - one file never stops the run
                refused += 1
                _logger.info('Backfill of rule %s skipped attachment %s (%s)',
                             rule.id, attachment.id, type(error).__name__)
        cut = len(eligible) and stopped != eligible[-1]
        finished = len(batch) < AUTO_FILE_BACKFILL_BATCH and not cut
        cursor = 0 if finished else (stopped.id if cut else batch[-1].id)
        looked = len(batch.filtered(lambda a: a.id <= cursor)) if cut else len(batch)
        super(WoowDocumentAutoRule, rule).write({'backfill_cursor': cursor})
        left = 0 if finished else attachments.search_count(
            rule._woow_backfill_domain() + [('id', '>', cursor)])
        return {'looked': looked, 'filed': filed, 'refused': refused,
                'skipped': looked - filed - refused, 'left': left}

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
