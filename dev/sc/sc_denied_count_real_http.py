# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""T-SHARE-44: real curl requests and SQL counter in a throwaway Odoo DB."""

import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path


def main():
    sbx = os.environ.get('WOOW_SBX', '')
    slot = os.environ.get('SLOT', '')
    base_db = os.environ.get('WOOW_SBX_BASE_DB', '')
    if (not Path(sbx).is_file() or not Path(sbx).is_absolute()
            or not re.fullmatch(r'[a-z][a-z0-9_-]{0,20}', slot)
            or not re.fullmatch(r'wdoc_[a-z0-9_]{1,35}', base_db)):
        sys.stderr.write('SC|case=T-SHARE-44|status=FAIL|reason=configuration\n')
        return 2

    db = 'wdoc_sc_%s_%s' % (slot.replace('-', '_'), secrets.token_hex(4))
    port = 19000 + secrets.randbelow(4000)
    marker = secrets.token_hex(6)
    jar = '/tmp/wdoc-sc-%s.cookie' % marker
    log = '/tmp/wdoc-sc-%s.log' % marker
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, SLOT=slot)

    def call(*args, timeout=120):
        return subprocess.run(['bash', sbx, *args], env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)

    created = False
    pid = None
    try:
        if call('sync', str(root / 'woow_document_management'),
                'woow_document_management').returncode:
            raise RuntimeError('sync')
        created = True
        if call('copydb', base_db, db).returncode:
            raise RuntimeError('copydb')
        # The copied DB is test-only. Remove any inherited denied counter.
        reset = call('psql', db, '-XAtc',
                     "DELETE FROM woow_document_log WHERE event='share_access' "
                     "AND result='denied' AND share_id IS NULL")
        if reset.returncode:
            raise RuntimeError('reset')
        server = (
            'nohup /opt/woow/venv/bin/odoo -c /etc/odoo-sbx/odoo.conf '
            '--addons-path=/var/lib/odoo/dev-addons/%s,'
            '/usr/lib/python3/dist-packages/odoo/addons '
            '-d %s --db-filter=^%s$ --http-interface=127.0.0.1 '
            '--http-port=%d --workers=0 --max-cron-threads=0 '
            '> %s 2>&1 < /dev/null & echo $!'
        ) % (slot, db, db, port, log)
        started = call('exec', 'bash', '-c', server)
        if started.returncode or not re.fullmatch(r'[1-9][0-9]*', started.stdout.strip()):
            raise RuntimeError('server_start')
        pid = started.stdout.strip()
        url = 'http://127.0.0.1:%d/woow-docs/s' % port
        landing = None
        for _ in range(40):
            landing = call('exec', 'curl', '-sS', '--max-time', '2', '-c', jar,
                           '-b', jar, url, timeout=10)
            if landing.returncode == 0 and 'csrf_token' in landing.stdout:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('server_ready')
        csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', landing.stdout)
        if csrf is None:
            raise RuntimeError('csrf')

        # Q1 buckets by minute. Keep all requests in one minute for exact row count.
        seconds = time.time() % 60
        if seconds > 50:
            time.sleep(61 - seconds)
        statuses = []
        for _ in range(11):
            response = call('exec', 'curl', '-sS', '--max-time', '10', '-o',
                            '/dev/null', '-w', '%{http_code}', '-c', jar,
                            '-b', jar, '--data-urlencode', 'csrf_token=' + csrf.group(1),
                            '--data-urlencode', 'token=' + 'x' * 43,
                            url + '/open', timeout=20)
            if response.returncode:
                raise RuntimeError('curl')
            statuses.append(response.stdout.strip())
        if statuses != ['404'] * 10 + ['429']:
            raise RuntimeError('http_status')
        counted = call('psql', db, '-XAtc',
                       "SELECT COALESCE(SUM(count),0),COALESCE(MAX(count),0),COUNT(*) "
                       "FROM woow_document_log WHERE event='share_access' "
                       "AND result='denied' AND share_id IS NULL")
        if counted.returncode or counted.stdout.strip() != '11|11|1':
            raise RuntimeError('sql_count')
        sys.stdout.write('SC|case=T-SHARE-44|status=PASS|http=10x404,1x429|count=11\n')
        return 0
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        sys.stderr.write('SC|case=T-SHARE-44|status=FAIL|reason=%s\n' % reason)
        return 1
    finally:
        cleanup_failed = False
        if pid and call('exec', 'kill', '-TERM', pid).returncode:
            cleanup_failed = True
        if call('exec', 'rm', '-f', jar, log).returncode:
            cleanup_failed = True
        if created and call('dropdb', db).returncode:
            cleanup_failed = True
        if cleanup_failed:
            sys.stderr.write('SC|case=T-SHARE-44|cleanup=FAIL\n')
            raise SystemExit(1)


if __name__ == '__main__':
    raise SystemExit(main())
