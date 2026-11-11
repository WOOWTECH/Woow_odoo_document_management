# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""T-SHARE-45: one source guesses passwords on two links at the same time.

Two HTTP clients from the same address open one protected link each and send
wrong passwords in rounds that start together, so the requests overlap in
separate Repeatable Read transactions that take different share locks. With
denied_rate_per_min = 3 across links, exactly 3 guesses may be compared (404);
every other guess is answered 429. The persisted source counter and the failure
buckets must equal the number of compared guesses.
"""

import json
import secrets

from sc_http_runtime import ScenarioError, run

LIMIT = 3
ROUNDS = 4

FIXTURE = '''import base64
import json
import sys
from datetime import timedelta

from odoo import fields

params = env['ir.config_parameter'].sudo()
params.set_param('woow_document_management.denied_rate_per_min', '3')
params.set_param('woow_document_management.share_unlock_max_fail', '10')
admin = env.ref('base.user_admin')
links = []
for index in range(2):
    folder = env['woow.document.folder'].sudo().create({
        'name': 'Two links fixture %d' % index, 'owner_id': admin.id})
    env['woow.document'].sudo().create({
        'name': 'fixture.txt', 'folder_id': folder.id,
        'datas': base64.b64encode(b'fixture')})
    share, code = env['woow.document.share'].with_user(admin)._issue({
        'share_type': 'folder', 'folder_id': folder.id,
        'expire_at': fields.Datetime.now() + timedelta(days=1),
        'password': @SECRET@})
    links.append([share.id, code])
env.cr.commit()
sys.stdout.write('SC_LINKS ' + json.dumps(links) + '\\n')
'''


def check(scenario):
    secret = secrets.token_urlsafe(16)
    scenario.prepare()
    links = scenario.shell(FIXTURE.replace('@SECRET@', repr(secret)), 'SC_LINKS')
    if len(links) != 2:
        raise ScenarioError('fixture')
    scenario.start_server()
    spec = json.dumps({'links': links, 'password': secret, 'rounds': ROUNDS})
    for _try in range(2):
        scenario.reset_denied()
        scenario.align_minute(40)
        result = scenario.client('unlock-race', spec)
        if scenario.minutes_used() != 1:
            continue
        statuses = [status for per_client in result['statuses'] for status in per_client]
        if result['errors'] or len(statuses) != 2 * ROUNDS or set(statuses) - {404, 429}:
            raise ScenarioError('http_status')
        compared = statuses.count(404)
        if compared > LIMIT:
            raise ScenarioError('limit_exceeded_%d' % compared)
        if compared != LIMIT:
            raise ScenarioError('limit_not_reached_%d' % compared)
        failures = scenario.sql(
            "SELECT COALESCE(SUM(count), 0) FROM woow_document_log "
            "WHERE event = 'share_unlock_failed' AND result = 'denied'")
        if failures != str(compared):
            raise ScenarioError('sql_failures')
        counter = scenario.sql(
            "SELECT string_agg(count::text, ',') FROM woow_document_denied_counter "
            "WHERE event = 'share_unlock_failed'")
        if counter != str(compared):
            raise ScenarioError('sql_counter')
        limited = scenario.sql(
            "SELECT COALESCE(SUM(count), 0) FROM woow_document_log "
            "WHERE event = 'rate_limited' AND result = 'denied'")
        if limited != str(statuses.count(429)):
            raise ScenarioError('sql_rate_limited')
        return '|requests=%d|compared=%d|limit=%d|counter=%s|rate_limited=%s' % (
            len(statuses), compared, LIMIT, counter, limited)
    raise ScenarioError('minute_boundary')


if __name__ == '__main__':
    raise SystemExit(run('T-SHARE-45', check))
