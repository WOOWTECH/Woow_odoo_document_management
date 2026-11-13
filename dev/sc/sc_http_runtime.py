# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""Fail-closed helper for scenarios that drive a real Odoo HTTP server in the sandbox.

Every request runs in its own Repeatable Read transaction, unlike HttpCase where
all requests share the test transaction. The server binds container loopback in a
copied database that is dropped afterwards.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

ADDON = 'woow_document_management'
PYTHON = '/opt/woow/venv/bin/python'

# Standard library HTTP client, run inside the sandbox pod: python -c CLIENT <command> <port> ...
CLIENT = r'''
import http.client
import json
import re
import sys
import threading
import time
import urllib.parse

CSRF = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')
PORT = int(sys.argv[2])


class Client:
    def __init__(self, headers=None):
        self.sid = None
        self.headers = dict(headers or {})

    def send(self, method, path, form=None):
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=120)
        headers = dict(self.headers)
        if self.sid:
            headers['Cookie'] = 'session_id=' + self.sid
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            text = response.read().decode('utf-8', 'replace')
            for name, value in response.getheaders():
                if name.lower() == 'set-cookie' and value.startswith('session_id='):
                    self.sid = value.split(';', 1)[0].split('=', 1)[1]
            return response.status, text
        finally:
            connection.close()

    def csrf(self, path):
        status, text = self.send('GET', path)
        match = CSRF.search(text)
        if status != 200 or match is None:
            raise RuntimeError('page without form token')
        return match.group(1)

    def login(self, login, password):
        token = self.csrf('/web/login')
        status, _ = self.send('POST', '/web/login', {
            'login': login, 'password': password, 'csrf_token': token, 'redirect': '/my'})
        if status not in (302, 303):
            raise RuntimeError('login refused')

    def upload(self, path, token, filename, content):
        """POST one file as multipart form data; returns (status, seconds)."""
        boundary = '----woowsc%x' % int(time.time() * 1000000)
        body = (('--%s\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n%s\r\n'
                 '--%s\r\nContent-Disposition: form-data; name="files"; filename="%s"\r\n'
                 'Content-Type: text/plain\r\n\r\n') % (boundary, token, boundary, filename)
                ).encode() + content + ('\r\n--%s--\r\n' % boundary).encode()
        headers = dict(self.headers)
        headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
        if self.sid:
            headers['Cookie'] = 'session_id=' + self.sid
        started = time.monotonic()
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=120)
        try:
            connection.request('POST', path, body=body, headers=headers)
            response = connection.getresponse()
            response.read()
            return response.status, time.monotonic() - started
        finally:
            connection.close()


def ready():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            Client().csrf('/web/login')
            return {'ready': True}
        except (OSError, RuntimeError, http.client.HTTPException):
            time.sleep(0.5)
    return {'ready': False}


def quota_contention(spec):
    """One account uploads without pause while another one uploads at a steady rhythm."""
    spec = json.loads(spec)
    accounts = {}
    for name in ('attacker', 'victim'):
        client = Client()
        client.login(*spec[name]['credentials'])
        folder = spec[name]['folder']
        path = '/my/docs/folder/%d' % folder
        accounts[name] = (client, '/my/docs/folder/%d/upload' % folder, client.csrf(path))
    stop = threading.Event()
    result = {'attacker': [], 'victim': [], 'errors': []}

    def attack():
        client, path, token = accounts['attacker']
        count = 0
        try:
            while not stop.is_set():
                count += 1
                status, seconds = client.upload(
                    path, token, 'attack-%d.txt' % count, ('attack %d' % count).encode())
                result['attacker'].append(status)
        except Exception as exc:  # reported to the runner, which fails closed
            result['errors'].append('attacker_' + type(exc).__name__)

    def victim():
        client, path, token = accounts['victim']
        try:
            for count in range(1, spec['uploads'] + 1):
                started = time.monotonic()
                status, seconds = client.upload(
                    path, token, 'victim-%d.txt' % count, ('victim %d' % count).encode())
                result['victim'].append([status, round(seconds * 1000)])
                pause = spec['period'] - (time.monotonic() - started)
                if pause > 0:
                    time.sleep(pause)
        except Exception as exc:  # reported to the runner, which fails closed
            result['errors'].append('victim_' + type(exc).__name__)

    attacker = threading.Thread(target=attack)
    attacker.start()
    time.sleep(spec['head_start'])
    victim_thread = threading.Thread(target=victim)
    victim_thread.start()
    victim_thread.join(timeout=300)
    stop.set()
    attacker.join(timeout=300)
    if victim_thread.is_alive() or attacker.is_alive():
        result['errors'].append('timeout')
    return result


COMMANDS = {'ready': ready, 'quota-contention': quota_contention}
sys.stdout.write(json.dumps(COMMANDS[sys.argv[1]](*sys.argv[3:])) + '\n')
'''


class ScenarioError(RuntimeError):
    pass


