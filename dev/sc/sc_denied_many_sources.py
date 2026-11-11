# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""T-SHARE-47: refused requests from many sources against a threaded server's pool.

Sixty HTTP clients, each named as its own source through X-Real-IP behind the trusted
loopback proxy, send refused link requests in rounds that start together to a threaded
server (workers = 0, no ODOO_MAX_HTTP_THREADS). Every request holds one database
connection, and each new source reserves its capacity slots through a second one, so
an unlimited burst would ask for more connections than the pool (db_maxconn, 64) holds
and leave requests without their own. The reservations are limited per process and
one that finds no turn or no connection is answered like a full minute, with 429 and
nothing recorded, never with a server error: every answer is 404 or 429, the server
log shows no error trace and no retry, and the counters show exactly the refusals that
were counted.
"""

import json

from sc_http_runtime import ScenarioError, run

SOURCES = 60
ROUNDS = 2
UNKNOWN_ID = 987654321
MAX_DENIED_ROWS_PER_MIN = 1024

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
    spec = json.dumps({'sources': SOURCES, 'rounds': ROUNDS, 'unknown': UNKNOWN_ID})
    for _try in range(2):
        scenario.reset_denied()
        scenario.align_minute(40)
        retries = scenario.server_retries()
        traces = scenario.server_log_count('Traceback')
        result = scenario.client('refuse-many', spec)
        if scenario.minutes_used() != 1:
            continue
        retries = scenario.server_retries() - retries
        traces = scenario.server_log_count('Traceback') - traces
        statuses = [status for per_source in result['statuses'] for status in per_source]
        if result['errors'] or len(statuses) != SOURCES * ROUNDS:
            raise ScenarioError('http_status')
        server_errors = sorted({status for status in statuses if status >= 500})
        if server_errors:
            raise ScenarioError('server_error_%s' % ','.join(str(s) for s in server_errors))
        if not set(statuses) <= {404, 429}:
            raise ScenarioError('status_%s' % ','.join(str(s) for s in sorted(set(statuses))))
        if traces:
            raise ScenarioError('error_trace_%d' % traces)
        if retries:
            raise ScenarioError('retried_%d' % retries)
        refused = statuses.count(404)
        limited = statuses.count(429)
        counted = [per_source.count(404) for per_source in result['statuses']]
        if not refused:
            raise ScenarioError('nothing_counted')
        # A 404 is a refusal that counted for its source; a 429 of a source that was
        # new and found no slot counted nothing and left no row.
        counters = scenario.sql(
            "SELECT COUNT(*) || ':' || COALESCE(SUM(count), 0) || ':' || COALESCE(MAX(count), 0) "
            "FROM woow_document_denied_counter WHERE event = 'share_access'")
        expected = '%d:%d:%d' % (sum(1 for number in counted if number), refused, max(counted))
        if counters != expected:
            raise ScenarioError('sql_counters_%s_expected_%s' % (counters, expected))
        buckets = scenario.sql(
            "SELECT COUNT(*) || ':' || COALESCE(SUM(count), 0) FROM woow_document_log "
            "WHERE result = 'denied' AND event = 'share_access'")
        bucket_rows, bucket_total = (int(part) for part in buckets.split(':'))
        counter_rows = int(counters.split(':')[0])
        # A bucket without a connection for its slot is not written, but its refusal
        # still counts for the source: the buckets never hold more than was counted.
        if not 0 < bucket_rows <= counter_rows or bucket_total > refused:
            raise ScenarioError('sql_buckets_%s' % buckets)
        capacity = int(scenario.sql(
            "SELECT COALESCE(SUM(count), 0) FROM woow_document_denied_budget"))
        if not counter_rows + bucket_rows <= capacity <= MAX_DENIED_ROWS_PER_MIN:
            raise ScenarioError('sql_capacity_%d' % capacity)
        return '|sources=%d|requests=%d|http=%dx404,%dx429|retries=0|capacity=%d' % (
            SOURCES, len(statuses), refused, limited, capacity)
    raise ScenarioError('minute_boundary')


if __name__ == '__main__':
    raise SystemExit(run('T-SHARE-47', check))
