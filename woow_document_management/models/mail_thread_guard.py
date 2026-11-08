# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Mail thread guard for documents and folders (IF-GUARD, owned by core-models-acl; ER-20).

``_get_thread_with_access`` never calls super, so the portal hash/pid/token fallback is never
reached for module models. Non-internal users cannot post, subscribe, edit messages or write
folders.
"""
from odoo import _, api, models
from odoo.exceptions import AccessError


def _deny_non_internal(records):
    if not records.env.user._is_internal():
        raise AccessError(_('Only internal users can do this.'))


class WoowDocumentThreadGuard(models.Model):
    _inherit = 'woow.document'

    @api.model
    def _get_thread_with_access(self, thread_id, mode="read", **kwargs):
        if not self.env.user._is_internal():
            return self.browse()
        thread = self.browse(thread_id)
        if thread.exists() and thread.sudo(False).has_access(mode):
            return thread
        return self.browse()

    def message_post(self, **kwargs):
        _deny_non_internal(self)
        return super().message_post(**kwargs)

    def message_subscribe(self, *args, **kwargs):
        _deny_non_internal(self)
        return super().message_subscribe(*args, **kwargs)

    def _message_update_content(self, *args, **kwargs):
        _deny_non_internal(self)
        return super()._message_update_content(*args, **kwargs)


class WoowDocumentFolderThreadGuard(models.Model):
    _inherit = 'woow.document.folder'

    @api.model
    def _get_thread_with_access(self, thread_id, mode="read", **kwargs):
        if not self.env.user._is_internal():
            return self.browse()
        thread = self.browse(thread_id)
        if thread.exists() and thread.sudo(False).has_access(mode):
            return thread
        return self.browse()

    def message_post(self, **kwargs):
        _deny_non_internal(self)
        return super().message_post(**kwargs)

    def message_subscribe(self, *args, **kwargs):
        _deny_non_internal(self)
        return super().message_subscribe(*args, **kwargs)

    def _message_update_content(self, *args, **kwargs):
        _deny_non_internal(self)
        return super()._message_update_content(*args, **kwargs)

    def write(self, vals):
        """Portal users only probe the folder write rule (HD-07); real writes are refused."""
        _deny_non_internal(self)
        return super().write(vals)
