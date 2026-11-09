# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""Small, fail-closed sandbox runner for isolated core-assumption scenarios."""

import os
import re
import secrets
import subprocess
import sys
from pathlib import Path


class ScenarioError(RuntimeError):
    pass


class Sandbox:
    def __init__(self, case):
        self.case = case
        self.script = os.environ.get('WOOW_SBX', '')
        self.slot = os.environ.get('SLOT', '')
        self.base_db = os.environ.get('WOOW_SBX_BASE_DB', '')
        if (not self.script or not Path(self.script).is_absolute()
                or not Path(self.script).is_file()
                or not os.access(self.script, os.R_OK)
                or not re.fullmatch(r'[a-z][a-z0-9_-]{0,20}', self.slot)
                or self.slot == 'security'
                or not re.fullmatch(r'wdoc_[a-z0-9_]{1,35}', self.base_db)):
            raise ScenarioError('configuration')
        self.db = 'wdoc_ca_%s_%s' % (self.slot.replace('-', '_'), secrets.token_hex(4))
        self.env = dict(os.environ, SLOT=self.slot)
        self.copy_attempted = False

    def call(self, *args, input=None, timeout=120):
        try:
            result = subprocess.run(['bash', self.script, *args], input=input,
                                    env=self.env, capture_output=True, text=True,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScenarioError(type(exc).__name__) from exc
        if result.returncode:
            raise ScenarioError('%s_failed' % args[0])
        return result.stdout

    def copy(self):
        self.copy_attempted = True
        self.call('copydb', self.base_db, self.db, timeout=300)

    def cleanup(self):
        if self.copy_attempted:
            self.call('dropdb', self.db, timeout=180)


def run(case, check):
    sandbox = None
    reason = None
    try:
        sandbox = Sandbox(case)
        check(sandbox)
    except ScenarioError as exc:
        reason = str(exc)
    finally:
        if sandbox is not None:
            try:
                sandbox.cleanup()
            except ScenarioError:
                reason = 'cleanup_failed'
    if reason:
        sys.stderr.write('SC|case=%s|status=FAIL|reason=%s\n' % (case, reason))
        return 2 if reason == 'configuration' else 1
    sys.stdout.write('SC|case=%s|status=PASS\n' % case)
    return 0
