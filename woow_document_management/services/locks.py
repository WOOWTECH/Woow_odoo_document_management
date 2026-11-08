# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Transaction-level advisory locks (IF-LOCK, owned by upload-quota; ER-24, ER-47.3).

Every advisory lock of the module goes through this file. Day-0 stub: lock acquisition
fails closed (``try_lock`` -> False, ``lock`` -> LockBusy); ``advisory_key`` is final.
"""
from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Iterator

from .constants import LOCK_TIMEOUT_MS


class LockBusy(Exception):
    """The lock could not be taken in time; the transaction is still usable."""


RANK: dict[str, int] = {'zip': 1, 'share': 2, 'quota': 3}   # acquisition order zip -> share -> quota
NAME_RE = re.compile(r'^(zip:u:\d+|zip:slot:[1-4]|zip:s:\d+:[0-9a-f]{16}|share:\d+|quota)$')


def advisory_key(name: str) -> int:
    """Stable 64-bit signed key for a lock name (independent of the process; T-UPL-31).

    Names not matching NAME_RE -> ValueError.
    """
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError('invalid lock name')
    digest = hashlib.sha256(b'woow_docmgmt/lock/v1:' + name.encode()).digest()
    return int.from_bytes(digest[:8], 'big', signed=True)


def try_lock(cr, name: str) -> bool:
    """Non-blocking transaction lock (Q4). True when taken; records the rank."""
    advisory_key(name)
    return False  # WOOW-STUB


def lock(cr, name: str, timeout_ms: int = LOCK_TIMEOUT_MS) -> None:
    """Blocking transaction lock with ``lock_timeout`` set inside a savepoint (Q4).

    On timeout the savepoint is rolled back and LockBusy is raised; the transaction stays usable.
    """
    advisory_key(name)
    raise LockBusy(name)  # WOOW-STUB


@contextlib.contextmanager
def bounded_wait(cr, timeout_ms: int) -> Iterator[None]:
    """Temporarily set ``lock_timeout`` inside a savepoint (IC-R09).

    A row-lock timeout inside the block rolls the savepoint back and raises LockBusy.
    No advisory lock is taken and no rank is recorded.
    """
    raise LockBusy('bounded_wait')  # WOOW-STUB
    yield  # pragma: no cover


def _reset_tracker(cr) -> None:
    """Test helper: forget the ranks recorded for ``cr`` (called by tests/common.py setUp)."""
    return None  # WOOW-STUB
