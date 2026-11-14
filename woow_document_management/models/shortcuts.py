# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Shortcuts: a document of the kind ``shortcut`` that points to a file or a link of another folder.

A shortcut has no content, no size and no access of its own over its target. What a reader of the
shortcut learns about the target is decided here, in the reader's own environment: the target is
named, opened or even identified only when the reader's record rules find it (and it is live).
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .document import WoowDocument
from .ir_attachment import NO_INDEX_CTX_KEY

_MANAGER = 'woow_document_management.group_woow_document_manager'

# Only a context carrying this module-level object may create a shortcut for a caller who is not the
# superuser. A context that comes over RPC is JSON, so it cannot hold the object.
SHORTCUT_CTX_KEY = 'woow_shortcut_create'
_SHORTCUT_SENTINEL = object()

# What a shortcut may point to, and what a caller may pass when a shortcut is created.
TARGET_TYPES = ('binary', 'url')
_CREATE_KEYS = frozenset({'name', 'folder_id', 'document_type', 'shortcut_target_id'})
_FIELD = 'shortcut_target_id'


# The field name at the start of one comma-separated part of an order string, read the way the core
# order regex reads it: any white space, an optional double quote, then the name.
_ORDER_FIELD = re.compile(r'\s*"?([a-z0-9_]+)', re.IGNORECASE)


def _names_target(path):
    """True when a field path (``shortcut_target_id``, ``shortcut_target_id.name``, ``"shortcut_target_id"``,
    ``shortcut_target_id:func``) starts at the target."""
    if not isinstance(path, str):
        return False
    match = _ORDER_FIELD.match(path)
    return bool(match) and match.group(1) == _FIELD


def _order_names_target(order):
    """True when any part of an order string starts at the target, whatever follows the field name."""
    return any(_names_target(part) for part in (order or '').split(','))