class HttpScenario:
    def __init__(self, case):
        self.case = case
        self.script = os.environ.get('WOOW_SBX', '')
        self.slot = os.environ.get('SLOT', '')
        self.base_db = os.environ.get('WOOW_SBX_BASE_DB', '')
        if (not self.script or not Path(self.script).is_absolute()
                or not Path(self.script).is_file()
                or not re.fullmatch(r'[a-z][a-z0-9_-]{0,20}', self.slot)
                or self.slot == 'security'
                or not re.fullmatch(r'wdoc_[a-z0-9_]{1,35}', self.base_db)):
            raise ScenarioError('configuration')
        self.db = 'wdoc_sc_%s_%s' % (self.slot.replace('-', '_'), secrets.token_hex(4))
        self.port = 19000 + secrets.randbelow(4000)
        self.server_log = '/tmp/wdoc-sc-%s.log' % secrets.token_hex(6)
        self.env = dict(os.environ, SLOT=self.slot)
        self.copy_attempted = False
        self.pid = None

    def call(self, *args, input=None, timeout=120):
        try:
            result = subprocess.run(['bash', self.script, *args], input=input, env=self.env,
                                    capture_output=True, text=True, timeout=timeout,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScenarioError(type(exc).__name__) from exc
        if result.returncode:
            raise ScenarioError('%s_failed' % args[0])
        return result.stdout

    def prepare(self):
        """Copy the base DB and install the synced addon into the copy."""
        root = Path(__file__).resolve().parents[2]
        self.call('sync', str(root / ADDON), ADDON, timeout=180)
        self.copy_attempted = True
        for attempt in range(3):
            try:
                self.call('copydb', self.base_db, self.db, timeout=300)
                break
            except ScenarioError:
                if attempt == 2:
                    raise
                time.sleep(10)   # the template DB may be busy for a moment
        installed = self.call('odoo', self.db, '-i', ADDON, '-u', ADDON, timeout=900)
        if 'RC 0' not in installed.splitlines():
            raise ScenarioError('install')

    def shell(self, script, marker):
        """Run an odoo shell fixture; it prints one '<marker> <json>' line."""
        output = self.call('shell', self.db, input=script, timeout=300)
        lines = [line for line in output.splitlines() if line.startswith(marker + ' ')]
        if len(lines) != 1:
            raise ScenarioError('fixture')
        return json.loads(lines[0][len(marker) + 1:])

    def sql(self, query):
        return self.call('psql', self.db, '-XAtc', query).strip()

    def start_server(self):
        command = (
            'nohup /opt/woow/venv/bin/odoo -c /etc/odoo-sbx/odoo.conf '
            '--addons-path=/var/lib/odoo/dev-addons/%s,/usr/lib/python3/dist-packages/odoo/addons '
            '-d %s --db-filter=^%s$ --http-interface=127.0.0.1 --http-port=%d '
            '--workers=0 --max-cron-threads=0 > %s 2>&1 < /dev/null & echo $!'
        ) % (self.slot, self.db, self.db, self.port, self.server_log)
        pid = self.call('exec', 'bash', '-c', command).strip()
        if not re.fullmatch(r'[1-9][0-9]*', pid):
            raise ScenarioError('server_start')
        self.pid = pid
        if self.client('ready').get('ready') is not True:
            raise ScenarioError('server_ready')

    def client(self, command, *args, timeout=600):
        output = self.call('exec', PYTHON, '-c', CLIENT, command, str(self.port), *args,
                           timeout=timeout)
        try:
            return json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise ScenarioError('client_output') from exc

    def server_log_count(self, pattern):
        """Lines of the server log that match the extended regular expression ``pattern``."""
        output = self.call('exec', 'bash', '-c',
                           "grep -cE '%s' %s || true" % (pattern, self.server_log))
        return int(output.strip() or 0)

    def server_retries(self):
        """Requests the server rolled back and retried after a concurrency error."""
        return self.server_log_count('tries left|maximum number of tries')

    def cleanup(self):
        failed = False
        steps = [('exec', 'rm', '-f', self.server_log)]
        if self.pid:
            steps.insert(0, ('exec', 'kill', '-TERM', self.pid))
        for args in steps:
            try:
                self.call(*args)
            except ScenarioError:
                failed = True
        if self.copy_attempted:
            try:
                self.call('dropdb', self.db, timeout=180)
            except ScenarioError:
                failed = True
        return not failed


def run(case, check):
    scenario = None
    reason = None
    summary = ''
    try:
        scenario = HttpScenario(case)
        summary = check(scenario) or ''
    except ScenarioError as exc:
        reason = str(exc)
    finally:
        if scenario is not None and not scenario.cleanup():
            sys.stderr.write('SC|case=%s|cleanup=FAIL\n' % case)
            reason = reason or 'cleanup_failed'
    if reason:
        sys.stderr.write('SC|case=%s|status=FAIL|reason=%s\n' % (case, reason))
        return 2 if reason == 'configuration' else 1
    sys.stdout.write('SC|case=%s|status=PASS%s\n' % (case, summary))
    return 0
