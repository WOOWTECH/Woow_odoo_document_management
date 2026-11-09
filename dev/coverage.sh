#!/usr/bin/env bash
# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
# Measure addon line coverage inside the disposable Odoo sandbox.
# Set COVERAGE_WHEEL_DIR to a local directory containing coverage==7.6.1
# wheels when the sandbox has no outbound network access.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SBX=${WOOW_SBX:-}
[[ $SBX = /* && -f $SBX ]] || {
    echo 'WOOW_SBX must name an absolute sandbox launcher path' >&2
    exit 2
}
SLOT=${SLOT:-release}
DB=${DB:-wdoc_release_cov}
BASE_DB=${BASE_DB:-wdoc_security}
[[ $SLOT =~ ^[a-z][a-z0-9_-]*$ && $DB =~ ^[a-z][a-z0-9_]*$ &&
   $BASE_DB =~ ^[a-z][a-z0-9_]*$ && $DB != "$BASE_DB" ]] || {
    echo 'invalid SLOT or DB' >&2
    exit 2
}
PORT=${PORT:-$((18100 + $(printf '%s' "$SLOT" | cksum | cut -d' ' -f1) % 800))}
D=/var/lib/odoo/dev-addons/$SLOT
[[ -n ${BROWSER_ROOT:-} && $BROWSER_ROOT = /var/lib/odoo/dev-addons/*/.browser ]] || {
    echo 'BROWSER_ROOT must name the staged sandbox .browser directory' >&2
    exit 2
}
BROWSER_PYDEPS=${BROWSER_ROOT%/.browser}/.pydeps
copied=0
cleanup() {
    SLOT=$SLOT bash "$SBX" exec rm -rf "$D/.cov" || true
    if (( copied )); then
        SLOT=$SLOT bash "$SBX" dropdb "$DB" || true
    fi
}
trap cleanup EXIT

SLOT=$SLOT bash "$SBX" sync "$ROOT/woow_document_management"
SLOT=$SLOT bash "$SBX" copydb "$BASE_DB" "$DB"
copied=1
if [[ -n ${COVERAGE_WHEEL_DIR:-} ]]; then
    SLOT=$SLOT bash "$SBX" sync "$COVERAGE_WHEEL_DIR" .cov-wheels
    SLOT=$SLOT bash "$SBX" exec bash -c \
        "mkdir -p '$D/.cov' && /opt/woow/venv/bin/python -m pip install -q \
         --no-index --find-links '$D/.cov-wheels' --target '$D/.cov' coverage==7.6.1"
else
    SLOT=$SLOT bash "$SBX" exec bash -c \
        "mkdir -p '$D/.cov' && /opt/woow/venv/bin/python -m pip install -q \
         --target '$D/.cov' coverage==7.6.1"
fi

set +e
SLOT=$SLOT bash "$SBX" exec bash -c \
    "cd /tmp && PATH='$BROWSER_ROOT/bin':\$PATH PYTHONPATH='$D/.cov:$BROWSER_PYDEPS' /opt/woow/venv/bin/python -m coverage run \
     --data-file='$D/.cov/data' --source='$D/woow_document_management' \
     --omit='*/tests/*' /usr/bin/odoo \
     -c /etc/odoo-sbx/odoo.conf \
     --addons-path='$D,/usr/lib/python3/dist-packages/odoo/addons' \
     -d '$DB' -i woow_document_management --test-enable \
     --test-tags /woow_document_management --stop-after-init --http-port='$PORT' \
     > '$D/.cov/odoo.log' 2>&1; rc=\$?; tail -n 12 '$D/.cov/odoo.log'; exit \$rc"
odoo_rc=$?
set -e
if (( odoo_rc != 0 )); then
    echo "Odoo coverage test failed: exit $odoo_rc" >&2
    exit "$odoo_rc"
fi
SLOT=$SLOT bash "$SBX" exec bash -c \
    "grep -Fq 'TOUR tour_ca21_fileviewer_custom_url SUCCEEDED' '$D/.cov/odoo.log' && \
     ! grep -Eq ': skipped [A-Za-z_][A-Za-z0-9_]*\\.test_' '$D/.cov/odoo.log'" || {
    echo 'Coverage run skipped a test or lacked the CA-21 browser success signal' >&2
    exit 1
}

SLOT=$SLOT bash "$SBX" exec bash -c \
    "PYTHONPATH='$D/.cov' /opt/woow/venv/bin/python -m coverage report \
     --data-file='$D/.cov/data' \
     --include='$D/woow_document_management/models/*,$D/woow_document_management/controllers/*,$D/woow_document_management/services/*,$D/woow_document_management/wizards/*,$D/woow_document_management/hooks.py' \
     --fail-under=85"
