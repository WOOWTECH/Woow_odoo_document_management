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
import weakref
from collections.abc import Iterator

from psycopg2 import errors

from .constants import LOCK_TIMEOUT_MS


class LockBusy(Exception):
    """The lock could not be taken in time; the transaction is still usable."""


RANK: dict[str, int] = {'zip': 1, 'share': 2, 'quota': 3, 'access': 4}
NAME_RE = re.compile(r'^(zip:u:\d+|zip:slot:[1-4]|zip:s:\d+:[0-9a-f]{16}|share:\d+|quota(?::warn)?|access:\d+)$')
_TRACKER = weakref.WeakKeyDictionary()
_HELD_QUERY = """SELECT (classid::bigint << 32) | objid::bigint
                 FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory'
                 AND granted AND objsubid = 1"""


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
    key = advisory_key(name)
    if cr in _TRACKER:
        cr.execute(_HELD_QUERY)  # sql-ok: Q4
        _prune_tracker(cr, cr.fetchall())
    rank = _check_rank(cr, name)
    cr.execute('SELECT pg_try_advisory_xact_lock(%s)', (key,))  # sql-ok: Q4
    taken = bool(cr.fetchone()[0])
    if taken:
        _remember(cr, key, rank)
    return taken


def lock(cr, name: str, timeout_ms: int = LOCK_TIMEOUT_MS) -> None:
    """Blocking transaction lock with ``lock_timeout`` set inside a savepoint (Q4).

    On timeout the savepoint is rolled back and LockBusy is raised; the transaction stays usable.
    """
    key = advisory_key(name)
    if cr in _TRACKER:
        cr.execute(_HELD_QUERY)  # sql-ok: Q4
        _prune_tracker(cr, cr.fetchall())
    rank = _check_rank(cr, name)
    try:
        with cr.savepoint(flush=False):
            cr.execute("SELECT current_setting('lock_timeout')")  # sql-ok: Q4
            previous = cr.fetchone()[0]
            cr.execute("SELECT set_config('lock_timeout', %s, true)", (_timeout(timeout_ms),))  # sql-ok: Q4
            cr.execute('SELECT pg_advisory_xact_lock(%s)', (key,))  # sql-ok: Q4
            cr.execute("SELECT set_config('lock_timeout', %s, true)", (previous,))  # sql-ok: Q4
    except (errors.LockNotAvailable, errors.DeadlockDetected) as exc:
        raise LockBusy(name) from exc
    _remember(cr, key, rank)


@contextlib.contextmanager
def bounded_wait(cr, timeout_ms: int) -> Iterator[None]:
    """Temporarily set ``lock_timeout`` inside a savepoint (IC-R09).

    A row-lock timeout inside the block rolls the savepoint back and raises LockBusy.
    No advisory lock is taken and no rank is recorded.
    """
    try:
        with cr.savepoint(flush=False):
            cr.execute("SELECT current_setting('lock_timeout')")  # sql-ok: Q4
            previous = cr.fetchone()[0]
            cr.execute("SELECT set_config('lock_timeout', %s, true)", (_timeout(timeout_ms),))  # sql-ok: Q4
            # The inner savepoint flushes and clears ORM caches on rollback. Setting the
            # timeout first also covers that flush if it waits for a row lock.
            with cr.savepoint():
                yield
            cr.execute("SELECT set_config('lock_timeout', %s, true)", (previous,))  # sql-ok: Q4
    except errors.LockNotAvailable as exc:
        raise LockBusy('bounded_wait') from exc


def _reset_tracker(cr) -> None:
    """Test helper: forget the ranks recorded for ``cr`` (called by tests/common.py setUp)."""
    _TRACKER.pop(cr, None)


def _timeout(value: int) -> str:
    if isinstance(value, bool) or not 1 <= int(value) <= 60_000:
        raise ValueError('invalid lock timeout')
    return '%dms' % int(value)


def _check_rank(cr, name: str) -> int:
    rank = RANK[name.split(':', 1)[0]]
    if rank < max(_TRACKER.get(cr, {}).values(), default=0):
        raise AssertionError('advisory lock order violation')
    return rank


def _prune_tracker(cr, held_rows) -> None:
    held = {row[0] for row in held_rows}
    _TRACKER[cr] = {key: rank for key, rank in _TRACKER[cr].items()
                    if key in held}


def _remember(cr, key: int, rank: int) -> None:
    if cr not in _TRACKER:
        ref = weakref.ref(cr)
        def clear():
            cursor = ref()
            if cursor is not None:
                _TRACKER.pop(cursor, None)
        cr.postcommit.add(clear)
        cr.postrollback.add(clear)
        _TRACKER[cr] = {}
    _TRACKER[cr][key] = rank
