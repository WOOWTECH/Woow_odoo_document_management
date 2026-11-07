# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Portal routes under /my/docs (IF-PORTAL, owned by portal).

Day-0 stub: the class only, no route. The final routes are listed in tools/routes.lock.json.
"""
from odoo.addons.portal.controllers.portal import CustomerPortal


class WoowDocumentPortal(CustomerPortal):
    pass
