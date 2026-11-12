# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""T-SHARE-48: refused requests of one source at the same time, over real HTTP.

Twelve clients that the trusted loopback proxy names as one source (the same
X-Real-IP) send refused link opens in rounds that start together. Counting a refusal
never waits for, fails on or runs again because of another request of the source: the
server log shows no retry and no error trace, every answer is 404 or 429, and exactly
``denied_rate_per_min`` refusals are counted (answered 404) however the requests
interleave; the others are answered 429 and recorded as ``rate_limited``. The counter
row, the audit buckets and the capacity add up to what was answered.
"""

import json

from sc_http_runtime import ScenarioError, run

CLIENTS = 12
ROUNDS = 3
LIMIT = 10          # the default denied_rate_per_min
SOURCE = '198.51.100.77'

FIXTURE = '''import json
import sys

params = env['ir.config_parameter'].sudo()
params.set_param('woow_document_management.trusted_proxies', '127.0.0.1/32')
params.set_param('woow_document_management.client_ip_header', 'X-Real-IP')
env.cr.commit()
sys.stdout.write('SC_PARAMS ' + json.dumps(True) + '\\n')
'''


def check(scenario):
    scenario.prepare()
    if scenario.shell(FIXTURE, 'SC_PARAMS') is not True:
        raise ScenarioError('fixture')
    scenario.start_server()
    spec = json.dumps({'clients': CLIENTS, 'rounds': ROUNDS, 'ip': SOURCE})
    for _try in range(2):
        scenario.reset_denied()
        scenario.align_minute(40)
        retries = scenario.server_retries()
        traces = scenario.server_log_count('Traceback')
        result = scenario.client('same-source', spec)
        if scenario.minutes_used() != 1:
            continue
        retries = scenario.server_retries() - retries
        traces = scenario.server_log_count('Traceback') - traces
        statuses = [status for per_client in result['statuses'] for status in per_client]
        if result['errors'] or len(statuses) != CLIENTS * ROUNDS:
            raise ScenarioError('http_status')
        if not set(statuses) <= {404, 429}:
            raise ScenarioError('status_%s' % ','.join(str(s) for s in sorted(set(statuses))))
        if traces:
            raise ScenarioError('error_trace_%d' % traces)
        if retries:
            raise ScenarioError('retried_%d' % retries)
        refused, limited = statuses.count(404), statuses.count(429)
        if refused != LIMIT:
            raise ScenarioError('counted_%d_expected_%d' % (refused, LIMIT))
        counters = scenario.sql(
            "SELECT event || ':' || count FROM woow_document_denied_counter ORDER BY event")
        if counters.splitlines() != ['share_access:%d' % LIMIT]:
            raise ScenarioError('sql_counter_%s' % counters)
        buckets = scenario.sql(
            "SELECT event || ':' || COUNT(*) || ':' || SUM(count) FROM woow_document_log "
            "WHERE result = 'denied' GROUP BY event ORDER BY event")
        expected = ['rate_limited:1:%d' % limited, 'share_access:1:%d' % LIMIT]
        if buckets.splitlines() != expected:
            raise ScenarioError('sql_buckets_%s' % buckets.replace('\n', ','))
        capacity = scenario.sql("SELECT COALESCE(SUM(count), 0) FROM woow_document_denied_budget")
        if capacity != '3':
            raise ScenarioError('sql_capacity_%s' % capacity)
        return '|clients=%d|requests=%d|http=%dx404,%dx429|retries=0|counter=%d' % (
            CLIENTS, len(statuses), refused, limited, LIMIT)
    raise ScenarioError('minute_boundary')


if __name__ == '__main__':
    raise SystemExit(run('T-SHARE-48', check))
