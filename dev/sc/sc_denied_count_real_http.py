# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""T-SHARE-44: 11 refused link opens over real HTTP and the persisted SQL counters.

With denied_rate_per_min = 10, opens 1-10 answer 404 and add to the source's
bucket; the 11th answers 429 and only adds a rate_limited event. All requests
run inside one minute of the database clock; a try that crossed a minute is
repeated once on clean counters.
"""

from sc_http_runtime import ScenarioError, run

LIMIT = 10


def check(scenario):
    scenario.prepare()
    scenario.start_server()
    for _try in range(2):
        scenario.reset_denied()
        scenario.align_minute(20)
        statuses = scenario.client('denied', str(LIMIT + 1))['statuses']
        if scenario.minutes_used() != 1:
            continue
        if statuses != [404] * LIMIT + [429]:
            raise ScenarioError('http_status')
        buckets = scenario.sql(
            "SELECT event || ':' || COALESCE(share_id, 0) || ':' || count "
            "FROM woow_document_log WHERE result = 'denied' ORDER BY event")
        if buckets.splitlines() != ['rate_limited:0:1', 'share_access:0:%d' % LIMIT]:
            raise ScenarioError('sql_buckets')
        counter = scenario.sql(
            "SELECT event || ':' || count FROM woow_document_denied_counter ORDER BY event")
        if counter.splitlines() != ['share_access:%d' % LIMIT]:
            raise ScenarioError('sql_counter')
        return '|http=%dx404,1x429|share_access=%d|rate_limited=1|counter=%d' % (
            LIMIT, LIMIT, LIMIT)
    raise ScenarioError('minute_boundary')


if __name__ == '__main__':
    raise SystemExit(run('T-SHARE-44', check))
