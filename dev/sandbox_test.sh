#!/usr/bin/env bash
# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
# SLOT=private WOOW_SBX=/absolute/sbx.sh BASE_DB=wdoc_core bash dev/sandbox_test.sh
set -euo pipefail
die() { printf 'G1 FAIL: %s\n' "$*" >&2; exit 1; }

slot=${SLOT:?set a private SLOT}
sbx=${WOOW_SBX:?set WOOW_SBX to an absolute sandbox launcher}
base_db=${BASE_DB:-${WOOW_SBX_BASE_DB:-}}
[[ -n $base_db ]] || die 'set BASE_DB (or WOOW_SBX_BASE_DB)'
[[ $sbx = /* && -f $sbx ]] || die 'WOOW_SBX must be an absolute file path'
[[ $slot =~ ^[a-z][a-z0-9_]*$ && $slot != main && $slot != release && $slot != security ]] || die 'private lowercase SLOT required'
[[ $base_db =~ ^[a-z][a-z0-9_]*$ ]] || die 'invalid BASE_DB'
[[ $# -eq 0 ]] || die 'G1 runs the complete /woow_document_management tag only'
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
addon=$repo/woow_document_management
expected=$addon/tests/EXPECTED_MIN.json
[[ -f $expected && -f $addon/__manifest__.py ]] || die 'addon or EXPECTED_MIN missing'
command -v python3 >/dev/null || die 'python3 is required'

# Unique name matters: copydb drops its destination before copying.
db="g1_${slot:0:32}_$$_$(od -An -N4 -tu4 /dev/urandom | tr -d ' ')"
[[ ${#db} -le 63 ]] || die 'generated DB name too long'
receipt=$(mktemp /tmp/woow-g1-receipt.XXXXXX)
log_copy=$(mktemp /tmp/woow-g1-log.XXXXXX)
copy_attempted=0
remote_log=
cleanup() {
    result=$?
    trap - EXIT INT TERM
    if [[ $copy_attempted -eq 1 ]] && ! SLOT="$slot" bash "$sbx" dropdb "$db"; then
        printf 'G1 FAIL: cleanup failed for %s\n' "$db" >&2
        result=1
    fi
    if [[ $result -eq 0 ]]; then
        rm -f -- "$receipt" "$log_copy"
    else
        printf 'G1 evidence: receipt=%s log=%s remote_log=%s\n' \
            "$receipt" "$log_copy" "${remote_log:-unavailable}" >&2
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if SLOT="$slot" bash "$sbx" listdb | grep -Fxq "$db"; then
    die "generated database already exists: $db"
fi
printf 'G1 slot=%s base_db=%s disposable_db=%s\n' "$slot" "$base_db" "$db"
SLOT="$slot" bash "$sbx" sync "$addon"
copy_attempted=1
SLOT="$slot" bash "$sbx" copydb "$base_db" "$db"

# sbx.sh may exit 0 after Odoo returned RC 1. Its printed RC is mandatory.
if ! SLOT="$slot" bash "$sbx" test "$db" woow_document_management \
        /woow_document_management >"$receipt" 2>&1; then
    cat "$receipt" >&2
    die 'sandbox launcher failed'
fi
cat "$receipt"
remote_log=$(sed -n 's/^LOG \(\/[^[:space:]]*\)$/\1/p' "$receipt")
[[ $(printf '%s\n' "$remote_log" | grep -c '^/') -eq 1 ]] || die 'missing or ambiguous LOG path'
[[ $remote_log == /var/lib/odoo/logs/* ]] || die 'unexpected log path'
SLOT="$slot" bash "$sbx" exec cat "$remote_log" >"$log_copy" || die 'cannot fetch full Odoo log'

python3 - "$expected" "$receipt" "$log_copy" <<'PY'
import collections
import json
import re
import sys

expected_path, receipt_path, log_path = sys.argv[1:]
with open(expected_path, encoding='utf-8') as stream:
    expected = json.load(stream)
with open(receipt_path, encoding='utf-8') as stream:
    receipt = stream.read()
with open(log_path, encoding='utf-8') as stream:
    log = stream.read()
errors = []
rc = re.findall(r'^RC ([0-9]+)$', receipt, re.M)
if rc != ['0']:
    errors.append(f'expected exactly RC 0, got {rc!r}')
summary = re.findall(r'(\d+) failed, (\d+) error\(s\) of (\d+) tests', log)
if len(summary) != 1:
    errors.append(f'expected one Odoo summary, got {len(summary)}')
    total = 0
else:
    failed, raised, total = map(int, summary[0])
    if failed or raised:
        errors.append(f'Odoo summary: {failed} failed, {raised} errors')
# Odoo logs expected PostgreSQL errors at ERROR level when a test asserts that
# a constraint or a bounded lock rejects a write. Permit only those exact
# test/error pairs; every other ERROR or FAIL line still fails the gate.
expected_sql_errors = {
    'test_active_folder_names_are_unique_per_parent_and_company':
        'duplicate key value violates unique constraint "woow_document_folder_name_uniq"',
    'test_portal_grants_never_exceed_upload':
        'violates check constraint "woow_document_folder_access_portal_max_upload"',
    'test_bounded_wait_rolls_back_orm_cache_on_row_lock':
        'canceling statement due to lock timeout',
    'test_timeout_keeps_transaction_usable':
        'canceling statement due to lock timeout',
    'test_trusted_issue_can_create_but_scope_cannot_be_rewritten':
        'duplicate key value violates unique constraint "woow_document_share_token_hash_uniq"',
}
current_test = None
pending_sql_error = False
for line in log.splitlines():
    start = re.search(r': Starting [A-Za-z_][A-Za-z0-9_]*\.(test_[A-Za-z0-9_]+)', line)
    if start:
        current_test = start.group(1)
    if pending_sql_error:
        allowed = expected_sql_errors.get(current_test)
        if not (allowed and line.startswith('ERROR: ') and allowed in line):
            errors.append(f'unexpected SQL error in {current_test}: {line[:160]}')
        pending_sql_error = False
        continue
    if re.search(r'\bERROR\b|FAIL:', line):
        if ' ERROR ' in line and ' odoo.sql_db: bad query:' in line \
                and current_test in expected_sql_errors:
            pending_sql_error = True
        else:
            errors.append(f'unexpected Odoo error in {current_test}: {line[:160]}')
if pending_sql_error:
    errors.append('truncated SQL error at end of log')
skipped = re.findall(r': skipped ([A-Za-z_][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+)\s*:', log)
if skipped:
    errors.append('Odoo skipped tests: ' + ', '.join(skipped))
if 'TOUR tour_ca21_fileviewer_custom_url SUCCEEDED' not in log:
    errors.append('CA-21 browser tour success signal missing')
started = re.findall(
    r'odoo\.addons\.woow_document_management\.tests\.(test_[A-Za-z0-9_]+): '
    r'Starting [A-Za-z_][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+', log)
by_file = collections.Counter(name + '.py' for name in started)
if not started or len(started) != total:
    errors.append(f'Starting lines={len(started)}, summary tests={total}')
batch_files = {
    'share': {'test_share_routes.py', 'test_share_token.py', 'test_mail_share.py'},
    'portal': {'test_portal_routes.py', 'test_portal_upload.py'},
    'files': {'test_upload_policy.py', 'test_quota.py', 'test_zip_builder.py',
              'test_trash_lifecycle.py', 'test_audit_log.py', 'test_attachment_guard.py',
              'test_add_attachment.py', 'test_native_routes.py', 'test_internal_routes.py'},
}
by_batch = collections.Counter()
for name, count in by_file.items():
    batch = next((key for key, names in batch_files.items() if name in names), 'core')
    by_batch[batch] += count
if expected.get('version') != 1 or not isinstance(expected.get('batches'), dict) \
        or not isinstance(expected.get('files'), dict):
    errors.append('EXPECTED_MIN schema invalid')
else:
    for batch in ('core', 'share', 'portal', 'files'):
        floor = expected['batches'].get(batch)
        if type(floor) is not int or floor <= 0:
            errors.append(f'{batch} floor must be positive')
        elif by_batch[batch] < floor:
            errors.append(f'{batch}: {by_batch[batch]} < {floor}')
    for name, floor in expected['files'].items():
        if not re.fullmatch(r'test_[A-Za-z0-9_]+\.py', name) \
                or type(floor) is not int or floor <= 0:
            errors.append(f'invalid file floor: {name}={floor!r}')
        elif by_file[name] < floor:
            errors.append(f'{name}: {by_file[name]} < {floor}')
    if not expected['files']:
        errors.append('per-file floors empty')
    required = expected.get('required_ca_methods')
    if not isinstance(required, dict) or set(required) != {f'{i:02d}' for i in range(1, 36) if i not in (6, 23, 24, 28)}:
        errors.append('CA method map must cover the 31 Odoo CA cases')
    else:
        for ca, method in required.items():
            if not isinstance(method, str) or not re.fullmatch(r'test_ca[0-3][0-9]_[A-Za-z0-9_]+', method) or f'ca{ca}_' not in method:
                errors.append(f'invalid CA method mapping for {ca}')
                continue
            matches = re.findall(r': Starting [A-Za-z_][A-Za-z0-9_]*\.' + re.escape(method) + r' \.\.\.', log)
            if len(matches) != 1:
                errors.append(f'CA-{ca} started {len(matches)} times: {method}')
    if total < sum(expected['batches'].values()):
        errors.append(f'total {total} below combined batch floor')
print(f'G1 total={total} batches={dict(sorted(by_batch.items()))}')
print('G1 files=' + json.dumps(dict(sorted(by_file.items())), sort_keys=True))
for error in errors:
    print(f'G1 FAIL: {error}', file=sys.stderr)
sys.exit(bool(errors))
PY
printf 'G1 PASS: validated Odoo receipt and EXPECTED_MIN\n'
