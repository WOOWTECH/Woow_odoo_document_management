# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Real client IP (IF-IP, owned by settings-ip).

Pure Python, does not import odoo.models. Day-0 stub: only the peer address is trusted;
``truncate_ip`` and ``hmac_ip`` are final pure functions (interface_contracts 21.8 item 4).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
from collections.abc import Mapping

HEADERS: tuple[str, ...] = ('', 'CF-Connecting-IP', 'X-Real-IP', 'X-Forwarded-For')


def parse_trusted_proxies(raw: str) -> tuple:
    """Comma or whitespace separated CIDRs; any invalid item -> () (only the peer is trusted)."""
    return ()  # WOOW-STUB


def client_ip(environ: Mapping[str, str], trusted: tuple, header: str) -> str:
    """Client IP (arch section 15). Day-0 stub: the peer REMOTE_ADDR only."""
    return environ.get('REMOTE_ADDR') or ''  # WOOW-STUB


def request_client_ip(httprequest, settings) -> str:
    """Client IP of an HTTP request, using the environ before ProxyFix (CA-14)."""
    environ = httprequest.environ.get('werkzeug.proxy_fix.orig') or httprequest.environ
    return environ.get('REMOTE_ADDR') or ''  # WOOW-STUB


def truncate_ip(ip: str) -> str:
    """IPv4 /24 or IPv6 /64 network string; invalid -> ''."""
    try:
        addr = ipaddress.ip_address((ip or '').strip())
    except ValueError:
        return ''
    prefix = 24 if addr.version == 4 else 64
    return str(ipaddress.ip_network('%s/%d' % (addr, prefix), strict=False))


def hmac_ip(k_ip: bytes, ip: str) -> str:
    """HMAC-SHA256 of the full IP, 64 hex characters."""
    return hmac.new(k_ip, (ip or '').encode(), hashlib.sha256).hexdigest()
