#!/bin/sh
# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
set -eu
cd "$(dirname "$0")"
case ${1:-} in
    share-max-downloads-race)
        exec python3 sc_share_max_downloads_race.py ;;
    denied-count-real-http)
        exec python3 sc_denied_count_real_http.py ;;
    source-limit-two-links)
        exec python3 sc_source_limit_two_links.py ;;
    denied-sources-together)
        exec python3 sc_denied_sources_together.py ;;
    denied-many-sources)
        exec python3 sc_denied_many_sources.py ;;
    denied-one-source-together)
        exec python3 sc_denied_one_source_together.py ;;
    unlock-over-limit-together)
        exec python3 sc_unlock_over_limit_together.py ;;
    quota-contention)
        exec python3 sc_quota_contention.py ;;
    *)
        printf '%s\n' 'usage: run_sc.sh share-max-downloads-race|denied-count-real-http|source-limit-two-links|denied-sources-together|denied-many-sources|denied-one-source-together|unlock-over-limit-together|quota-contention' >&2
        exit 2 ;;
esac
