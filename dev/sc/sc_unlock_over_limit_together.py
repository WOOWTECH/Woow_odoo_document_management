# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""T-SHARE-49: password attempts of a source that used its limit, all at once, over real HTTP.

One source (every client reaches the server from the same loopback address) first uses its
minute limit on one protected link, with wrong passwords sent one after the other
(denied_rate_per_min = 3). Then twelve clients, each with a session of its own on that link,
send passwords in rounds that start together. Every attempt is answered 429 without a
comparison, and none fails on or is run again because of another: the server log shows no
retry and no error trace. The refusals are added to the one bucket of rate-limited requests
and match the answers; the source's counter and the counted failures stay at the limit, and
the minute capacity holds one slot, the bucket.
"""

import json
import secrets

from sc_http_runtime import ScenarioError, run

CLIENTS = 12
ROUNDS = 3
LIMIT = 3

FIXTURE = '''import base64
import json
import sys
from datetime import timedelta

from odoo import fields

params = env['ir.config_parameter'].sudo()
params.set_param('woow_document_management.denied_rate_per_min', '3')
params.set_param('woow_document_management.share_unlock_max_fail', '10')
admin = env.ref('base.user_admin')
folder = env['woow.document.folder'].sudo().create({
    'name': 'Over the limit fixture', 'owner_id': admin.id})
env['woow.document'].sudo().create({
    'name': 'fixture.txt', 'folder_id': folder.id, 'datas': base64.b64encode(b'fixture')})
share, code = env['woow.document.share'].with_user(admin)._issue({
    'share_type': 'folder', 'folder_id': folder.id,
    'expire_at': fields.Datetime.now() + timedelta(days=1), 'password': @SECRET@})
env.cr.commit()
sys.stdout.write('SC_LINKS ' + json.dumps([[share.id, code]]) + '\\n')
'''


def check(scenario):
    secret = secrets.token_urlsafe(16)
    scenario.prepare()
    links = scenario.shell(FIXTURE.replace('@SECRET@', repr(secret)), 'SC_LINKS')
    if len(links) != 1:
        raise ScenarioError('fixture')
    scenario.start_server()
    fill = json.dumps({'links': links, 'password': secret, 'rounds': LIMIT})
    burst = json.dumps({'links': links * CLIENTS, 'password': secret, 'rounds': ROUNDS})
    for _try in range(2):
        scenario.reset_denied()
        scenario.align_minute(45)
        # The source uses its limit: wrong passwords one after the other are compared (404),
        # and the correct one sent afterwards is refused (429).
        used = scenario.client('unlock-race', fill)
        if used['errors'] or used['statuses'] != [[404] * LIMIT]:
            raise ScenarioError('fill_status')
        if used['final'] != 429:
            raise ScenarioError('fill_correct_password_%s' % used['final'])
        retries = scenario.server_retries()
        traces = scenario.server_log_count('Traceback')
        result = scenario.client('unlock-race', burst)
        if scenario.minutes_used() != 1:
            continue
        retries = scenario.server_retries() - retries
        traces = scenario.server_log_count('Traceback') - traces
        statuses = [status for per_client in result['statuses'] for status in per_client]
        if result['errors'] or len(statuses) != CLIENTS * ROUNDS:
            raise ScenarioError('http_status')
        if set(statuses) != {429} or result['final'] != 429:
            raise ScenarioError('status_%s' % ','.join(
                str(s) for s in sorted(set(statuses + [result['final']]))))
        if traces:
            raise ScenarioError('error_trace_%d' % traces)
        if retries:
            raise ScenarioError('retried_%d' % retries)
        failures = scenario.sql(
            "SELECT COALESCE(SUM(count), 0) FROM woow_document_log "
            "WHERE event = 'share_unlock_failed' AND result = 'denied'")
        if failures != str(LIMIT):
            raise ScenarioError('sql_failures_%s' % failures)
        counter = scenario.sql(
            "SELECT string_agg(count::text, ',') FROM woow_document_denied_counter "
            "WHERE event = 'share_unlock_failed'")
        if counter != str(LIMIT):
            raise ScenarioError('sql_counter_%s' % counter)
        # Every refusal of the source, the first correct password and the last one included.
        answered = len(statuses) + 2
        limited = scenario.sql(
            "SELECT COUNT(*) || ':' || COALESCE(SUM(count), 0) FROM woow_document_log "
            "WHERE event = 'rate_limited' AND result = 'denied'")
        if limited != '1:%d' % answered:
            raise ScenarioError('sql_rate_limited_%s' % limited)
        capacity = scenario.sql("SELECT COALESCE(SUM(count), 0) FROM woow_document_denied_budget")
        if capacity != '1':
            raise ScenarioError('sql_capacity_%s' % capacity)
        return '|clients=%d|requests=%d|http=%dx429|retries=0|counter=%s|rate_limited=%d' % (
            CLIENTS, len(statuses), len(statuses), counter, answered)
    raise ScenarioError('minute_boundary')


if __name__ == '__main__':
    raise SystemExit(run('T-SHARE-49', check))
