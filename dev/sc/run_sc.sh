#!/bin/sh
# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
set -eu
cd "$(dirname "$0")"
case ${1:-} in
    quota-contention)
        exec python3 sc_quota_contention.py ;;
    *)
        printf '%s\n' 'usage: run_sc.sh quota-contention' >&2
        exit 2 ;;
esac
