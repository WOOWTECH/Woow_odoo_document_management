# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""Run T-SHARE-32 in a disposable sandbox database over real HTTP."""

import os
import re
import secrets
import subprocess
import sys
from pathlib import Path


METHOD = 'TestShareHttp.test_concurrent_zip_downloads_respect_one_use_limit'
TAG = '/woow_document_management:' + METHOD
RESULT = re.compile(r'0 failed, 0 error\(s\) of 1 tests\b')


def main():
    sbx = os.environ.get('WOOW_SBX', '')
    slot = os.environ.get('SLOT', '')
    base_db = os.environ.get('WOOW_SBX_BASE_DB', '')
    if (not Path(sbx).is_file() or not Path(sbx).is_absolute()
            or not re.fullmatch(r'[a-z][a-z0-9_-]{0,20}', slot)
            or not re.fullmatch(r'wdoc_[a-z0-9_]{1,35}', base_db)):
        sys.stderr.write('SC|status=FAIL|reason=configuration\n')
        return 2
    db = 'wdoc_sc_%s_%s' % (slot.replace('-', '_'), secrets.token_hex(4))
    root = Path(__file__).resolve().parents[2]
    addon = root / 'woow_document_management'
    env = dict(os.environ, SLOT=slot)

    def call(*args, timeout=120):
        return subprocess.run(['bash', sbx, *args], env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)

    created = False
    try:
        synced = call('sync', str(addon), 'woow_document_management')
        if synced.returncode:
            raise RuntimeError('sync')
        created = True
        copied = call('copydb', base_db, db)
        if copied.returncode:
            raise RuntimeError('copydb')
        tested = call('test', db, 'woow_document_management', TAG, timeout=900)
        match = re.search(r'^LOG (/.+\.log)$', tested.stdout, re.MULTILINE)
        if not match or tested.returncode or 'RC 0' not in tested.stdout:
            raise RuntimeError('test_command')
        checked = call('log', match.group(1), 'Starting .*test_concurrent_zip|[0-9]+ failed, [0-9]+ error', timeout=60)
        if (checked.returncode
                or 'Starting ' + METHOD not in checked.stdout
                or not RESULT.search(checked.stdout)):
            raise RuntimeError('test_result')
        sys.stdout.write('SC|case=T-SHARE-32|status=PASS|tests=1\n')
        return 0
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        sys.stderr.write('SC|case=T-SHARE-32|status=FAIL|reason=%s\n' % reason)
        return 1
    finally:
        if created:
            dropped = call('dropdb', db)
            if dropped.returncode:
                sys.stderr.write('SC|case=T-SHARE-32|cleanup=FAIL\n')
                raise SystemExit(1)


if __name__ == '__main__':
    raise SystemExit(main())
