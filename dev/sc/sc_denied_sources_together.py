# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""T-SHARE-46: refused requests from many sources at the same time, and an unlock.

Twelve HTTP clients, each named as its own source through X-Real-IP behind the
trusted loopback proxy, send refused link opens in rounds that start together.
In the first round every source is new, so each request reserves capacity slots
for its counter row and its audit bucket while the others do the same; a correct
password is sent to a protected link in that round as well. Slots are reserved in
short transactions of their own, so no request may be rolled back and retried for
a concurrency error: the server log must show no retry, every refused open is
answered 404, and the unlock 303 without using capacity or leaving a counter row.
"""

import json
import secrets

from sc_http_runtime import ScenarioError, run

SOURCES = 12
ROUNDS = 3

FIXTURE = '''import base64
import json
import sys
from datetime import timedelta

from odoo import fields

params = env['ir.config_parameter'].sudo()
params.set_param('woow_document_management.trusted_proxies', '127.0.0.1/32')
params.set_param('woow_document_management.client_ip_header', 'X-Real-IP')
admin = env.ref('base.user_admin')
folder = env['woow.document.folder'].sudo().create({
    'name': 'Many sources fixture', 'owner_id': admin.id})
env['woow.document'].sudo().create({
    'name': 'fixture.txt', 'folder_id': folder.id, 'datas': base64.b64encode(b'fixture')})
share, code = env['woow.document.share'].with_user(admin)._issue({
    'share_type': 'folder', 'folder_id': folder.id,
    'expire_at': fields.Datetime.now() + timedelta(days=1), 'password': @SECRET@})
env.cr.commit()
sys.stdout.write('SC_LINK ' + json.dumps([share.id, code]) + '\\n')
'''


def check(scenario):
    secret = secrets.token_urlsafe(16)
    scenario.prepare()
    link = scenario.shell(FIXTURE.replace('@SECRET@', repr(secret)), 'SC_LINK')
    if len(link) != 2:
        raise ScenarioError('fixture')
    scenario.start_server()
    spec = json.dumps({'sources': SOURCES, 'rounds': ROUNDS, 'share': link[0],
                       'code': link[1], 'password': secret})
    for _try in range(2):
        scenario.reset_denied()
        scenario.align_minute(40)
        retries = scenario.server_retries()
        result = scenario.client('sources', spec)
        if scenario.minutes_used() != 1:
            continue
        retries = scenario.server_retries() - retries
        statuses = [status for per_source in result['statuses'] for status in per_source]
        if result['errors'] or len(statuses) != SOURCES * ROUNDS:
            raise ScenarioError('http_status')
        if retries:
            raise ScenarioError('retried_%d' % retries)
        if set(statuses) != {404}:
            raise ScenarioError('refused_%s' % ','.join(sorted({str(s) for s in statuses})))
        if result['unlock'] != 303:
            raise ScenarioError('unlock_%s' % result['unlock'])
        counters = scenario.sql(
            "SELECT event || ':' || COUNT(*) || ':' || MIN(count) || ':' || MAX(count) "
            "FROM woow_document_denied_counter GROUP BY event")
        if counters != 'share_access:%d:%d:%d' % (SOURCES, ROUNDS, ROUNDS):
            raise ScenarioError('sql_counters')
        buckets = scenario.sql(
            "SELECT event || ':' || COUNT(*) || ':' || SUM(count) FROM woow_document_log "
            "WHERE result = 'denied' GROUP BY event")
        if buckets != 'share_access:%d:%d' % (SOURCES, SOURCES * ROUNDS):
            raise ScenarioError('sql_buckets')
        capacity = scenario.sql("SELECT COALESCE(SUM(count), 0) FROM woow_document_denied_budget")
        if capacity != str(2 * SOURCES):
            raise ScenarioError('sql_capacity_%s' % capacity)
        return '|sources=%d|requests=%d|http=%dx404|unlock=303|retries=0|capacity=%s' % (
            SOURCES, len(statuses), len(statuses), capacity)
    raise ScenarioError('minute_boundary')


if __name__ == '__main__':
    raise SystemExit(run('T-SHARE-46', check))
