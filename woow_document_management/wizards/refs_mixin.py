# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""One answer for a missing and a hidden record, for the wizards that keep ids of the module.

A wizard row stores the ids of the documents, folders and tags that it will work on. The
database answers a missing id with a constraint error and an id that exists but that the caller
cannot see with a new row, which tells the caller which ids exist. A wizard that inherits this
mixin names its references in ``_woow_visible_refs`` and every create and write looks each of
them up with the caller's own search (the record rules apply); one that is not found gets the
refusal of the field, the same for both cases.
"""
from odoo import api, models
from odoo.exceptions import AccessError

from ..models.document_folder import _m2o_value


def _ids_of_commands(value):
    """The ids that a value of a many2many field names, or None when its shape is not understood.

    The ids of ``link`` (4), ``set`` (6) and ``update`` (1) commands, plain ids and records are
    returned; ``create`` (0) makes a new row, and ``unlink``, ``delete`` and ``clear`` only remove
    links, so none of them can reveal an id.
    """
    if isinstance(value, models.BaseModel):
        return list(value.ids)
    if value is None or value is False:
        return []
    if not isinstance(value, (list, tuple)):
        return None
    ids = []
    for command in value:
        if isinstance(command, bool):
            return None
        if isinstance(command, int):
            ids.append(command)
        elif isinstance(command, (list, tuple)) and command \
                and isinstance(command[0], int) and not isinstance(command[0], bool):
            code = int(command[0])      # (the commands of ``Command`` are an int enum)
            if code == 4 or code == 1:
                if len(command) < 2 or type(command[1]) is not int:
                    return None
                ids.append(command[1])
            elif code == 6:
                if len(command) < 3 or not isinstance(command[2], (list, tuple)) \
                        or any(type(item) is not int for item in command[2]):
                    return None
                ids.extend(command[2])
        elif isinstance(command, dict):
            continue
        else:
            return None
    return ids


class WoowWizardRefsMixin(models.AbstractModel):
    _name = 'woow.document.wizard.refs.mixin'
    _description = 'Wizard rows that name records of the module'

    def _woow_visible_refs(self):
        """``{field name: (comodel, refusal)}`` of the fields that hold ids of records to look up."""
        return {}

    @api.model
    def _woow_check_visible_refs(self, vals):
        """AccessError (the refusal of the field) unless the caller finds every record named in ``vals``."""
        if self.env.su:
            return
        for name, (comodel, message) in self._woow_visible_refs().items():
            if name not in vals:
                continue
            field = self._fields[name]
            if field.type == 'many2many':
                ids = _ids_of_commands(vals[name])
            else:
                ref = _m2o_value(vals[name], comodel)
                ids = None if ref is None else ([ref] if ref else [])
            if ids is None:
                raise AccessError(message)
            wanted = set(ids)
            if wanted and set(self.env[comodel].with_context(active_test=False).search(
                    [('id', 'in', list(wanted))]).ids) != wanted:
                raise AccessError(message)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            for vals in vals_list:
                # what the row will hold, the defaults of the context and of the model included
                self._woow_check_visible_refs(self._add_missing_default_values(dict(vals)))
        return super().create(vals_list)

    def write(self, vals):
        self._woow_check_visible_refs(vals)
        return super().write(vals)
