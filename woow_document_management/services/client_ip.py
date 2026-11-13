# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Real client IP from a configured and trusted proxy chain (IF-IP)."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from collections.abc import Mapping

HEADERS: tuple[str, ...] = ('', 'CF-Connecting-IP', 'X-Real-IP', 'X-Forwarded-For')


def parse_trusted_proxies(raw: str) -> tuple:
    """Comma or whitespace separated CIDRs; any invalid item -> () (only the peer is trusted)."""
    parts = re.split(r'[,\s]+', (raw or '').strip())
    if not parts or parts == ['']:
        return ()
    try:
        return tuple(ipaddress.ip_network(part, strict=False) for part in parts)
    except ValueError:
        return ()


def _address(value):
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None


def _trusted(address, networks):
    if address.version == 6 and address.ipv4_mapped is not None:
        # a dual-stack listener names an IPv4 peer like this, and it is that IPv4 address
        address = address.ipv4_mapped
    return any(address in network for network in networks)


def client_ip(environ: Mapping[str, str], trusted: tuple, header: str) -> str:
    """Use forwarding headers only when the immediate peer is trusted."""
    peer = _address(environ.get('REMOTE_ADDR', ''))
    if peer is None:
        return ''
    fallback = str(peer)
    if header not in HEADERS or not header or not _trusted(peer, trusted):
        return fallback
    key = 'HTTP_' + header.upper().replace('-', '_')
    value = environ.get(key, '')
    if header != 'X-Forwarded-For':
        address = _address(value)
        return str(address) if address is not None else fallback
    parts = [part.strip() for part in str(value).split(',')]
    addresses = [_address(part) for part in parts]
    if not parts or any(address is None for address in addresses):
        return fallback
    for address in reversed(addresses):
        if not _trusted(address, trusted):
            return str(address)
    return fallback


def request_client_ip(httprequest, settings) -> str:
    """Client IP of an HTTP request, using the environ before ProxyFix (CA-14)."""
    environ = httprequest.environ
    original = environ.get('werkzeug.proxy_fix.orig')
    if isinstance(original, Mapping) and original.get('REMOTE_ADDR'):
        # ProxyFix's orig mapping contains the socket peer but omits headers.
        # Keep the request headers while restoring only that trusted boundary.
        environ = {**environ, 'REMOTE_ADDR': original['REMOTE_ADDR']}
    return client_ip(environ, settings.trusted_proxies, settings.client_ip_header)


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
