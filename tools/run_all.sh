#!/bin/sh
# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
#
# Run every repository check: the 8 static tools, their self-tests, semgrep and gitleaks.
#   sh tools/run_all.sh [--g1]
# --g1 is passed to the tools (release gate: exact sudo counts, no WOOW-STUB marker, 10 entry points).
# semgrep runs when it is on PATH (always in CI: WOOW_REQUIRE_SEMGREP=1 makes a missing semgrep fail).
# gitleaks must be available (PATH or ~/.local/bin); a missing gitleaks fails.
# Exit code: 0 when everything passed, 1 otherwise.
set -u
cd "$(dirname "$0")/.." || exit 2
rc=0
for name in sudo routes forbidden sql sweep_config security_matrix public_methods cleanroom; do
    echo "== check_${name}"
    python3 -I "tools/check_${name}.py" "$@" || rc=1
done

echo "== unittest tools/tests"
python3 -I -m unittest discover -s tools/tests -t tools/tests || rc=1

if command -v semgrep >/dev/null 2>&1; then
    echo "== semgrep p/python p/secrets"
    semgrep scan --config p/python --config p/secrets --error --metrics=off \
        --disable-version-check --quiet . || rc=1
elif [ "${WOOW_REQUIRE_SEMGREP:-0}" = "1" ]; then
    echo "semgrep: unavailable (required)"; rc=1
else
    echo "== semgrep: not on PATH, skipped locally (CI always runs it)"
fi

GITLEAKS=$(command -v gitleaks 2>/dev/null || true)
[ -n "$GITLEAKS" ] || { [ -x "$HOME/.local/bin/gitleaks" ] && GITLEAKS="$HOME/.local/bin/gitleaks"; }
if [ -n "$GITLEAKS" ]; then
    echo "== gitleaks dir"
    "$GITLEAKS" dir . --redact --no-banner --config .github/gitleaks.toml || rc=1
    echo "== gitleaks git (full history)"
    "$GITLEAKS" git . --redact --no-banner --config .github/gitleaks.toml || rc=1
else
    echo "gitleaks: unavailable"; rc=1
fi

[ "$rc" -eq 0 ] && echo "ALL CHECKS PASSED" || echo "CHECKS FAILED"
exit "$rc"
