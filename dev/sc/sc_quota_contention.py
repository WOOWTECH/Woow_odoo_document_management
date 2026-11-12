# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""T-UPL-32: one account uploading without pause must not hold other accounts up.

With a quota set, every upload takes the site-wide quota lock and measures the stored
bytes while it holds it. A portal account that sends single-file uploads back to back
keeps that lock busy most of the time; another account that uploads once a second must
still be served: every upload answers 200, within a second (p95), and no request is
rolled back and run again for a concurrency error (the server log is checked). The
database holds a long attachment table, so that the measurement takes a real while.
"""

import json
import secrets

from sc_http_runtime import ScenarioError, run

SEEDED_ATTACHMENTS = 150_000
VICTIM_UPLOADS = 12
PERIOD_SECONDS = 1.0
HEAD_START_SECONDS = 1.0
P95_LIMIT_MS = 1000

FIXTURE = '''import json
import sys

from odoo import Command

params = env['ir.config_parameter'].sudo()
params.set_param('woow_document_management.quota_mb', '100000')
admin = env.ref('base.user_admin')
accounts = {}
for name in ('attacker', 'victim'):
    partner = env['res.partner'].sudo().create({'name': 'Quota %s contact' % name})
    login = 'quota-%s-@LOGIN@' % name
    env['res.users'].sudo().create({
        'name': 'Quota %s' % name, 'login': login, 'password': @PASSWORD@,
        'partner_id': partner.id,
        'groups_id': [Command.set([env.ref('base.group_portal').id])]})
    folder = env['woow.document.folder'].sudo().create({
        'name': 'Quota %s folder' % name, 'owner_id': admin.id})
    env['woow.document.folder.access'].sudo().create({
        'folder_id': folder.id, 'subject_type': 'partner', 'partner_id': partner.id,
        'level': 'upload'})
    accounts[name] = {'credentials': [login, @PASSWORD@], 'folder': folder.id}
env.cr.execute("""INSERT INTO ir_attachment (name, type, store_fname, file_size)
                  SELECT 'seed-' || n, 'binary', 'zz/seed-' || n, 1024
                  FROM generate_series(1, @SEEDED@) AS n""")
env.cr.execute('ANALYZE ir_attachment')
env.cr.commit()
sys.stdout.write('SC_ACCOUNTS ' + json.dumps(accounts) + '\\n')
'''


def check(scenario):
    password = secrets.token_urlsafe(16)
    scenario.prepare()
    fixture = (FIXTURE.replace('@PASSWORD@', repr(password)).replace('@LOGIN@', secrets.token_hex(3))
               .replace('@SEEDED@', str(SEEDED_ATTACHMENTS)))
    accounts = scenario.shell(fixture, 'SC_ACCOUNTS')
    scenario.start_server()
    retries = scenario.server_retries()
    spec = json.dumps(dict(accounts, uploads=VICTIM_UPLOADS, period=PERIOD_SECONDS,
                           head_start=HEAD_START_SECONDS))
    result = scenario.client('quota-contention', spec)
    retries = scenario.server_retries() - retries
    if result['errors'] or len(result['victim']) != VICTIM_UPLOADS:
        raise ScenarioError('http_%s' % ','.join(result['errors'] or ['victim_uploads']))
    statuses = [status for status, _ms in result['victim']]
    refused = sorted({status for status in statuses + result['attacker'] if status != 200})
    if refused:
        raise ScenarioError('status_%s' % ','.join(str(status) for status in refused))
    if len(result['attacker']) < VICTIM_UPLOADS:
        raise ScenarioError('attacker_sent_%d' % len(result['attacker']))
    if retries:
        raise ScenarioError('retried_%d' % retries)
    times = sorted(milliseconds for _status, milliseconds in result['victim'])
    p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
    if p95 >= P95_LIMIT_MS:
        raise ScenarioError('p95_%dms' % p95)
    stored = int(scenario.sql(
        "SELECT COUNT(*) FROM woow_document WHERE name LIKE 'victim-%' OR name LIKE 'attack-%'"))
    if stored != len(statuses) + len(result['attacker']):
        raise ScenarioError('stored_%d' % stored)
    return '|victim=%dx200|p95=%dms|attacker=%dx200|retries=0' % (
        len(statuses), p95, len(result['attacker']))


if __name__ == '__main__':
    raise SystemExit(run('T-UPL-32', check))
