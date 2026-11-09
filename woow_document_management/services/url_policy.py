# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""URL validation for url-type documents (IF-URL, owned by core-models-acl).

Pure Python. Day-0 stub: everything is rejected.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit


_BAD = re.compile(r'[\\\s\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]')


def validate(url: str, *, allow_mailto: bool = False) -> str:
    """Return the normalised URL or raise ValueError(code) (code maps to M-30)."""
    if not isinstance(url, str) or not url or len(url) > 2048 or _BAD.search(url):
        raise ValueError('invalid_url')
    decoded = unquote(url)
    if _BAD.search(decoded):
        raise ValueError('invalid_url')
    try:
        parsed = urlsplit(url)
        # Querying hostname and port also checks malformed brackets and ports.
        hostname, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValueError('invalid_url') from exc
    scheme = parsed.scheme.lower()
    if scheme == 'mailto' and allow_mailto:
        if parsed.netloc or not parsed.path or '@' not in parsed.path or parsed.fragment:
            raise ValueError('invalid_url')
        return urlunsplit((scheme, '', parsed.path, parsed.query, ''))
    if scheme not in {'https', 'http'} or not parsed.netloc or not hostname:
        raise ValueError('invalid_url')
    if parsed.username is not None or parsed.password is not None or '@' in parsed.netloc:
        raise ValueError('invalid_url')
    if '%' in parsed.netloc or '\\' in parsed.netloc or port == 0:
        raise ValueError('invalid_url')
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def is_safe(url: str, *, allow_mailto: bool = False) -> bool:
    """True when ``validate`` accepts the URL."""
    try:
        validate(url, allow_mailto=allow_mailto)
        return True
    except ValueError:
        return False
