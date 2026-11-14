# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The workspace screen: favourites, sections and the file-kind icon of a document."""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import folding
from ..services.constants import RECENT_DAYS

# (prefix or exact mimetype, icon). The first match wins; the order goes from the specific to the
# general. Icons are Font Awesome classes that Odoo Community already ships.
_ICON_BY_MIMETYPE = (
    ('application/pdf', 'fa-file-pdf-o'),
    ('application/zip', 'fa-file-archive-o'),
    ('application/x-zip', 'fa-file-archive-o'),
    ('application/gzip', 'fa-file-archive-o'),
    ('application/x-tar', 'fa-file-archive-o'),
    ('application/msword', 'fa-file-word-o'),
    ('application/vnd.openxmlformats-officedocument.wordprocessingml', 'fa-file-word-o'),
    ('application/vnd.oasis.opendocument.text', 'fa-file-word-o'),
    ('application/vnd.ms-excel', 'fa-file-excel-o'),
    ('application/vnd.openxmlformats-officedocument.spreadsheetml', 'fa-file-excel-o'),
    ('application/vnd.oasis.opendocument.spreadsheet', 'fa-file-excel-o'),
    ('text/csv', 'fa-file-excel-o'),
    ('application/vnd.ms-powerpoint', 'fa-file-powerpoint-o'),
    ('application/vnd.openxmlformats-officedocument.presentationml', 'fa-file-powerpoint-o'),
    ('application/vnd.oasis.opendocument.presentation', 'fa-file-powerpoint-o'),
    ('image/', 'fa-file-image-o'),
    ('video/', 'fa-file-video-o'),
    ('audio/', 'fa-file-audio-o'),
    ('text/', 'fa-file-text-o'),
)
_SECTIONS = ('mine', 'shared', 'recent', 'starred')


class WoowDocumentWorkspace(models.Model):
    _inherit = 'woow.document'

    favorite_ids = fields.One2many('woow.document.favorite', 'document_id', copy=False,
                                   groups='base.group_user')
    is_favorite = fields.Boolean(
        string='Starred', compute='_compute_is_favorite', search='_search_is_favorite',
        groups='base.group_user',
        help='Whether you marked this document with a star. Stars are private to each user.')
    woow_section = fields.Selection(
        [('mine', 'My files'), ('shared', 'Shared with me'), ('recent', 'Recent'),
         ('starred', 'Starred')], string='Section', compute='_compute_woow_section',
        search='_search_woow_section', groups='base.group_user',
        help='A quick filter of the workspace; it is only used to search.')
    woow_icon = fields.Char(string='Icon', compute='_compute_woow_icon')

    @api.depends('favorite_ids')
    @api.depends_context('uid')
    def _compute_is_favorite(self):
        starred = set()
        ids = [doc.id for doc in self if isinstance(doc.id, int)]
        if ids:
            # The user is named here as well as by the record rule: a call made with sudo()
            # keeps the uid of its caller and must still answer for that user only.
            starred = set(self.env['woow.document.favorite'].search_fetch(
                [('document_id', 'in', ids), ('user_id', '=', self.env.uid)],
                ['document_id']).document_id.ids)
        for doc in self:
            doc.is_favorite = doc.id in starred

    @api.model
    def _search_is_favorite(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(_('This filter cannot be combined that way.'))
        starred = ('favorite_ids', 'any', [('user_id', '=', self.env.uid)])
        return [starred] if (operator == '=') == value else ['!', starred]

    def _compute_woow_section(self):
        for doc in self:
            doc.woow_section = False

    @api.model
    def _search_woow_section(self, operator, value):
        """The domain of one section (or of several, joined by "or") for the caller."""
        if operator == '=':
            values = [value]
        elif operator == 'in' and isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
        else:
            raise UserError(_('This filter cannot be combined that way.'))
        if any(not isinstance(item, str) or item not in _SECTIONS for item in values):
            raise UserError(_('This filter cannot be combined that way.'))
        uid = self.env.uid
        domains = {
            'mine': [('owner_id', '=', uid)],
            'shared': [('shared_user_ids', 'in', [uid])],
            'recent': [('write_date', '>=', fields.Datetime.now() - timedelta(days=RECENT_DAYS))],
            'starred': [('favorite_ids', 'any', [('user_id', '=', uid)])],
        }
        chosen = [domains[item] for item in dict.fromkeys(values)]
        if not chosen:
            return [('id', '=', False)]
        result = chosen[0]
        for domain in chosen[1:]:
            result = ['|'] + result + domain
        return result

    @api.depends('mimetype', 'document_type')
    def _compute_woow_icon(self):
        for doc in self:
            icon = 'fa-file-o'
            if doc.document_type == 'url':
                icon = 'fa-link'
            else:
                mimetype = (doc.mimetype or '').lower()
                for prefix, candidate in _ICON_BY_MIMETYPE:
                    if mimetype.startswith(prefix):
                        icon = candidate
                        break
            doc.woow_icon = icon

    def action_toggle_favorite(self):
        """Star the document, or take the star away; it only ever touches the caller's own row."""
        self.check_access('read')
        self.ensure_one()
        favorites = self.env['woow.document.favorite']
        mine = favorites.search([('document_id', '=', self.id),
                                 ('user_id', '=', self.env.uid)])
        if mine:
            mine.unlink()
        else:
            favorites.create({'document_id': self.id})
        return True

    @api.model
    def search_panel_select_range(self, field_name, **kwargs):
        """The folder tree of the search panel, without losing a folder under one the user cannot read.

        Core keeps a folder in the tree only when every folder above it is in the tree too, and a
        folder that the user may open but whose parent they may not (a folder with members of its
        own, say) would be left out. Such a folder is attached to the nearest folder above it that
        the user can see, or becomes a root. Only the folders that the call itself found are used,
        under the caller's record rules, and no name or id of a folder that the caller cannot read
        is added: the name of a folder is already folded by its display name.
        """
        result = super().search_panel_select_range(field_name, **kwargs)
        parent_field = isinstance(result, dict) and result.get('parent_field')
        if field_name != 'folder_id' or not parent_field or 'values' not in result:
            return result
        flat = super().search_panel_select_range(field_name, **dict(kwargs, hierarchize=False))
        if not isinstance(flat, dict) or 'values' not in flat:
            return result
        shown = {value['id']: value for value in result['values']}
        missing = [value for value in flat['values'] if value['id'] not in shown]
        if not missing:
            return result
        folders = self.env['woow.document.folder'].browse([value['id'] for value in missing])
        paths = {folder.id: folding.parse_parent_path(folder.parent_path) for folder in folders}
        counters = any('__count' in value for value in missing)
        # The shallow ones first, so that a folder finds the ones above it already placed.
        for value in sorted(missing, key=lambda item: len(paths.get(item['id'], ()))):
            above = paths.get(value['id'], ())[:-1]
            parent = next((folder_id for folder_id in reversed(above) if folder_id in shown), False)
            shown[value['id']] = entry = dict(value, **{parent_field: parent})
            if counters:
                while parent:
                    shown[parent]['__count'] = shown[parent].get('__count', 0) + entry.get(
                        '__count', 0)
                    parent = shown[parent][parent_field]
        result['values'] = list(shown.values())
        return result
