# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Public share link routes under /woow-docs/s (IF-ROUTE-SHARE, owned by share).

The only file allowed to declare auth='public' routes. Day-0 stub: no route yet; the entry
helper fails closed. The final routes are listed in tools/routes.lock.json.
"""
from typing import NamedTuple

from odoo import http

from . import streaming


class ShareAccess(NamedTuple):
    share_sudo: object      # reads the link's own fields only
    scope_env: object       # creator env (rules apply)
    ip: str
    ip_hash: str
    grant: object


class WoowDocShareController(http.Controller):

    def _share_entry(self, *, token=None, share_id=None, require_grant=True,
                     require_unlocked=True, resolve=True):
        """First statement of every public route except the landing page (R11).

        Returns a ShareAccess or a Response; never raises. Day-0 stub: uniform 404.
        """
        return streaming.uniform_404('share')  # WOOW-STUB