class WoowDocumentShortcut(models.Model):
    _inherit = 'woow.document'

    _sql_constraints = [
        ('shortcut_target_consistent',
         "CHECK ((document_type = 'shortcut' AND shortcut_target_id IS NOT NULL) "
         "OR (document_type != 'shortcut' AND shortcut_target_id IS NULL))",
         'A shortcut points to one document, and only a shortcut has a target.'),
    ]

    document_type = fields.Selection(selection_add=[('shortcut', 'Shortcut')],
                                     ondelete={'shortcut': 'cascade'})
    shortcut_target_id = fields.Many2one('woow.document', string='Shortcut target', index=True,
                                         ondelete='cascade', copy=False)
    shortcut_target_available = fields.Boolean(
        string='Target available', compute='_compute_shortcut_target_available')

    # ------------------------------------------------------------------ visibility of the target

    def _woow_visible_target_ids(self):
        """Ids of the targets of the shortcuts in ``self`` that the caller may read and that are live.

        One search in the caller's own environment, so the record rules decide. A target in the
        trash (or in a trashed folder), a target the caller cannot read and a target that does not
        exist are the same answer: not in the result.
        """
        ids = {doc.shortcut_target_id.id for doc in self
               if doc.document_type == 'shortcut' and doc.shortcut_target_id}
        if not ids:
            return set()
        return set(self.env['woow.document'].search([
            ('id', 'in', list(ids)), ('active', '=', True), ('folder_in_trash', '=', False),
            ('document_type', 'in', list(TARGET_TYPES))]).ids)

    @api.depends('document_type', 'shortcut_target_id', 'shortcut_target_id.active',
                 'shortcut_target_id.folder_in_trash')
    @api.depends_context('uid')
    def _compute_shortcut_target_available(self):
        real = self.filtered(lambda doc: isinstance(doc.id, int))
        visible = real._woow_visible_target_ids()
        for doc in self:
            doc.shortcut_target_available = bool(
                doc.document_type == 'shortcut' and doc.shortcut_target_id
                and doc.shortcut_target_id.id in visible)

    def _read_format(self, fnames, load='_classic_read'):
        """The target of a shortcut is read as empty unless the caller may read it.

        ``read``, ``web_read``, ``search_read`` and the list and form views all format their
        values here. The superuser (server code) reads the stored value.
        """
        result = super()._read_format(fnames, load=load)
        if _FIELD in fnames and not self.env.su:
            rows = [vals for vals in result if vals.get(_FIELD)]
            if rows:
                visible = self.browse([vals['id'] for vals in rows])._woow_visible_target_ids()
                for vals in rows:
                    value = vals[_FIELD]
                    target_id = value[0] if isinstance(value, (tuple, list)) else value
                    if target_id not in visible:
                        vals[_FIELD] = False
        return result

    @api.readonly
    def web_read(self, specification):
        """``web_read`` that formats the target of a shortcut from the masked value.

        Core reads the related record of a many2one itself and labels it as the superuser, which
        would name a target that the caller cannot read. The target is therefore requested without
        a sub-specification, and the label is added here for a target that was found readable.
        """
        asked = specification.get(_FIELD)
        if asked is None or self.env.su:
            return super().web_read(specification)
        specification = dict(specification, **{_FIELD: {}})
        result = super().web_read(specification)
        if 'fields' in asked:
            wanted = {name for name in asked['fields'] if name in ('id', 'display_name')}
            targets = self.env['woow.document'].browse(
                [vals[_FIELD] for vals in result if vals.get(_FIELD)])
            names = {target.id: target.display_name for target in targets
                     if 'display_name' in wanted}
            for vals in result:
                target_id = vals.get(_FIELD)
                if target_id:
                    vals[_FIELD] = dict(
                        {'id': target_id},
                        **({'display_name': names.get(target_id)} if 'display_name' in wanted else {}))
        return result

    def _woow_shortcut_target_restricted(self):
        """True for a caller who is neither the superuser nor a document administrator."""
        return not self.env.su and not self.env.user.has_group(_MANAGER)

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        """A search cannot be built on the target of a shortcut (it would tell which one it is)."""
        if self._woow_shortcut_target_restricted():
            terms = [term[0] for term in (domain or [])
                     if isinstance(term, (list, tuple)) and term]
            if any(_names_target(path) for path in terms) or _order_names_target(order):
                raise AccessError(_('This field cannot be used in a search.'))
        return super()._search(domain, offset=offset, limit=limit, order=order)

    def _order_field_to_sql(self, alias, field_name, direction, nulls, query):
        """Where the core resolves one term of an order: search, search_read and the order of a
        grouped read all pass here, whichever way the order string was spelled."""
        if _names_target(field_name) and self._woow_shortcut_target_restricted():
            raise AccessError(_('This field cannot be used in a search.'))
        return super()._order_field_to_sql(alias, field_name, direction, nulls, query)

    @api.model
    def _read_group_groupby(self, groupby_spec, query):
        if self._woow_shortcut_target_restricted() and _names_target(groupby_spec.split(':', 1)[0]):
            raise AccessError(_('This field cannot be used to group documents.'))
        return super()._read_group_groupby(groupby_spec, query)

    @api.model
    def _read_group_select(self, aggregate_spec, query):
        if self._woow_shortcut_target_restricted() and _names_target(aggregate_spec.split(':', 1)[0]):
            raise AccessError(_('This field cannot be used to group documents.'))
        return super()._read_group_select(aggregate_spec, query)

    def _export_rows(self, fields, *, _is_toplevel_call=True):
        if self._woow_shortcut_target_restricted() and any(
                path and _names_target(path[0]) for path in fields):
            raise AccessError(_('This field cannot be exported.'))
        return super()._export_rows(fields, _is_toplevel_call=_is_toplevel_call)

    # ------------------------------------------------------------------ constraints

    @api.constrains('document_type', 'shortcut_target_id')
    def _check_shortcut_target(self):
        for doc in self:
            if doc.document_type != 'shortcut':
                if doc.shortcut_target_id:
                    raise ValidationError(_('Only a shortcut has a target.'))
            elif not doc.shortcut_target_id or doc.shortcut_target_id.document_type not in TARGET_TYPES:
                raise ValidationError(_('A shortcut points to a file or a link.'))

    # ------------------------------------------------------------------ create, write, copy

    @api.model_create_multi
    def create(self, vals_list):
        """Shortcuts are created here, by the wizard; every other document goes on to the base create."""
        shortcut = [vals.get('document_type') == 'shortcut' for vals in vals_list]
        if not any(shortcut):
            if not self.env.su and any(_FIELD in vals for vals in vals_list):
                raise AccessError(_('File metadata is set by the server.'))
            return super().create(vals_list)
        others = super().create([vals for vals, kind in zip(vals_list, shortcut) if not kind]) \
            if not all(shortcut) else self.browse()
        shortcuts = self._woow_create_shortcuts([vals for vals, kind in zip(vals_list, shortcut) if kind])
        created, other_pos, short_pos = self.browse(), iter(others), iter(shortcuts)
        for kind in shortcut:
            created |= next(short_pos) if kind else next(other_pos)
        return created

    def _woow_create_shortcuts(self, vals_list):
        """Create shortcuts after the checks the wizard promises, whoever the caller is.

        The caller needs the upload level on the destination folder and read access to a live
        target; the shortcut takes the name of the target and belongs to the caller. It holds no
        content, so there is no upload policy and the quota is not touched.
        """
        if not self.env.su:
            if self.env.context.get(SHORTCUT_CTX_KEY) is not _SHORTCUT_SENTINEL:
                raise AccessError(_('A shortcut is created with the shortcut wizard.'))
            if not self.env.user._is_internal():
                raise AccessError(_('Only internal users can create shortcuts.'))
            if len(vals_list) > self.env['woow.document.settings']._get_settings().max_files_per_upload:
                raise UserError(_('Invalid number of files.'))
        created = self.browse()
        with self.env.cr.savepoint():
            for incoming in vals_list:
                vals = dict(incoming)
                if not self.env.su and set(vals) - _CREATE_KEYS:
                    raise AccessError(_('File metadata is set by the server.'))
                if vals.get('datas') or vals.get('url'):
                    raise UserError(_('A shortcut has no content of its own.'))
                folder = self.env['woow.document.folder'].browse(vals.get('folder_id')).exists()
                if not folder or folder.in_trash:
                    raise AccessError(_('The target folder is unavailable.'))
                targets = self.env['woow.document'].browse(vals.get(_FIELD)).exists()
                if not self.env.su:
                    folder.check_access('read')
                    folder._woow_require_rank(2)
                    # the caller's own search: a document that the rules hide is answered as missing
                    targets = self.env['woow.document'].search([('id', 'in', targets.ids)])
                if len(targets) != 1 or not targets.active or targets.folder_in_trash \
                        or targets.document_type not in TARGET_TYPES:
                    raise AccessError(_('The document is unavailable.'))
                if targets.company_id != folder.company_id:
                    raise UserError(_('A shortcut must be in the same company as its target.'))
                vals.update(name=targets.name, folder_id=folder.id, shortcut_target_id=targets.id,
                            document_type='shortcut')
                if not self.env.su:
                    vals['owner_id'] = self.env.uid
                record = super(WoowDocument, self.with_context(
                    mail_create_nolog=True, mail_create_nosubscribe=True, tracking_disable=True,
                    **{NO_INDEX_CTX_KEY: True})).create([vals])
                created |= record
                self.env['woow.document.log']._log_event(
                    'upload', channel=self._woow_log_channel(), document_id=record.id,
                    folder_id=folder.id, company_id=folder.company_id.id,
                    detail={'bytes': 0, 'shortcut': 1})
            self.env.flush_all()
        return created

    def unlink(self):
        """A purge of a target takes its shortcuts with it, through the same rules as any purge.

        The database would delete them by itself (``ondelete='cascade'``) and skip both the legal
        hold and the audit row. A shortcut on legal hold stops the purge of its target; the others
        are logged as purged and removed first, in the same savepoint as the target, so a refusal
        of the purge puts them back. Callers who are not administrators go straight to the base
        guard, which refuses them.
        """
        if not self or not (self.env.su or self.env.user.has_group(_MANAGER)):
            return super().unlink()
        shortcuts = self.with_context(active_test=False).search(
            [(_FIELD, 'in', self.ids)]) - self
        if not shortcuts:
            return super().unlink()
        if any(shortcut.legal_hold for shortcut in shortcuts):
            raise UserError(_('A document on legal hold cannot be deleted.'))
        with self.env.cr.savepoint():
            for shortcut in shortcuts:
                self.env['woow.document.log']._log_event(
                    'purge', channel=self._woow_log_channel(), document_id=shortcut.id,
                    folder_id=shortcut.folder_id.id, company_id=shortcut.company_id.id)
            # not the unlink of the base: that one wants a trashed document, and a shortcut whose
            # target is purged is not trashed (the administrator rights were judged above)
            super(WoowDocument, shortcuts).unlink()
            return super().unlink()

    def write(self, vals):
        """The target of a shortcut is set when it is created and never changes."""
        if _FIELD in vals and not self.env.su:
            raise AccessError(_('File metadata is set by the server.'))
        return super().write(vals)

    def copy(self, default=None):
        """A shortcut is not duplicated: it would be a second pointer made without the checks."""
        if any(doc.document_type == 'shortcut' for doc in self):
            raise UserError(_('A shortcut cannot be duplicated.'))
        return super().copy(default)

    # ------------------------------------------------------------------ buttons

    def action_open_shortcut_wizard(self):
        """Open the wizard that makes a shortcut to this file or link in another folder."""
        self.check_access('read')
        if len(self) != 1:
            raise UserError(_('Select one document.'))
        if not self.env.user._is_internal():
            raise AccessError(_('Only internal users can create shortcuts.'))
        if not self.active or self.folder_in_trash or self.document_type not in TARGET_TYPES:
            raise UserError(_('A shortcut can only point to a file or a link.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Create a shortcut'),
            'res_model': 'woow.document.shortcut.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_document_id': self.id},
        }

    def action_open_shortcut_target(self):
        """Open the target of this shortcut, if the caller may read it; otherwise a neutral message."""
        self.check_access('read')
        if len(self) != 1:
            raise UserError(_('Select one document.'))
        if self.document_type != 'shortcut':
            raise UserError(_('This document is not a shortcut.'))
        if not self.shortcut_target_available:
            raise AccessError(_('The target of this shortcut is not available.'))
        return {
            'type': 'ir.actions.act_window', 'res_model': 'woow.document',
            'res_id': self.shortcut_target_id.id, 'view_mode': 'form', 'target': 'current',
        }
