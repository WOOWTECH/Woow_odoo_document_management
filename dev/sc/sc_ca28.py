# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""CA-28: exercise the real uninstall loader path on an isolated database."""

from pathlib import Path

from sc_ca_runtime import ScenarioError, run


PROBE = '''import sys
import odoo.addons.woow_document_management as addon
if not env.su:
    raise RuntimeError('CA28_SHELL_NOT_SUPERUSER')
module = env['ir.module.module'].sudo().search([
    ('name', '=', 'woow_document_management'), ('state', '=', 'installed')], limit=1)
if not module:
    raise RuntimeError('module_not_installed')
def probe(hook_env):
    if not hook_env.su:
        raise RuntimeError('CA28_NOT_SUPERUSER')
    raise RuntimeError('CA28_SUPERUSER_OK')
addon.uninstall_hook = probe
try:
    module.button_immediate_uninstall()
except RuntimeError as exc:
    if str(exc) != 'CA28_SUPERUSER_OK':
        raise
    sys.stdout.write('CA28_SHELL_AND_HOOK_SUPERUSER_OK\\n')
else:
    raise AssertionError('uninstall_hook_not_called')
'''


def check(sandbox):
    addon = Path(__file__).resolve().parents[2] / 'woow_document_management'
    sandbox.call('sync', str(addon), 'woow_document_management', timeout=180)
    sandbox.copy()
    output = sandbox.call('shell', sandbox.db, input=PROBE, timeout=600)
    if output.splitlines().count('CA28_SHELL_AND_HOOK_SUPERUSER_OK') != 1:
        raise ScenarioError('shell_or_hook_not_superuser')


if __name__ == '__main__':
    raise SystemExit(run('T-CA-28', check))
